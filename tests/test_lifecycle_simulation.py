"""Offline end-to-end lifecycle simulation using a deterministic fake exchange.

No network client, credentials, OKX endpoint, or exchange order is used here.
The scenario exercises the existing order/protection/state managers together.
"""
from decimal import Decimal

import pytest

from nebula.orders import DemoOrderManager, OrderManagerError
from nebula.protection_manager import DemoProtectionManager
from nebula.state import StateStore


ENTRY_ID = "NBENTRY_LIFECYCLE"
ENTRY_ORDER_ID = "10001"
ALGO_ORDER_ID = "90001"
CHILD_ORDER_ID = "20001"


class LifecycleFakeExchange:
    """Deterministic in-memory exchange fixture; every write is recorded locally."""

    def __init__(self):
        self.entry_state = "partially_filled"
        self.entry_fill = "0.006"
        self.child_state = "partially_filled"
        self.child_fill = "0.0045"
        self.write_calls = []
        self.entry_fill_rows = [{
            "instId": "BTC-USDT", "ordId": ENTRY_ORDER_ID, "tradeId": "70001",
            "clOrdId": ENTRY_ID, "side": "buy", "fillSz": "0.01",
            "fillPx": "100", "fee": "-0.001", "feeCcy": "BTC",
            "fillTime": "1000", "billId": "70001", "instType": "SPOT",
            "subType": "1", "tradeQuoteCcy": "USDT", "ts": "1001",
        }]
        self.child_fill_rows = [{
            "instId": "BTC-USDT", "ordId": CHILD_ORDER_ID, "tradeId": "80001",
            "clOrdId": "", "side": "sell", "fillSz": "0.0045",
            "fillPx": "110", "fee": "-0.00495", "feeCcy": "USDT",
            "fillTime": "2000", "billId": "80001", "instType": "SPOT",
            "subType": "2", "tradeQuoteCcy": "USDT", "ts": "2001",
        }]

    def get_spot_instrument(self, inst_id):
        assert inst_id == "BTC-USDT"
        return {
            "instType": "SPOT", "instId": inst_id, "baseCcy": "BTC",
            "quoteCcy": "USDT", "tickSz": "0.1", "lotSz": "0.0001",
            "minSz": "0.0001", "state": "live",
        }

    def place_spot_limit(self, **kwargs):
        self.write_calls.append(("entry", dict(kwargs)))
        return [{"ordId": ENTRY_ORDER_ID, "sCode": "0"}]

    def get_order(self, inst_id, client_order_id):
        assert inst_id == "BTC-USDT"
        assert client_order_id == ENTRY_ID
        return [{
            "ordId": ENTRY_ORDER_ID, "clOrdId": ENTRY_ID,
            "state": self.entry_state, "sz": "0.01",
            "accFillSz": self.entry_fill, "avgPx": "100",
            "fee": "-0.001", "feeCcy": "BTC",
        }]

    def get_pending_orders(self, inst_type="SPOT"):
        assert inst_type == "SPOT"
        return []

    def place_spot_oco_exit(self, **kwargs):
        self.write_calls.append(("oco", dict(kwargs)))
        return [{"algoId": ALGO_ORDER_ID, "sCode": "0"}]

    def get_algo_order(self, algo_client_order_id):
        return [{
            "algoClOrdId": algo_client_order_id, "algoId": ALGO_ORDER_ID,
            "state": "effective", "ordIdList": [CHILD_ORDER_ID],
        }]

    def get_order_by_id(self, inst_id, order_id):
        assert inst_id == "BTC-USDT"
        if order_id == ENTRY_ORDER_ID:
            return [{
                "ordId": ENTRY_ORDER_ID, "clOrdId": ENTRY_ID,
                "state": self.entry_state, "sz": "0.01",
                "accFillSz": self.entry_fill, "avgPx": "100",
                "fee": "-0.001", "feeCcy": "BTC",
            }]
        if order_id == CHILD_ORDER_ID:
            return [{
                "ordId": CHILD_ORDER_ID, "state": self.child_state,
                "sz": "0.009", "accFillSz": self.child_fill, "avgPx": "110",
            }]
        raise AssertionError(f"Unexpected fake order ID: {order_id}")

    def get_fills_history(self, inst_id, order_id, limit=100, after=None):
        assert inst_id == "BTC-USDT"
        if order_id == ENTRY_ORDER_ID:
            return list(self.entry_fill_rows)
        if order_id == CHILD_ORDER_ID:
            return list(self.child_fill_rows)
        raise AssertionError(f"Unexpected fake order ID: {order_id}")

    def get_balance(self):
        # The fake account becomes flat only after the simulated child order fills.
        base = "0" if self.child_state == "filled" else "0.0045"
        return [{"details": [
            {"ccy": "BTC", "availBal": base, "frozenBal": "0", "liab": "0"},
            {"ccy": "USDT", "availBal": "1000", "frozenBal": "0", "liab": "0"},
        ]}]

    def get_pending_algo_orders(self, ord_type="oco"):
        assert ord_type in {"oco", "conditional"}
        return []


