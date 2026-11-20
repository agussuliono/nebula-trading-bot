import pytest
from nebula.orders import DemoOrderManager, OrderManagerError
from nebula.exchange import OkxError
from nebula.state import StateStore


class FakeClient:
    def __init__(self, response=None, error=None, order=None, pending=None):
        self.response = response if response is not None else [{"ordId": "123"}]
        self.error = error
        self.order = order
        self.pending = pending or []
        self.place_calls = 0
        self.last_order = None
    def get_spot_instrument(self, inst_id):
        return {"instType": "SPOT", "instId": inst_id, "baseCcy": inst_id.split("-")[0], "quoteCcy": "USDT",
                "tickSz": "0.1", "lotSz": "0.001", "minSz": "0.001", "state": "live"}
    def place_spot_limit(self, **kwargs):
        self.place_calls += 1
        self.last_order = kwargs
        if self.error:
            raise self.error
        return self.response
    def get_order(self, inst_id, client_order_id):
        return self.order or []
    def get_pending_orders(self, inst_type="SPOT"):
        return self.pending


def setup(tmp_path, client):
    return DemoOrderManager(client, StateStore(str(tmp_path / "state.db")))


def submit(manager, cid="NB123"):
    return manager.submit_limit(inst_id="BTC-USDT", side="buy", size="0.01",
                                price="50000", client_order_id=cid, candle_ts=123)


def test_submit_reserves_and_persists_ack(tmp_path):
    client = FakeClient()
    manager = setup(tmp_path, client)
    result = submit(manager)
    assert result["status"] == "acknowledged"
    assert manager.state.get_intent("NB123")["exchange_order_id"] == "123"
    assert client.place_calls == 1
    assert client.last_order["size"] == "0.01"
    assert client.last_order["price"] == "50000"


def test_duplicate_never_submits_twice(tmp_path):
    client = FakeClient()
    manager = setup(tmp_path, client)
    submit(manager)
    with pytest.raises(OrderManagerError, match="not resubmitted"):
        submit(manager)
    assert client.place_calls == 1


def test_timeout_marks_unknown_and_blocks_retry(tmp_path):
    client = FakeClient(error=OkxError("OKX request timed out; outcome may be unknown"))
    manager = setup(tmp_path, client)
    with pytest.raises(OkxError):
        submit(manager)
    assert manager.state.get_intent("NB123")["status"] == "submission_unknown"
    with pytest.raises(OrderManagerError):
        submit(manager)
    assert client.place_calls == 1


def test_local_gate_failure_is_definitive(tmp_path):
    from nebula.config import SafetyError
    client = FakeClient(error=SafetyError("Demo order submission is disabled"))
    manager = setup(tmp_path, client)
    with pytest.raises(SafetyError):
        submit(manager)
    assert manager.state.get_intent("NB123")["status"] == "rejected"


def test_reconcile_partially_filled_order(tmp_path):
    client = FakeClient(order=[{"ordId": "123", "state": "partially_filled", "sz": "0.010", "accFillSz": "0.004", "avgPx": "49900", "fee": "-0.01", "feeCcy": "USDT"}])
    manager = setup(tmp_path, client)
    submit(manager)
    result = manager.reconcile("NB123")
    assert result["status"] == "partially_filled"
    snapshot = manager.state.get_order_snapshot("NB123")
    assert snapshot["cumulative_fill_size"] == "0.004"
    assert snapshot["fee"] == "-0.01"


def test_reconcile_filled_order(tmp_path):
    client = FakeClient(order=[{"ordId": "123", "state": "filled", "sz": "0.010", "accFillSz": "0.01", "avgPx": "50000", "fee": "-0.02", "feeCcy": "USDT"}])
    manager = setup(tmp_path, client)
    submit(manager)
    assert manager.reconcile("NB123")["status"] == "filled"


def test_missing_from_exchange_remains_blocked(tmp_path):
    client = FakeClient(order=[], pending=[])
    manager = setup(tmp_path, client)
    with pytest.raises(OkxError):
        # Simulate uncertain POST outcome.
        manager.client.error = OkxError("timeout")
        submit(manager)
    result = manager.reconcile("NB123")
    assert result["status"] == "submission_unknown"
    with pytest.raises(OrderManagerError):
        submit(manager)


def test_pending_lookup_can_recover_live_order(tmp_path):
    client = FakeClient(order=[], pending=[{"clOrdId": "NB123", "ordId": "123", "state": "live", "sz": "0.010", "accFillSz": "0", "avgPx": ""}])
    manager = setup(tmp_path, client)
    with pytest.raises(OkxError):
        manager.client.error = OkxError("timeout")
        submit(manager)
    assert manager.reconcile("NB123")["status"] == "live"


