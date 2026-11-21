"""Fault-injection tests for ambiguous exchange failures and recovery.

All clients below are deterministic in-memory fakes. No network or OKX endpoint
is called, and every scenario verifies fail-closed position-lock behavior.
"""
import pytest

from nebula.exchange import OkxError
from nebula.orders import DemoOrderManager, OrderManagerError
from nebula.protection_manager import DemoProtectionManager, ProtectionManagerError
from nebula.state import StateStore


ENTRY_ID = "NBFAULT_ENTRY"


class FaultyOrderClient:
    def __init__(self):
        self.place_calls = 0
        self.place_error = None
        self.order_error = None
        self.pending_error = None
        self.order = None
        self.pending = []

    def get_spot_instrument(self, inst_id):
        return {
            "instType": "SPOT", "instId": inst_id, "baseCcy": "BTC",
            "quoteCcy": "USDT", "tickSz": "0.1", "lotSz": "0.001",
            "minSz": "0.001", "state": "live",
        }

    def place_spot_limit(self, **kwargs):
        self.place_calls += 1
        if self.place_error:
            raise self.place_error
        return [{"ordId": "41001"}]

    def get_order(self, inst_id, client_order_id):
        if self.order_error:
            raise self.order_error
        return [self.order] if self.order else []

    def get_pending_orders(self, inst_type="SPOT"):
        if self.pending_error:
            raise self.pending_error
        return list(self.pending)


class CancelRaceClient:
    def __init__(self, final_order):
        self.final_order = final_order
        self.cancel_calls = 0
        self.lookup_calls = 0

    def cancel_order(self, **kwargs):
        self.cancel_calls += 1
        # The exchange may have accepted the cancel while the client timed out.
        raise TimeoutError("cancel response lost")

    def get_order(self, inst_id, client_order_id):
        self.lookup_calls += 1
        return [dict(self.final_order)]


class ChildReadFailureClient:
    def get_order_by_id(self, inst_id, order_id):
        raise TimeoutError("temporary order-detail read failure")


class AccountReadFailureClient:
    def get_balance(self):
        raise TimeoutError("temporary account snapshot failure")

    def get_pending_orders(self, inst_type="SPOT"):
        return []

    def get_pending_algo_orders(self, ord_type="oco"):
        return []


def reserve_partial_entry(store):
    store.reserve_entry_intent(ENTRY_ID, "BTC-USDT", 123, "buy")
    store.update_intent(ENTRY_ID, "partially_filled", "41001")
    store.save_order_snapshot(ENTRY_ID, {
        "ordId": "41001", "state": "partially_filled", "sz": "0.010",
        "accFillSz": "0.004", "avgPx": "50000", "fee": "-0.01",
        "feeCcy": "USDT",
    })


def test_ambiguous_entry_and_failed_lookups_keep_lock_and_prevent_retry(tmp_path):
    client = FaultyOrderClient()
    client.place_error = OkxError("OKX request timed out; outcome may be unknown")
    store = StateStore(str(tmp_path / "ambiguous-entry.db"))
    manager = DemoOrderManager(client, store)

    with pytest.raises(OkxError):
        manager.submit_limit(
            inst_id="BTC-USDT", side="buy", size="0.01", price="50000",
            client_order_id=ENTRY_ID, candle_ts=123,
        )
    assert store.get_intent(ENTRY_ID)["status"] == "submission_unknown"
    assert store.get_position_lock()["owner_client_order_id"] == ENTRY_ID

    client.order_error = TimeoutError("order detail endpoint unavailable")
    client.pending_error = TimeoutError("pending orders endpoint unavailable")
    with pytest.raises(OrderManagerError, match="reconciliation failed"):
        manager.reconcile(ENTRY_ID)

    with pytest.raises(OrderManagerError, match="not resubmitted"):
        manager.submit_limit(
            inst_id="BTC-USDT", side="buy", size="0.01", price="50000",
            client_order_id=ENTRY_ID, candle_ts=123,
        )
    assert client.place_calls == 1
    assert store.get_position_lock()["owner_client_order_id"] == ENTRY_ID


