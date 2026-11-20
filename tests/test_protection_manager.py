import pytest
from nebula.config import Settings, SafetyError
from nebula.exchange import OkxDemoClient, OkxError
from nebula.protection_manager import DemoProtectionManager, ProtectionManagerError
from nebula.state import StateStore


class PublicDemoStub:
    def get_spot_instrument(self, inst_id):
        return {'instType':'SPOT','instId':inst_id,'baseCcy':'BTC','quoteCcy':'USDT',
                'tickSz':'0.1','lotSz':'0.0001','minSz':'0.0001','state':'live'}


def seeded(tmp_path, state='filled', fill='0.004', risk_plan=None):
    store = StateStore(str(tmp_path / 'protection.db'))
    store.reserve_entry_intent('NBENTRY1', 'BTC-USDT', 123, 'buy', risk_plan=risk_plan)
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


def test_protection_uses_exact_persisted_portfolio_approved_levels(tmp_path):
    plan = {
        "instrument": "BTC-USDT", "size": "0.01", "entry_price": "50000",
        "stop_loss": "48000", "take_profit": "52000", "risk_to_stop_quote": "20",
    }
    store = seeded(tmp_path, risk_plan=plan)
    manager = DemoProtectionManager(PublicDemoStub(), store)

    result = manager.prepare(entry_client_order_id="NBENTRY1")
    assert result["status"] == "reserved"
    assert result["stop_loss"] == "48000"
    assert result["take_profit"] == "52000"


def test_protection_rejects_levels_that_differ_from_approved_entry_plan(tmp_path):
    plan = {
        "instrument": "BTC-USDT", "size": "0.01", "entry_price": "50000",
        "stop_loss": "48000", "take_profit": "52000", "risk_to_stop_quote": "20",
    }
    store = seeded(tmp_path, risk_plan=plan)
    manager = DemoProtectionManager(PublicDemoStub(), store)

    with pytest.raises(ProtectionManagerError, match="differ"):
        manager.prepare(
            entry_client_order_id="NBENTRY1",
            take_profit="53000",
            stop_loss="48000",
        )
    assert store.get_protection_intent("NBENTRY1") is None


def test_protection_rejects_actual_fill_above_approved_take_profit(tmp_path):
    plan = {
        "instrument": "BTC-USDT", "size": "0.01", "entry_price": "50000",
        "stop_loss": "48000", "take_profit": "52000", "risk_to_stop_quote": "20",
    }
    store = seeded(tmp_path, risk_plan=plan)
    store.save_order_snapshot("NBENTRY1", {
        "ordId": "10001", "state": "filled", "sz": "0.01",
        "accFillSz": "0.004", "avgPx": "52500",
        "fee": "-0.1", "feeCcy": "USDT",
    })
    manager = DemoProtectionManager(PublicDemoStub(), store)

    with pytest.raises(ProtectionManagerError, match="Persisted approved entry plan is invalid"):
        manager.prepare(entry_client_order_id="NBENTRY1")
    assert store.get_protection_intent("NBENTRY1") is None


def test_protection_rejects_fill_quantity_above_approved_entry_size(tmp_path):
    plan = {
        "instrument": "BTC-USDT", "size": "0.003", "entry_price": "50000",
        "stop_loss": "48000", "take_profit": "52000", "risk_to_stop_quote": "6",
    }
    store = seeded(tmp_path, risk_plan=plan, fill="0.004")
    manager = DemoProtectionManager(PublicDemoStub(), store)

    with pytest.raises(ProtectionManagerError, match="Persisted approved entry plan is invalid"):
        manager.prepare(entry_client_order_id="NBENTRY1")
    assert store.get_protection_intent("NBENTRY1") is None


def test_exchange_backed_protection_requires_persisted_approved_plan(tmp_path):
    class ExchangeBacked(PublicDemoStub):
        settings = Settings(max_daily_loss_quote=10)

    store = seeded(tmp_path)
    manager = DemoProtectionManager(ExchangeBacked(), store)

    with pytest.raises(ProtectionManagerError, match="No persisted portfolio-approved"):
        manager.prepare(
            entry_client_order_id="NBENTRY1",
            take_profit="52000",
            stop_loss="48000",
        )
    assert store.get_protection_intent("NBENTRY1") is None


