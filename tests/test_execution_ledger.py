from decimal import Decimal
import pytest
from nebula.accounting import round_trip_accounting
from nebula.protection_manager import DemoProtectionManager, ProtectionManagerError
from nebula.state import StateStore


def seed(tmp_path):
    store = StateStore(str(tmp_path / "ledger.db"))
    store.reserve_entry_intent("NBENTRY1", "BTC-USDT", 1, "buy")
    store.update_intent("NBENTRY1", "submitting")
    store.update_intent("NBENTRY1", "acknowledged", "100")
    store.update_intent("NBENTRY1", "filled", "100")
    store.save_order_snapshot("NBENTRY1", {"ordId":"100", "state":"filled", "sz":"0.01",
        "accFillSz":"0.01", "avgPx":"100", "fee":"-0.001", "feeCcy":"BTC"})
    store.reserve_protection_intent(entry_client_order_id="NBENTRY1", instrument="BTC-USDT",
        algo_client_order_id="NBPENTRY1", filled_size="0.01", take_profit="110", stop_loss="90")
    store.update_protection_intent("NBENTRY1", "triggered", exchange_algo_id="900")
    return store


def test_fill_ledger_is_idempotent_and_preserves_fee_sign(tmp_path):
    store = seed(tmp_path)
    fill = {"instId":"BTC-USDT", "ordId":"100", "tradeId":"1", "clOrdId":"NBENTRY1",
        "side":"buy", "fillSz":"0.01", "fillPx":"100", "fee":"-0.001",
        "feeCcy":"BTC", "fillTime":"123"}
    assert store.save_fill(fill) is True
    assert store.save_fill(fill) is False
    rows = store.list_fills("BTC-USDT", ["100"])
    assert len(rows) == 1
    assert rows[0]["fee"] == "-0.001"


def test_conflicting_duplicate_trade_id_fails_closed(tmp_path):
    store = seed(tmp_path)
    base = {"instId":"BTC-USDT", "ordId":"100", "tradeId":"1", "side":"buy",
        "fillSz":"0.01", "fillPx":"100", "fee":"-0.001", "feeCcy":"BTC"}
    store.save_fill(base)
    changed = dict(base, fillPx="101")
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        store.save_fill(changed)


def test_accounting_converts_base_and_quote_fees_without_losing_sign():
    fills = [
        {"side":"buy", "fill_size":"1", "fill_price":"100", "fee":"-0.001", "fee_currency":"BTC"},
        {"side":"sell", "fill_size":"0.999", "fill_price":"110", "fee":"-0.11", "fee_currency":"USDT"},
    ]
    result = round_trip_accounting(fills, instrument="BTC-USDT", dust_limit="0.0001")
    assert result["is_flat_by_fill_ledger"] is True
    assert Decimal(result["realized_pnl_quote"]) == Decimal("9.78")
    assert result["accounting_complete"] is True


def test_accounting_does_not_claim_realized_pnl_if_inventory_open_or_fee_unknown():
    open_position = round_trip_accounting([
        {"side":"buy", "fill_size":"1", "fill_price":"100", "fee":"-0.1", "fee_currency":"USDT"}
    ], instrument="BTC-USDT")
    assert open_position["realized_pnl_quote"] is None
    unknown_fee = round_trip_accounting([
        {"side":"buy", "fill_size":"1", "fill_price":"100", "fee":"-0.1", "fee_currency":"BNB"},
        {"side":"sell", "fill_size":"1", "fill_price":"110", "fee":"0", "fee_currency":"USDT"},
    ], instrument="BTC-USDT")
    assert unknown_fee["realized_pnl_quote"] is None
    assert not unknown_fee["accounting_complete"]