def test_stale_lower_cumulative_fill_is_rejected_without_overwriting_snapshot(tmp_path):
    client = FaultyOrderClient()
    store = StateStore(str(tmp_path / "fill-regression.db"))
    manager = DemoOrderManager(client, store)
    manager.submit_limit(
        inst_id="BTC-USDT", side="buy", size="0.01", price="50000",
        client_order_id=ENTRY_ID, candle_ts=123,
    )

    client.order = {
        "ordId": "41001", "clOrdId": ENTRY_ID, "state": "partially_filled",
        "sz": "0.010", "accFillSz": "0.004", "avgPx": "50000",
    }
    assert manager.reconcile(ENTRY_ID)["status"] == "partially_filled"

    # A delayed/stale exchange response must not roll back cumulative execution.
    client.order["accFillSz"] = "0.003"
    with pytest.raises(OrderManagerError, match="snapshot failed validation"):
        manager.reconcile(ENTRY_ID)

    snapshot = store.get_order_snapshot(ENTRY_ID)
    assert snapshot["cumulative_fill_size"] == "0.004"
    assert store.get_intent(ENTRY_ID)["status"] == "partially_filled"
    assert store.get_position_lock()["owner_client_order_id"] == ENTRY_ID


def test_cancel_timeout_racing_with_fill_uses_final_exchange_snapshot(tmp_path):
    store = StateStore(str(tmp_path / "cancel-race.db"))
    reserve_partial_entry(store)
    final_order = {
        "ordId": "41001", "clOrdId": ENTRY_ID, "state": "filled",
        "sz": "0.010", "accFillSz": "0.007", "avgPx": "50100",
        "fee": "-0.02", "feeCcy": "USDT",
    }
    client = CancelRaceClient(final_order)
    manager = DemoProtectionManager(client, store)

    result = manager.cancel_entry_remainder_and_reconcile(ENTRY_ID)

    assert result == {"status": "filled", "filled_size": "0.007"}
    assert client.cancel_calls == 1
    assert client.lookup_calls == 1
    assert store.get_order_snapshot(ENTRY_ID)["cumulative_fill_size"] == "0.007"
    assert store.get_intent(ENTRY_ID)["status"] == "filled"
    # A positive fill remains a position; the position lock must not be released.
    assert store.get_position_lock()["owner_client_order_id"] == ENTRY_ID


def test_cancel_timeout_plus_lookup_failure_preserves_partial_state_and_lock(tmp_path):
    store = StateStore(str(tmp_path / "cancel-ambiguous.db"))
    reserve_partial_entry(store)

    class Client:
        def cancel_order(self, **kwargs):
            raise TimeoutError("cancel response lost")

        def get_order(self, inst_id, client_order_id):
            raise TimeoutError("follow-up order lookup unavailable")

    manager = DemoProtectionManager(Client(), store)
    with pytest.raises(ProtectionManagerError, match="not confirmed"):
        manager.cancel_entry_remainder_and_reconcile(ENTRY_ID)

    assert store.get_order_snapshot(ENTRY_ID)["cumulative_fill_size"] == "0.004"
    assert store.get_position_lock()["owner_client_order_id"] == ENTRY_ID
    assert store.list_events(limit=10)[0]["event_type"] == "entry_cancel_or_lookup_ambiguous"


def test_child_order_read_timeout_does_not_release_triggered_position(tmp_path):
    store = StateStore(str(tmp_path / "child-read.db"))
    reserve_partial_entry(store)
    store.reserve_protection_intent(
        entry_client_order_id=ENTRY_ID, instrument="BTC-USDT",
        algo_client_order_id="NBPFAULT", filled_size="0.004",
        take_profit="52000", stop_loss="48000",
    )
    store.update_protection_intent(ENTRY_ID, "triggered", exchange_algo_id="99881")
    store.save_exit_child_orders(ENTRY_ID, "BTC-USDT", ["51001"])

    manager = DemoProtectionManager(ChildReadFailureClient(), store)
    with pytest.raises(ProtectionManagerError, match="reconciliation incomplete"):
        manager.reconcile_exit_children(ENTRY_ID)

    assert store.get_protection_intent(ENTRY_ID)["status"] == "triggered"
    assert store.get_position_lock()["owner_client_order_id"] == ENTRY_ID
    assert store.list_exit_child_orders(ENTRY_ID)[0]["state"] == "unknown"