def test_full_spot_position_lifecycle_survives_restart_and_releases_only_when_flat(tmp_path):
    db_path = str(tmp_path / "lifecycle.sqlite3")
    exchange = LifecycleFakeExchange()
    state = StateStore(db_path)

    # Entry intent is durable before the fake exchange acknowledges it.
    orders = DemoOrderManager(exchange, state)
    result = orders.submit_limit(
        inst_id="BTC-USDT", side="buy", size="0.01", price="100",
        client_order_id=ENTRY_ID, candle_ts=1_800_000_000_000,
    )
    assert result["status"] == "acknowledged"
    assert state.get_position_lock()["owner_client_order_id"] == ENTRY_ID

    # Repeating the same client ID must not create a second exchange submission.
    with pytest.raises(OrderManagerError, match="not resubmitted"):
        orders.submit_limit(
            inst_id="BTC-USDT", side="buy", size="0.01", price="100",
            client_order_id=ENTRY_ID, candle_ts=1_800_000_000_000,
        )
    assert len([call for call in exchange.write_calls if call[0] == "entry"]) == 1

    # The entry is first observed partially filled; its lock must remain held.
    assert orders.reconcile(ENTRY_ID)["status"] == "partially_filled"
    assert state.get_position_lock()["owner_client_order_id"] == ENTRY_ID

    # The unfilled remainder is simulated as canceled; only the final fill is protected.
    exchange.entry_state = "canceled"
    exchange.entry_fill = "0.01"
    assert orders.reconcile(ENTRY_ID)["status"] == "canceled"

    protection = DemoProtectionManager(exchange, state)
    reserved = protection.prepare(
        entry_client_order_id=ENTRY_ID, take_profit="110", stop_loss="90",
    )
    assert reserved["status"] == "reserved"
    assert reserved["filled_size"] == "0.01"

    active = protection.submit_reserved(ENTRY_ID)
    assert active["status"] == "active"
    assert len([call for call in exchange.write_calls if call[0] == "oco"]) == 1

    # OCO has triggered, but its child is only partially filled: no unlock.
    triggered = protection.reconcile(ENTRY_ID)
    assert triggered["status"] == "triggered"
    assert triggered["accounting"]["accounting_complete"] is False
    assert triggered["position_lock_retained"] is True
    assert state.get_position_lock()["owner_client_order_id"] == ENTRY_ID

    # Simulate process restart while the protective child is still partially filled.
    restarted_state = StateStore(db_path)
    restarted = DemoProtectionManager(exchange, restarted_state)
    assert restarted_state.get_protection_intent(ENTRY_ID)["status"] == "triggered"
    assert restarted_state.get_position_lock()["owner_client_order_id"] == ENTRY_ID

    # The same child completes after restart. Its first fill is deduplicated; the
    # second fill is appended to the durable ledger exactly once.
    exchange.child_state = "filled"
    exchange.child_fill = "0.009"
    exchange.child_fill_rows.append({
        "instId": "BTC-USDT", "ordId": CHILD_ORDER_ID, "tradeId": "80002",
        "clOrdId": "", "side": "sell", "fillSz": "0.0045",
        "fillPx": "110", "fee": "-0.00495", "feeCcy": "USDT",
        "fillTime": "3000", "billId": "80002", "instType": "SPOT",
        "subType": "2", "tradeQuoteCcy": "USDT", "ts": "3001",
    })
    completed = restarted.reconcile(ENTRY_ID)

    assert completed["status"] == "triggered"
    assert completed["child_orders"][0]["state"] == "filled"
    assert completed["fill_count"] == 3
    assert completed["accounting"]["accounting_complete"] is True
    assert completed["accounting"]["is_flat_by_fill_ledger"] is True
    assert Decimal(completed["accounting"]["base_inventory_delta"]) == Decimal("0")
    assert restarted_state.get_position_lock()["owner_client_order_id"] == ENTRY_ID

    # A second independent, fresh account view must also prove flat before unlock.
    finalized = restarted.finalize_position_if_flat(
        ENTRY_ID, accounting=completed["accounting"], dust_limit="0",
    )
    assert finalized["released"] is True
    assert finalized["reason"] == "verified_flat"
    assert restarted_state.get_position_lock() is None

    # Reconciliation is read-only; the only fake writes were one entry and one OCO.
    assert [kind for kind, _ in exchange.write_calls] == ["entry", "oco"]
    events = restarted_state.list_events(limit=100)
    assert any(event["event_type"] == "position_lock_released" for event in events)


def test_partial_child_fill_and_nonflat_account_never_release_position_lock(tmp_path):
    db_path = str(tmp_path / "partial-lifecycle.sqlite3")
    exchange = LifecycleFakeExchange()
    state = StateStore(db_path)
    orders = DemoOrderManager(exchange, state)
    orders.submit_limit(
        inst_id="BTC-USDT", side="buy", size="0.01", price="100",
        client_order_id=ENTRY_ID, candle_ts=1_800_000_000_000,
    )
    exchange.entry_state = "filled"
    exchange.entry_fill = "0.01"
    orders.reconcile(ENTRY_ID)

    manager = DemoProtectionManager(exchange, state)
    manager.prepare(entry_client_order_id=ENTRY_ID, take_profit="110", stop_loss="90")
    manager.submit_reserved(ENTRY_ID)
    result = manager.reconcile(ENTRY_ID)

    assert result["accounting"]["accounting_complete"] is False
    assert result["position_lock_retained"] is True
    assert state.get_position_lock()["owner_client_order_id"] == ENTRY_ID
    assert result["child_orders"][0]["state"] == "partially_filled"
    assert exchange.get_balance()[0]["details"][0]["availBal"] == "0.0045"

    # Even a caller-supplied flat accounting object cannot override a non-flat
    # independent exchange snapshot.
    blocked = manager.finalize_position_if_flat(
        ENTRY_ID,
        accounting={"accounting_complete": True, "is_flat_by_fill_ledger": True,
                    "realized_pnl_quote": "0"},
        dust_limit="0",
    )
    assert blocked["released"] is False
    assert blocked["reason"] == "account_not_flat_or_open_orders_remain"
    assert state.get_position_lock()["owner_client_order_id"] == ENTRY_ID