def test_exchange_backed_oco_uses_net_base_after_base_currency_fee(tmp_path):
    plan = {
        "instrument": "BTC-USDT", "size": "0.004", "entry_price": "50000",
        "stop_loss": "48000", "take_profit": "52000", "risk_to_stop_quote": "8",
    }
    store = seeded(tmp_path, fill="0.004", risk_plan=plan)

    class ExchangeBacked(PublicDemoStub):
        settings = Settings(max_daily_loss_quote=10)

        def get_fills_history(self, instrument, order_id, limit=100, after=None):
            assert instrument == "BTC-USDT"
            assert order_id == "10001"
            return [{
                "instId": "BTC-USDT", "ordId": "10001", "tradeId": "70001",
                "clOrdId": "NBENTRY1", "side": "buy",
                "fillSz": "0.004", "fillPx": "50000",
                "fee": "-0.0001", "feeCcy": "BTC",
                "fillTime": "1800000000000",
            }]

    manager = DemoProtectionManager(ExchangeBacked(), store)
    result = manager.prepare(entry_client_order_id="NBENTRY1")

    assert result["status"] == "reserved"
    assert result["filled_size"] == "0.0039"
    assert store.get_protection_intent("NBENTRY1")["filled_size"] == "0.0039"


def test_exchange_backed_oco_blocks_when_trade_fill_ledger_is_incomplete(tmp_path):
    plan = {
        "instrument": "BTC-USDT", "size": "0.004", "entry_price": "50000",
        "stop_loss": "48000", "take_profit": "52000", "risk_to_stop_quote": "8",
    }
    store = seeded(tmp_path, fill="0.004", risk_plan=plan)

    class ExchangeBacked(PublicDemoStub):
        settings = Settings(max_daily_loss_quote=10)

        def get_fills_history(self, instrument, order_id, limit=100, after=None):
            return []

    manager = DemoProtectionManager(ExchangeBacked(), store)
    with pytest.raises(ProtectionManagerError, match="Entry fills/fees could not be reconciled"):
        manager.prepare(entry_client_order_id="NBENTRY1")
    assert store.get_protection_intent("NBENTRY1") is None


def test_prepare_requires_position_lock_owner(tmp_path):
    store = StateStore(str(tmp_path / 'unlocked.db'))
    store.reserve_intent('NBENTRY1','BTC-USDT',123,'buy')
    store.update_intent('NBENTRY1','filled','1')
    store.save_order_snapshot('NBENTRY1', {'ordId':'1','state':'filled','sz':'1','accFillSz':'1','avgPx':'10'})
    with pytest.raises(ProtectionManagerError, match='lock'):
        DemoProtectionManager(PublicDemoStub(), store).prepare(entry_client_order_id='NBENTRY1', take_profit='12', stop_loss='9')


@pytest.mark.parametrize(
    "available, pending, expected",
    [
        ("0.003", [], "available base balance"),
        ("0.01", [{"instId": "BTC-USDT"}], "open regular/algo orders"),
    ],
)
def test_exchange_backed_oco_requires_fresh_unreserved_base_balance(
    tmp_path, available, pending, expected
):
    class ExchangeBacked(PublicDemoStub):
        def __init__(self):
            self.place_calls = 0

        def get_balance(self):
            return [{"details": [
                {"ccy": "BTC", "availBal": available, "frozenBal": "0"},
                {"ccy": "USDT", "availBal": "1000", "frozenBal": "0"},
            ]}]

        def get_pending_orders(self, inst_type):
            return pending

        def get_pending_algo_orders(self, ord_type):
            return []

        def place_spot_oco_exit(self, **kwargs):
            self.place_calls += 1
            return [{"algoId": "9988", "sCode": "0"}]

    store = seeded(tmp_path)
    client = ExchangeBacked()
    manager = DemoProtectionManager(client, store)
    manager.prepare(
        entry_client_order_id="NBENTRY1",
        take_profit="52000",
        stop_loss="48000",
    )
    # The seeded intent is a legacy fake entry without risk-plan/real fills.
    # Move the client into the exchange-backed path only for the pre-submit gate.
    client.settings = Settings()

    with pytest.raises(ProtectionManagerError, match="Fresh Spot balance/order check failed"):
        manager.submit_reserved("NBENTRY1")

    assert store.get_protection_intent("NBENTRY1")["status"] == "reserved"
    assert client.place_calls == 0
    assert store.get_position_lock()["owner_client_order_id"] == "NBENTRY1"