def test_fresh_account_read_failure_blocks_final_unlock(tmp_path):
    store = StateStore(str(tmp_path / "flat-read.db"))
    reserve_partial_entry(store)
    store.reserve_protection_intent(
        entry_client_order_id=ENTRY_ID, instrument="BTC-USDT",
        algo_client_order_id="NBPFAULT", filled_size="0.004",
        take_profit="52000", stop_loss="48000",
    )
    store.update_protection_intent(ENTRY_ID, "triggered", exchange_algo_id="99881")

    manager = DemoProtectionManager(AccountReadFailureClient(), store)
    accounting = {
        "accounting_complete": True,
        "is_flat_by_fill_ledger": True,
        "realized_pnl_quote": "0",
    }
    with pytest.raises(ProtectionManagerError, match="Fresh account reconciliation failed"):
        manager.finalize_position_if_flat(ENTRY_ID, accounting=accounting, dust_limit="0")

    assert store.get_position_lock()["owner_client_order_id"] == ENTRY_ID
    assert store.get_protection_intent(ENTRY_ID)["status"] == "triggered"


def _fill_row(bill_id, trade_id, *, inst_id="BTC-USDT", order_id="41001",
              inst_type="SPOT", side="buy", subtype="1"):
    return {
        "billId": str(bill_id), "tradeId": str(trade_id), "instId": inst_id,
        "ordId": order_id, "instType": inst_type, "side": side,
        "subType": subtype, "tradeQuoteCcy": "USDT",
        "fillSz": "0.001", "fillPx": "50000", "fee": "-0.000001",
        "feeCcy": "BTC", "fillTime": "1795132800000", "ts": "1795132800001",
    }


class FillHistoryClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_fills_history(self, instrument, order_id, *, limit, after):
        self.calls.append((instrument, order_id, limit, after))
        if not self.pages:
            raise AssertionError("unexpected extra fill-history page request")
        return self.pages.pop(0)


def test_fill_history_pagination_validates_all_pages_and_uses_oldest_bill_cursor(tmp_path):
    store = StateStore(str(tmp_path / "fill-pages.db"))
    client = FillHistoryClient([
        [_fill_row(104, 14), _fill_row(103, 13)],
        [_fill_row(102, 12), _fill_row(101, 11)],
        [_fill_row(100, 10)],
    ])
    manager = DemoProtectionManager(client, store)

    inserted = manager._ingest_order_fills("BTC-USDT", "41001", page_limit=2, max_pages=3)

    assert inserted == 5
    assert len(store.list_fills("BTC-USDT", ["41001"])) == 5
    assert [call[3] for call in client.calls] == [None, "103", "101"]


def test_fill_history_pagination_rejects_repeated_or_non_older_cursor(tmp_path):
    store = StateStore(str(tmp_path / "fill-cursor.db"))
    client = FillHistoryClient([
        [_fill_row(104, 14), _fill_row(103, 13)],
        [_fill_row(105, 12)],
    ])
    manager = DemoProtectionManager(client, store)

    with pytest.raises(ProtectionManagerError, match="strictly toward older"):
        manager._ingest_order_fills("BTC-USDT", "41001", page_limit=2, max_pages=3)

    assert len(store.list_fills("BTC-USDT", ["41001"])) == 2


@pytest.mark.parametrize("row", [
    _fill_row(103, 13, order_id="99999"),
    _fill_row(103, 13, inst_type="MARGIN"),
    _fill_row(103, 13, inst_id="ETH-USDT"),
    _fill_row(103, 13, side="sell", subtype="1"),
    {**_fill_row(103, 13), "billId": ""},
    {**_fill_row(103, 13), "fillTime": ""},
    {**_fill_row(103, 13), "fee": "NaN"},
])
def test_fill_history_rejects_mismatched_or_malformed_records_before_page_insert(tmp_path, row):
    store = StateStore(str(tmp_path / "fill-invalid.db"))
    manager = DemoProtectionManager(FillHistoryClient([[row]]), store)

    with pytest.raises(ProtectionManagerError):
        manager._ingest_order_fills("BTC-USDT", "41001", page_limit=2, max_pages=3)

    assert store.list_fills("BTC-USDT", ["41001"]) == []


def test_fill_history_rejects_non_list_page_and_invalid_pagination_bounds(tmp_path):
    store = StateStore(str(tmp_path / "fill-shape.db"))
    manager = DemoProtectionManager(FillHistoryClient([None]), store)

    with pytest.raises(ProtectionManagerError, match="malformed"):
        manager._ingest_order_fills("BTC-USDT", "41001", page_limit=2, max_pages=3)
    with pytest.raises(ProtectionManagerError, match="bounds"):
        manager._ingest_order_fills("BTC-USDT", "41001", page_limit=0, max_pages=3)