def test_state_migrates_legacy_schema_and_records_audit(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE intents (client_order_id TEXT PRIMARY KEY, instrument TEXT NOT NULL, candle_ts INTEGER NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    store = StateStore(str(path))
    assert store.reserve_intent("NB123", "BTC-USDT", 1, "buy")
    store.record_event("test", "NB123", {"safe": True})
    assert store.list_events()[0]["details"] == {"safe": True}


def test_restart_reconciles_persisted_ambiguous_intent(tmp_path):
    db_path = str(tmp_path / "restart.db")
    first_client = FakeClient(error=OkxError("timeout"))
    first = DemoOrderManager(first_client, StateStore(db_path))
    with pytest.raises(OkxError):
        submit(first, "NBRESTART")

    # Simulate process restart: a new StateStore and manager load the same DB.
    second_client = FakeClient(order=[{"ordId": "777", "clOrdId": "NBRESTART", "state": "live", "sz": "0.010", "accFillSz": "0", "avgPx": ""}])
    second = DemoOrderManager(second_client, StateStore(db_path))
    result = second.reconcile("NBRESTART")
    assert result["status"] == "live"
    with pytest.raises(OrderManagerError, match="not resubmitted"):
        second.submit_limit(inst_id="BTC-USDT", side="buy", size="0.01", price="50000",
                            client_order_id="NBRESTART", candle_ts=123)
    assert second_client.place_calls == 0


def test_second_entry_blocked_while_first_position_slot_is_occupied(tmp_path):
    client = FakeClient()
    manager = setup(tmp_path, client)
    submit(manager, "NBENTRY1")
    with pytest.raises(OrderManagerError, match="slot is occupied"):
        manager.submit_limit(inst_id="ETH-USDT", side="buy", size="0.01", price="2000",
                             client_order_id="NBENTRY2", candle_ts=124)
    assert client.place_calls == 1
    assert manager.state.get_position_lock()["owner_client_order_id"] == "NBENTRY1"


def test_sell_requires_matching_active_position_lock(tmp_path):
    client = FakeClient()
    manager = setup(tmp_path, client)
    with pytest.raises(OrderManagerError, match="active position"):
        manager.submit_limit(inst_id="BTC-USDT", side="sell", size="0.01", price="51000",
                             client_order_id="NBEXIT1", candle_ts=125,
                             position_client_order_id="MISSING")
    assert client.place_calls == 0


def test_sell_is_linked_to_locked_position(tmp_path):
    client = FakeClient()
    manager = setup(tmp_path, client)
    submit(manager, "NBENTRY3")
    result = manager.submit_limit(inst_id="BTC-USDT", side="sell", size="0.01", price="51000",
                                  client_order_id="NBEXIT3", candle_ts=126,
                                  position_client_order_id="NBENTRY3")
    assert result["status"] == "acknowledged"
    assert manager.state.get_position_lock()["owner_client_order_id"] == "NBENTRY3"


def test_position_lock_release_requires_flat_balance_and_open_order_check(tmp_path):
    store = StateStore(str(tmp_path / "flat.db"))
    assert store.reserve_entry_intent("NBFLAT", "BTC-USDT", 1, "buy")
    with pytest.raises(ValueError, match="open-order check"):
        store.release_position_lock_after_flat_check(position_client_order_id="NBFLAT",
            base_balance="0", dust_limit="0.0001", open_orders_checked=False)
    with pytest.raises(ValueError, match="not confirmed flat"):
        store.release_position_lock_after_flat_check(position_client_order_id="NBFLAT",
            base_balance="0.01", dust_limit="0.0001", open_orders_checked=True)
    assert store.release_position_lock_after_flat_check(position_client_order_id="NBFLAT",
        base_balance="0", dust_limit="0.0001", open_orders_checked=True)
    assert store.get_position_lock() is None


def test_rejected_entry_releases_slot_but_ambiguous_entry_does_not(tmp_path):
    from nebula.config import SafetyError
    rejected = FakeClient(error=SafetyError("order gate off"))
    manager = setup(tmp_path, rejected)
    with pytest.raises(SafetyError):
        submit(manager, "NBREJECT")
    assert manager.state.get_position_lock() is None

    ambiguous = FakeClient(error=OkxError("timeout"))
    manager2 = setup(tmp_path, ambiguous)
    with pytest.raises(OkxError):
        submit(manager2, "NBUNKNOWN")
    assert manager2.state.get_position_lock()["owner_client_order_id"] == "NBUNKNOWN"


def test_circuit_breaker_blocks_new_buy_before_exchange_access(tmp_path, monkeypatch):
    from nebula.risk_controls import CircuitBreaker
    client = FakeClient()
    store = StateStore(str(tmp_path / "risk.db"))
    breaker = CircuitBreaker(store, "5")
    # Freeze the UTC trading day so this test is independent of wall-clock date.
    monkeypatch.setattr(CircuitBreaker, "utc_day", staticmethod(lambda now=None: "2026-11-19"))
    breaker.record_closed_trade("NBLOSS1", "-5")
    manager = DemoOrderManager(client, store, circuit_breaker=breaker)
    with pytest.raises(Exception, match="circuit breaker"):
        manager.submit_limit(inst_id="BTC-USDT", side="buy", size="0.01", price="50000",
                             client_order_id="NB_BLOCKED", candle_ts=999)
    assert client.place_calls == 0
    assert store.get_intent("NB_BLOCKED") is None