def test_triggered_oco_reconciles_children_and_fills_without_releasing_lock(tmp_path):
    class FakeClient:
        def get_order_by_id(self, inst_id, order_id):
            return [{"ordId":order_id, "state":"filled", "sz":"0.009", "accFillSz":"0.009", "avgPx":"110"}]
        def get_fills_history(self, inst_id, order_id, limit=100, after=None):
            if order_id == "100":
                return [{"instId":inst_id,"ordId":"100","tradeId":"1","clOrdId":"NBENTRY1",
                    "side":"buy","fillSz":"0.01","fillPx":"100","fee":"-0.001","feeCcy":"BTC","fillTime":"1000",
                    "billId":"1001","instType":"SPOT","subType":"1","tradeQuoteCcy":"USDT","ts":"1001"}]
            return [{"instId":inst_id,"ordId":order_id,"tradeId":"2","clOrdId":"",
                "side":"sell","fillSz":"0.009","fillPx":"110","fee":"-0.09","feeCcy":"USDT","fillTime":"2000",
                "billId":"1002","instType":"SPOT","subType":"2","tradeQuoteCcy":"USDT","ts":"2001"}]
    store = seed(tmp_path)
    result = DemoProtectionManager(FakeClient(), store).reconcile_exit_children("NBENTRY1", child_order_ids=["200"])
    assert result["child_orders"][0]["state"] == "filled"
    assert result["fill_count"] == 2
    assert Decimal(result["accounting"]["realized_pnl_quote"]) == Decimal("-0.10")
    assert result["position_lock_retained"] is True
    assert store.get_position_lock()["owner_client_order_id"] == "NBENTRY1"


def test_child_order_ids_are_persisted_and_cannot_regress(tmp_path):
    store = seed(tmp_path)
    assert len(store.save_exit_child_orders("NBENTRY1", "BTC-USDT", ["200", "201", "200"])) == 2
    store.update_exit_child_order("NBENTRY1", "200", {"sz":"1", "accFillSz":"0.5", "avgPx":"100", "state":"partially_filled"})
    with pytest.raises(ValueError, match="regressed"):
        store.update_exit_child_order("NBENTRY1", "200", {"sz":"1", "accFillSz":"0.4", "avgPx":"100", "state":"partially_filled"})


def test_finalize_requires_both_flat_ledger_and_fresh_flat_account(tmp_path):
    class FlatClient:
        def get_balance(self):
            return [{"details":[{"ccy":"BTC","availBal":"0","frozenBal":"0", "liab": "0"},
                                {"ccy":"USDT","availBal":"99","frozenBal":"0", "liab": "0"}]}]
        def get_pending_orders(self, inst_type="SPOT"): return []
        def get_pending_algo_orders(self, ord_type="oco"): return []
    store = seed(tmp_path)
    store.save_exit_child_orders("NBENTRY1", "BTC-USDT", ["200"])
    store.update_exit_child_order("NBENTRY1", "200", {"sz":"0.009", "accFillSz":"0.009", "avgPx":"110", "state":"filled"})
    manager = DemoProtectionManager(FlatClient(), store)
    incomplete = {"accounting_complete":False,"is_flat_by_fill_ledger":False}
    assert manager.finalize_position_if_flat("NBENTRY1", accounting=incomplete)["released"] is False
    complete = {"accounting_complete":True,"is_flat_by_fill_ledger":True}
    result = manager.finalize_position_if_flat("NBENTRY1", accounting=complete)
    assert result["released"] is True
    assert store.get_position_lock() is None


def test_finalize_keeps_lock_when_exchange_reports_residual_or_open_order(tmp_path):
    class NotFlatClient:
        def get_balance(self):
            return [{"details":[{"ccy":"BTC","availBal":"0.001","frozenBal":"0", "liab": "0"}]}]
        def get_pending_orders(self, inst_type="SPOT"): return [{"instId":"BTC-USDT","ordId":"777"}]
        def get_pending_algo_orders(self, ord_type="oco"): return []
    store = seed(tmp_path)
    store.save_exit_child_orders("NBENTRY1", "BTC-USDT", ["200"])
    store.update_exit_child_order("NBENTRY1", "200", {"sz":"0.009", "accFillSz":"0.009", "avgPx":"110", "state":"filled"})
    manager = DemoProtectionManager(NotFlatClient(), store)
    result = manager.finalize_position_if_flat("NBENTRY1", accounting={"accounting_complete":True,"is_flat_by_fill_ledger":True})
    assert result["released"] is False
    assert store.get_position_lock()["owner_client_order_id"] == "NBENTRY1"


