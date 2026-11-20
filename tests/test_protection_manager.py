import pytest
from nebula.config import Settings, SafetyError
from nebula.exchange import OkxDemoClient, OkxError
from nebula.protection_manager import DemoProtectionManager, ProtectionManagerError
from nebula.state import StateStore


class PublicDemoStub:
    def get_spot_instrument(self, inst_id):
        return {'instType':'SPOT','instId':inst_id,'baseCcy':'BTC','quoteCcy':'USDT',
                'tickSz':'0.1','lotSz':'0.0001','minSz':'0.0001','state':'live'}


def seeded(tmp_path, state='filled', fill='0.004'):
    store = StateStore(str(tmp_path / 'protection.db'))
    store.reserve_entry_intent('NBENTRY1', 'BTC-USDT', 123, 'buy')
    store.update_intent('NBENTRY1', state, '10001')
    store.save_order_snapshot('NBENTRY1', {'ordId':'10001','state':state,'sz':'0.01',
        'accFillSz':fill,'avgPx':'50000','fee':'-0.1','feeCcy':'USDT'})
    return store


def test_partial_entry_waits_until_terminal_before_protection(tmp_path):
    store = seeded(tmp_path, state='partially_filled')
    result = DemoProtectionManager(PublicDemoStub(), store).prepare(
        entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    assert result['status'] == 'waiting_entry_terminal'
    assert store.get_protection_intent('NBENTRY1') is None


def test_terminal_partial_fill_can_be_protected_at_final_quantity(tmp_path):
    store = seeded(tmp_path, state='canceled', fill='0.0025')
    result = DemoProtectionManager(PublicDemoStub(), store).prepare(
        entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    assert result['filled_size'] == '0.0025'
    assert result['status'] == 'reserved'
    assert result['algo_client_order_id'].startswith('NBP')


def test_prepare_is_idempotent_and_rejects_parameter_change(tmp_path):
    store = seeded(tmp_path)
    manager = DemoProtectionManager(PublicDemoStub(), store)
    first = manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    again = manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    assert first['algo_client_order_id'] == again['algo_client_order_id']
    with pytest.raises(ProtectionManagerError, match='different'):
        manager.prepare(entry_client_order_id='NBENTRY1', take_profit='53000', stop_loss='48000')


def test_prepare_requires_position_lock_owner(tmp_path):
    store = StateStore(str(tmp_path / 'unlocked.db'))
    store.reserve_intent('NBENTRY1','BTC-USDT',123,'buy')
    store.update_intent('NBENTRY1','filled','1')
    store.save_order_snapshot('NBENTRY1', {'ordId':'1','state':'filled','sz':'1','accFillSz':'1','avgPx':'10'})
    with pytest.raises(ProtectionManagerError, match='lock'):
        DemoProtectionManager(PublicDemoStub(), store).prepare(entry_client_order_id='NBENTRY1', take_profit='12', stop_loss='9')


def test_submit_stays_gated_and_never_retries_failed_intent(tmp_path):
    store = seeded(tmp_path)
    class Client(PublicDemoStub):
        def __init__(self): self.demo = OkxDemoClient(Settings())
        def place_spot_oco_exit(self, **kwargs): return self.demo.place_spot_oco_exit(**kwargs)
    manager = DemoProtectionManager(Client(), store)
    manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    with pytest.raises(SafetyError):
        manager.submit_reserved('NBENTRY1')
    assert store.get_protection_intent('NBENTRY1')['status'] == 'failed'
    with pytest.raises(ProtectionManagerError, match='never resubmit'):
        manager.submit_reserved('NBENTRY1')


def test_ambiguous_oco_submission_is_persistently_blocked(tmp_path):
    class Ambiguous(PublicDemoStub):
        def place_spot_oco_exit(self, **kwargs):
            raise OkxError('OKX request timed out; outcome may be unknown')
    store = seeded(tmp_path)
    manager = DemoProtectionManager(Ambiguous(), store)
    manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    with pytest.raises(ProtectionManagerError, match='reconcile'):
        manager.submit_reserved('NBENTRY1')
    assert store.get_protection_intent('NBENTRY1')['status'] == 'submission_unknown'
    with pytest.raises(ProtectionManagerError, match='never resubmit'):
        manager.submit_reserved('NBENTRY1')


def test_reconcile_recovers_active_algo_after_restart(tmp_path):
    class Lookup(PublicDemoStub):
        def get_algo_order(self, algo_client_order_id):
            return [{'algoClOrdId':algo_client_order_id,'algoId':'9988','state':'live'}]
    store = seeded(tmp_path)
    manager = DemoProtectionManager(Lookup(), store)
    intent = manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    store.update_protection_intent('NBENTRY1','submission_unknown')
    result = manager.reconcile('NBENTRY1')
    assert result['status'] == 'active'
    assert result['exchange_algo_id'] == '9988'


def test_reconcile_marks_triggered_algo_without_releasing_position_lock(tmp_path):
    class Lookup(PublicDemoStub):
        def get_algo_order(self, algo_client_order_id):
            return [{'algoClOrdId':algo_client_order_id,'algoId':'9989','state':'effective',
                     'ordIdList':[]}]
    store = seeded(tmp_path)
    manager = DemoProtectionManager(Lookup(), store)
    manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    store.update_protection_intent('NBENTRY1','submission_unknown')
    result = manager.reconcile('NBENTRY1')
    assert result['status'] == 'triggered'
    assert store.get_position_lock()['owner_client_order_id'] == 'NBENTRY1'


def test_reconcile_missing_algo_remains_ambiguous(tmp_path):
    class Lookup(PublicDemoStub):
        def get_algo_order(self, algo_client_order_id): return []
    store = seeded(tmp_path)
    manager = DemoProtectionManager(Lookup(), store)
    manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    store.update_protection_intent('NBENTRY1','submission_unknown')
    result = manager.reconcile('NBENTRY1')
    assert result['status'] == 'submission_unknown'


def test_protection_rejects_fill_below_minimum_or_off_lot(tmp_path):
    class BadLot(PublicDemoStub):
        def get_spot_instrument(self, inst_id):
            row = super().get_spot_instrument(inst_id)
            row['lotSz'] = '0.003'
            row['minSz'] = '0.001'
            return row
    store = seeded(tmp_path, fill='0.004')
    with pytest.raises(ProtectionManagerError, match='lotSz/minSz'):
        DemoProtectionManager(BadLot(), store).prepare(
            entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')


def test_protection_intent_survives_state_store_restart(tmp_path):
    path = tmp_path / 'restart.db'
    store = StateStore(str(path))
    store.reserve_entry_intent('NBENTRY1','BTC-USDT',123,'buy')
    store.update_intent('NBENTRY1','filled','10001')
    store.save_order_snapshot('NBENTRY1', {'ordId':'10001','state':'filled','sz':'0.01',
        'accFillSz':'0.004','avgPx':'50000'})
    DemoProtectionManager(PublicDemoStub(), store).prepare(
        entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    restarted = StateStore(str(path))
    assert restarted.get_protection_intent('NBENTRY1')['status'] == 'reserved'


def test_triggered_oco_missing_from_lookup_does_not_downgrade_or_unlock(tmp_path):
    class Missing(PublicDemoStub):
        def get_algo_order(self, algo_client_order_id): return []
    store = seeded(tmp_path)
    manager = DemoProtectionManager(Missing(), store)
    manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    store.update_protection_intent('NBENTRY1', 'triggered', exchange_algo_id='9989')
    with pytest.raises(ProtectionManagerError, match='terminal OCO is missing'):
        manager.reconcile('NBENTRY1')
    assert store.get_protection_intent('NBENTRY1')['status'] == 'triggered'
    assert store.get_position_lock()['owner_client_order_id'] == 'NBENTRY1'


def test_terminal_oco_state_cannot_regress_to_active(tmp_path):
    store = seeded(tmp_path)
    store.reserve_protection_intent(entry_client_order_id='NBENTRY1', instrument='BTC-USDT',
        algo_client_order_id='NBPTEST1', filled_size='0.004', take_profit='52000', stop_loss='48000')
    store.update_protection_intent('NBENTRY1', 'triggered', exchange_algo_id='9989')
    with pytest.raises(ValueError, match='terminal OCO state'):
        store.update_protection_intent('NBENTRY1', 'active', exchange_algo_id='9989')