def test_local_oco_gate_block_remains_retryable_without_network_io(tmp_path):
    store = seeded(tmp_path)

    class Client(PublicDemoStub):
        def __init__(self):
            self.demo = OkxDemoClient(Settings())

        def place_spot_oco_exit(self, **kwargs):
            return self.demo.place_spot_oco_exit(**kwargs)

    manager_client = Client()
    manager = DemoProtectionManager(manager_client, store)
    manager.prepare(entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')

    # The default Settings gate blocks before the HTTP client is called.
    with pytest.raises(SafetyError):
        manager.submit_reserved('NBENTRY1')
    intent = store.get_protection_intent('NBENTRY1')
    assert intent['status'] == 'reserved'
    assert intent['last_error_type'] == 'SafetyError'

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'code': '0', 'data': [{'algoId': '9988', 'sCode': '0'}]}

    class Session:
        def __init__(self):
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return Response()

    settings = Settings(
        api_key='demo-key', secret_key='demo-secret', passphrase='demo-pass',
        enable_demo_orders=True, kill_switch=False,
        max_daily_loss_quote=20, max_risk_quote=2, max_position_quote=100,
    )
    session = Session()
    manager_client.demo = OkxDemoClient(settings, session=session)
    result = manager.submit_reserved('NBENTRY1')

    assert result['status'] == 'active'
    assert result['exchange_algo_id'] == '9988'
    assert len(session.calls) == 1
    assert session.calls[0][1]['allow_redirects'] is False


def test_ambiguous_oco_intent_cannot_be_deferred_to_reserved(tmp_path):
    store = seeded(tmp_path)
    DemoProtectionManager(PublicDemoStub(), store).prepare(
        entry_client_order_id='NBENTRY1', take_profit='52000', stop_loss='48000')
    store.update_protection_intent('NBENTRY1', 'submission_unknown')
    with pytest.raises(ValueError, match='Only a locally blocked OCO submission'):
        store.defer_protection_intent('NBENTRY1', error_type='SafetyError')
    assert store.get_protection_intent('NBENTRY1')['status'] == 'submission_unknown'


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


def test_partially_effective_oco_is_not_misclassified_as_live(tmp_path):
    class Lookup(PublicDemoStub):
        def get_algo_order(self, algo_client_order_id):
            return [{
                'algoClOrdId': algo_client_order_id,
                'algoId': '9991',
                'state': 'partially_effective',
                'ordIdList': [],
            }]

    store = seeded(tmp_path)
    manager = DemoProtectionManager(Lookup(), store)
    manager.prepare(
        entry_client_order_id='NBENTRY1',
        take_profit='52000',
        stop_loss='48000',
    )
    store.update_protection_intent('NBENTRY1', 'submission_unknown')

    result = manager.reconcile('NBENTRY1')

    assert result['status'] == 'triggered'
    assert result['position_lock_retained'] is True
    assert result['child_orders_reconciled'] is False
    assert store.get_position_lock()['owner_client_order_id'] == 'NBENTRY1'


def test_partially_failed_oco_remains_blocked_and_reconcilable(tmp_path):
    class Lookup(PublicDemoStub):
        def __init__(self):
            self.calls = 0

        def get_algo_order(self, algo_client_order_id):
            self.calls += 1
            return [{
                'algoClOrdId': algo_client_order_id,
                'algoId': '9992',
                'state': 'partially_failed',
                'ordIdList': [],
            }]

    store = seeded(tmp_path)
    client = Lookup()
    manager = DemoProtectionManager(client, store)
    manager.prepare(
        entry_client_order_id='NBENTRY1',
        take_profit='52000',
        stop_loss='48000',
    )
    store.update_protection_intent('NBENTRY1', 'submission_unknown')

    first = manager.reconcile('NBENTRY1')
    second = manager.reconcile('NBENTRY1')

    assert first['status'] == 'partially_failed'
    assert first['position_lock_retained'] is True
    assert second['status'] == 'partially_failed'
    assert client.calls == 2
    assert store.get_position_lock()['owner_client_order_id'] == 'NBENTRY1'
    with pytest.raises(ProtectionManagerError, match='never resubmit'):
        manager.submit_reserved('NBENTRY1')


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