def test_finalize_keeps_lock_when_balance_snapshot_is_invalid(tmp_path):
    class InvalidSnapshotClient:
        def get_balance(self):
            return [{"details":[{"ccy":"BTC","availBal":False,"frozenBal":"0","liab":"0"},
                                {"ccy":"USDT","availBal":"99","frozenBal":"0","liab":"0"}]}]
        def get_pending_orders(self, inst_type="SPOT"): return []
        def get_pending_algo_orders(self, ord_type="oco"): return []
    store = seed(tmp_path)
    store.save_exit_child_orders("NBENTRY1", "BTC-USDT", ["200"])
    store.update_exit_child_order("NBENTRY1", "200", {"sz":"0.009", "accFillSz":"0.009", "avgPx":"110", "state":"filled"})
    manager = DemoProtectionManager(InvalidSnapshotClient(), store)
    with pytest.raises(ProtectionManagerError, match="position lock remains held"):
        manager.finalize_position_if_flat("NBENTRY1", accounting={"accounting_complete":True,"is_flat_by_fill_ledger":True})
    assert store.get_position_lock()["owner_client_order_id"] == "NBENTRY1"


def test_finalize_keeps_lock_when_currency_code_is_not_canonical(tmp_path):
    class NoncanonicalClient:
        def get_balance(self):
            return [{"details":[{"ccy":"btc","availBal":"0.5","frozenBal":"0","liab":"0"},
                                {"ccy":"USDT","availBal":"99","frozenBal":"0","liab":"0"}]}]
        def get_pending_orders(self, inst_type="SPOT"): return []
        def get_pending_algo_orders(self, ord_type="oco"): return []
    store = seed(tmp_path)
    store.save_exit_child_orders("NBENTRY1", "BTC-USDT", ["200"])
    store.update_exit_child_order("NBENTRY1", "200", {"sz":"0.009", "accFillSz":"0.009", "avgPx":"110", "state":"filled"})
    manager = DemoProtectionManager(NoncanonicalClient(), store)
    with pytest.raises(ProtectionManagerError, match="position lock remains held"):
        manager.finalize_position_if_flat("NBENTRY1", accounting={"accounting_complete":True,"is_flat_by_fill_ledger":True})
    assert store.get_position_lock()["owner_client_order_id"] == "NBENTRY1"


def test_reconcile_triggered_algo_discovers_child_order_ids(tmp_path):
    class Client:
        def get_algo_order(self, algo_client_order_id):
            return [{"algoClOrdId":algo_client_order_id,"algoId":"900","state":"effective","ordIdList":["200"]}]
        def get_order_by_id(self, inst_id, order_id):
            return [{"ordId":order_id,"state":"filled","sz":"0.009","accFillSz":"0.009","avgPx":"110"}]
        def get_fills_history(self, inst_id, order_id, limit=100, after=None):
            if order_id == "100":
                return [{"instId":inst_id,"ordId":"100","tradeId":"1","clOrdId":"NBENTRY1","side":"buy",
                    "fillSz":"0.01","fillPx":"100","fee":"-0.001","feeCcy":"BTC","fillTime":"1000",
                    "billId":"1001","instType":"SPOT","subType":"1","tradeQuoteCcy":"USDT","ts":"1001"}]
            return [{"instId":inst_id,"ordId":"200","tradeId":"2","side":"sell",
                "fillSz":"0.009","fillPx":"110","fee":"-0.09","feeCcy":"USDT","fillTime":"2000",
                "billId":"1002","instType":"SPOT","subType":"2","tradeQuoteCcy":"USDT","ts":"2001"}]
    store = seed(tmp_path)
    result = DemoProtectionManager(Client(), store).reconcile("NBENTRY1")
    assert result["status"] == "triggered"
    assert result["child_orders"][0]["exchange_order_id"] == "200"
    assert result["accounting"]["accounting_complete"] is True


def test_fill_history_pagination_is_bounded_and_deduplicated(tmp_path):
    class PagedClient:
        def __init__(self): self.calls=[]
        def get_fills_history(self, instrument, order_id, limit=100, after=None):
            self.calls.append(after)
            if after is None:
                ids = range(1099, 999, -1)
                offset = 0
            else:
                ids = [999]
                offset = 100
            return [{"instId":instrument,"ordId":order_id,"tradeId":str(offset+j+1),"side":"buy",
                "fillSz":"0.0001","fillPx":"100","fee":"0","feeCcy":"USDT",
                "billId":str(bill_id),"fillTime":str(offset+j+1),
                "instType":"SPOT","subType":"1","tradeQuoteCcy":"USDT","ts":str(offset+j+2)}
                for j,bill_id in enumerate(ids)]
    store = seed(tmp_path)
    client = PagedClient()
    manager = DemoProtectionManager(client, store)
    inserted = manager._ingest_order_fills("BTC-USDT", "100")
    assert inserted == 101
    assert client.calls == [None, "1000"]
    assert len(store.list_fills("BTC-USDT", ["100"])) == 101


