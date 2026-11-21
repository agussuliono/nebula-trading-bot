import pytest
from nebula.config import SafetyError
from nebula.protection_manager import DemoProtectionManager, ProtectionManagerError
from nebula.state import StateStore


class InstrumentStub:
    def get_spot_instrument(self, inst_id):
        return {'instType':'SPOT','instId':inst_id,'baseCcy':'BTC','quoteCcy':'USDT',
                'tickSz':'0.1','lotSz':'0.0001','minSz':'0.0001','state':'live'}


def seeded(tmp_path, *, state='partially_filled', fill='0.002'):
    store = StateStore(str(tmp_path / 'entry-cancel.db'))
    store.reserve_entry_intent('NBENTRY1', 'BTC-USDT', 123, 'buy')
    store.update_intent('NBENTRY1', 'submitting')
    store.update_intent('NBENTRY1', 'acknowledged', '10001')
    store.update_intent('NBENTRY1', state, '10001')
    store.save_order_snapshot('NBENTRY1', {'ordId':'10001','clOrdId':'NBENTRY1','state':state,
        'sz':'0.01','accFillSz':fill,'avgPx':'50000' if fill != '0' else '',
        'fee':'-0.1','feeCcy':'USDT'})
    return store


class CancelLookup(InstrumentStub):
    def __init__(self, order, cancel_error=None, lookup_error=None):
        self.order = order
        self.cancel_error = cancel_error
        self.lookup_error = lookup_error
        self.cancel_calls = 0
    def cancel_order(self, *, inst_id, order_id):
        self.cancel_calls += 1
        if self.cancel_error:
            raise self.cancel_error
        return [{'ordId':order_id, 'sCode':'0'}]
    def get_order(self, inst_id, client_order_id):
        if self.lookup_error:
            raise self.lookup_error
        return [self.order]


def exchange_order(state, fill='0.002'):
    return {'ordId':'10001','clOrdId':'NBENTRY1','instId':'BTC-USDT','state':state,
            'sz':'0.01','accFillSz':fill,'avgPx':'50000' if fill != '0' else '',
            'fee':'-0.1','feeCcy':'USDT'}


def test_cancel_remainder_requires_terminal_confirmation_and_keeps_partial_position_lock(tmp_path):
    store = seeded(tmp_path)
    client = CancelLookup(exchange_order('canceled'))
    manager = DemoProtectionManager(client, store)
    result = manager.cancel_entry_remainder_and_reconcile('NBENTRY1')
    assert result == {'status':'canceled','filled_size':'0.002'}
    assert client.cancel_calls == 1
    assert store.get_order_snapshot('NBENTRY1')['exchange_state'] == 'canceled'
    assert store.get_position_lock()['owner_client_order_id'] == 'NBENTRY1'
    plan = manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    assert plan['filled_size'] == '0.002'


def test_cancel_ack_does_not_mean_terminal_and_retries_only_reconciliation(tmp_path):
    store = seeded(tmp_path)
    client = CancelLookup(exchange_order('partially_filled', '0.003'))
    manager = DemoProtectionManager(client, store)
    result = manager.cancel_entry_remainder_and_reconcile('NBENTRY1')
    assert result['status'] == 'cancel_pending'
    assert result['exchange_state'] == 'partially_filled'
    assert store.get_position_lock() is not None
    plan = manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    assert plan['status'] == 'waiting_entry_terminal'
    assert store.get_protection_intent('NBENTRY1') is None


def test_cancel_timeout_but_exchange_lookup_finds_filled_race(tmp_path):
    store = seeded(tmp_path)
    client = CancelLookup(exchange_order('filled', '0.01'), cancel_error=TimeoutError('cancel timeout'))
    manager = DemoProtectionManager(client, store)
    result = manager.cancel_entry_remainder_and_reconcile('NBENTRY1')
    assert result == {'status':'filled','filled_size':'0.01'}
    assert store.get_order_snapshot('NBENTRY1')['exchange_state'] == 'filled'


def test_zero_fill_confirmed_cancel_releases_slot(tmp_path):
    store = seeded(tmp_path, fill='0')
    client = CancelLookup(exchange_order('canceled', '0'))
    result = DemoProtectionManager(client, store).cancel_entry_remainder_and_reconcile('NBENTRY1')
    assert result == {'status':'canceled','filled_size':'0'}
    assert store.get_position_lock() is None


def test_cancel_gate_block_does_not_change_order_state(tmp_path):
    store = seeded(tmp_path)
    client = CancelLookup(exchange_order('canceled'), cancel_error=SafetyError('gate off'))
    manager = DemoProtectionManager(client, store)
    with pytest.raises(ProtectionManagerError, match='safety gate'):
        manager.cancel_entry_remainder_and_reconcile('NBENTRY1')
    assert store.get_intent('NBENTRY1')['status'] == 'partially_filled'
    assert store.get_position_lock() is not None
    assert client.cancel_calls == 1


def test_lookup_failure_after_cancel_keeps_slot_and_does_not_assume_cancelled(tmp_path):
    store = seeded(tmp_path)
    client = CancelLookup(exchange_order('canceled'), lookup_error=TimeoutError('lookup timeout'))
    manager = DemoProtectionManager(client, store)
    with pytest.raises(ProtectionManagerError, match='not confirmed'):
        manager.cancel_entry_remainder_and_reconcile('NBENTRY1')
    assert store.get_position_lock() is not None
    assert store.get_intent('NBENTRY1')['status'] == 'partially_filled'