def test_dust_tolerance_does_not_turn_residual_inventory_into_realized_pnl():
    result = round_trip_accounting([
        {"side":"buy", "fill_size":"1", "fill_price":"100", "fee":"0", "fee_currency":"USDT"},
        {"side":"sell", "fill_size":"0.9999", "fill_price":"110", "fee":"0", "fee_currency":"USDT"},
    ], instrument="BTC-USDT", dust_limit="0.0002")
    assert result["is_within_dust_by_fill_ledger"] is True
    assert result["is_flat_by_fill_ledger"] is False
    assert result["realized_pnl_quote"] is None
    assert result["accounting_complete"] is False


def test_unresolved_oco_child_blocks_lock_release_even_if_balance_appears_flat(tmp_path):
    class FlatClient:
        def get_balance(self): return [{"details":[{"ccy":"BTC","availBal":"0","frozenBal":"0", "liab": "0"}]}]
        def get_pending_orders(self, inst_type="SPOT"): return []
        def get_pending_algo_orders(self, ord_type="oco"): return []
    store = seed(tmp_path)
    store.save_exit_child_orders("NBENTRY1", "BTC-USDT", ["200"])
    manager = DemoProtectionManager(FlatClient(), store)
    with pytest.raises(ProtectionManagerError, match="Local execution state"):
        manager.finalize_position_if_flat("NBENTRY1", accounting={"accounting_complete":True,"is_flat_by_fill_ledger":True})
    assert store.get_position_lock()["owner_client_order_id"] == "NBENTRY1"


def test_fill_history_overflow_fails_closed_instead_of_using_partial_ledger(tmp_path):
    class EndlessClient:
        def get_fills_history(self, instrument, order_id, limit=100, after=None):
            page = 0 if after is None else 1
            start = page * limit
            return [{"instId":instrument,"ordId":order_id,"tradeId":str(i+1),"side":"buy",
                "fillSz":"0.0001","fillPx":"100","fee":"0","feeCcy":"USDT",
                "billId":str(5100-i),"fillTime":str(i),"instType":"SPOT",
                "subType":"1","tradeQuoteCcy":"USDT","ts":str(i+1)}
                for i in range(start,start+limit)]
    store = seed(tmp_path)
    manager = DemoProtectionManager(EndlessClient(), store)
    with pytest.raises(ProtectionManagerError, match="pagination limit"):
        manager._ingest_order_fills("BTC-USDT", "100", max_pages=1)
    assert len(store.list_fills("BTC-USDT", ["100"])) == 100  # second page is rejected before ingestion


def test_verified_flat_finalization_records_realized_pnl_once(tmp_path):
    from nebula.risk_controls import CircuitBreaker
    class FlatClient:
        def get_balance(self): return [{"details":[{"ccy":"BTC","availBal":"0","frozenBal":"0", "liab": "0"}]}]
        def get_pending_orders(self, inst_type="SPOT"): return []
        def get_pending_algo_orders(self, ord_type="oco"): return []
    store = seed(tmp_path)
    store.save_exit_child_orders("NBENTRY1", "BTC-USDT", ["200"])
    store.update_exit_child_order("NBENTRY1", "200", {"sz":"0.01", "accFillSz":"0.01", "avgPx":"110", "state":"filled"})
    breaker = CircuitBreaker(store, "20")
    manager = DemoProtectionManager(FlatClient(), store, circuit_breaker=breaker)
    accounting = {"accounting_complete":True, "is_flat_by_fill_ledger":True,
                  "realized_pnl_quote":"9.9"}
    result = manager.finalize_position_if_flat("NBENTRY1", accounting=accounting)
    assert result["released"] is True
    assert result["daily_risk"]["daily_realized_pnl_quote"] == "9.9"
    # Replayed finalization does not double count the same position event.
    breaker.record_closed_trade("NBENTRY1", "9.9")
    assert breaker.status()["daily_realized_pnl_quote"] == "9.9"
