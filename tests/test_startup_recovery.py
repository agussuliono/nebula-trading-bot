"""Offline startup recovery tests. No credentials, network, or exchange writes."""
import sqlite3
from types import SimpleNamespace

import pytest

from nebula.protection_manager import DemoProtectionManager
from nebula.startup import StartupRecovery
from nebula.state import StateStore


class CleanAccount:
    settings = SimpleNamespace(demo_account_exclusive=True, max_daily_loss_quote=20)

    def get_balance(self):
        return [{"details": [{"ccy": "USDT", "availBal": "1000", "frozenBal": "0"}]}]

    def get_pending_orders(self, inst_type="SPOT"):
        return []

    def get_pending_algo_orders(self, ord_type="oco"):
        return []


def seed_filled_position(state, entry_id="NBSTARTUPENTRY"):
    state.reserve_entry_intent(entry_id, "BTC-USDT", 123, "buy")
    state.update_intent(entry_id, "filled", "10001")
    state.save_order_snapshot(entry_id, {
        "ordId": "10001", "state": "filled", "sz": "0.01",
        "accFillSz": "0.01", "avgPx": "50000",
        "fee": "-0.001", "feeCcy": "BTC",
    })
    return entry_id


def test_local_database_integrity_passes_for_clean_state(tmp_path):
    state = StateStore(str(tmp_path / "clean.db"))
    report = StartupRecovery(CleanAccount(), state).validate_local_database()
    assert report["ok"] is True
    assert report["position_lock_present"] is False


def test_missing_required_table_blocks_startup(tmp_path):
    path = str(tmp_path / "missing-table.db")
    state = StateStore(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE exit_child_orders")

    report = StartupRecovery(CleanAccount(), state).validate_local_database()
    assert report["ok"] is False
    assert "missing_tables:exit_child_orders" in report["errors"]


def test_position_lock_with_missing_owner_blocks_startup(tmp_path):
    path = str(tmp_path / "orphan-lock.db")
    state = StateStore(path)
    state.reserve_entry_intent("NBLOCKOWNER", "BTC-USDT", 123, "buy")
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE position_lock SET owner_client_order_id='NBMISSING' WHERE singleton=1"
        )

    report = StartupRecovery(CleanAccount(), state).validate_local_database()
    assert report["ok"] is False
    assert "position_lock_owner_mismatch" in report["errors"]


def test_filled_position_without_lock_or_release_event_blocks_startup(tmp_path):
    state = StateStore(str(tmp_path / "lost-lock.db"))
    seed_filled_position(state)
    with state._connect() as db:
        db.execute("DELETE FROM position_lock")

    report = StartupRecovery(CleanAccount(), state).validate_local_database()
    assert report["ok"] is False
    assert any(x.startswith("filled_entry_without_lock_or_release:") for x in report["errors"])


def test_clean_empty_account_is_reported_but_never_authorizes_entries(tmp_path):
    state = StateStore(str(tmp_path / "empty.db"))
    result = StartupRecovery(CleanAccount(), state).run()

    assert result["status"] == "clean_no_local_position"
    assert result["entry_authorized"] is False
    assert result["exchange_writes"] == 0
    assert result["blockers"] == []


def test_untracked_exchange_order_blocks_empty_local_state(tmp_path):
    class OrphanOrderAccount(CleanAccount):
        def get_pending_orders(self, inst_type="SPOT"):
            return [{"instId": "ETH-USDT", "ordId": "20001"}]

    state = StateStore(str(tmp_path / "orphan-order.db"))
    result = StartupRecovery(OrphanOrderAccount(), state).run()

    assert result["status"] == "blocked_account_mismatch"
    assert "untracked_open_exchange_orders" in result["blockers"]
    assert result["entry_authorized"] is False


def test_untracked_non_quote_balance_blocks_empty_local_state(tmp_path):
    class OrphanBalanceAccount(CleanAccount):
        def get_balance(self):
            return [{"details": [
                {"ccy": "USDT", "availBal": "1000", "frozenBal": "0"},
                {"ccy": "ETH", "availBal": "0.1", "frozenBal": "0"},
            ]}]

    state = StateStore(str(tmp_path / "orphan-balance.db"))
    result = StartupRecovery(OrphanBalanceAccount(), state).run()

    assert result["status"] == "blocked_account_mismatch"
    assert "untracked_non_quote_asset_balances" in result["blockers"]


def test_filled_position_without_protection_intent_remains_blocked(tmp_path):
    class PositionAccount(CleanAccount):
        def get_fills_history(self, instrument, order_id, limit=100, after=None):
            return [{
                "instId": instrument, "ordId": order_id, "tradeId": "70001",
                "clOrdId": "NBSTARTUPENTRY", "side": "buy", "fillSz": "0.01",
                "fillPx": "50000", "fee": "-0.001", "feeCcy": "BTC",
                "fillTime": "1000",
            }]

    state = StateStore(str(tmp_path / "missing-protection.db"))
    seed_filled_position(state)
    result = StartupRecovery(PositionAccount(), state).run()

    assert result["status"] == "blocked_reconciliation_failure"
    assert result["entry_authorized"] is False
    assert state.get_position_lock()["owner_client_order_id"] == "NBSTARTUPENTRY"


def test_active_protection_must_match_fresh_exchange_snapshot(tmp_path):
    class ProtectedAccount(CleanAccount):
        def get_fills_history(self, instrument, order_id, limit=100, after=None):
            return [{
                "instId": instrument, "ordId": order_id, "tradeId": "70001",
                "clOrdId": "NBSTARTUPENTRY", "side": "buy", "fillSz": "0.01",
                "fillPx": "50000", "fee": "-0.001", "feeCcy": "BTC",
                "fillTime": "1000",
            }]

        def get_algo_order(self, algo_client_order_id):
            return [{
                "algoClOrdId": algo_client_order_id, "algoId": "90001",
                "state": "live", "ordIdList": [],
            }]

        def get_balance(self):
            return [{"details": [
                {"ccy": "BTC", "availBal": "0.009", "frozenBal": "0"},
                {"ccy": "USDT", "availBal": "500", "frozenBal": "0"},
            ]}]

        def get_pending_algo_orders(self, ord_type="oco"):
            if ord_type != "oco":
                return []
            return [{
                "instId": "BTC-USDT", "algoClOrdId": "NBPSTARTUP",
                "algoId": "90001", "state": "live",
            }]

    state = StateStore(str(tmp_path / "protected.db"))
    seed_filled_position(state)
    state.reserve_protection_intent(
        entry_client_order_id="NBSTARTUPENTRY",
        instrument="BTC-USDT",
        algo_client_order_id="NBPSTARTUP",
        filled_size="0.009",
        take_profit="52000",
        stop_loss="48000",
    )
    state.update_protection_intent("NBSTARTUPENTRY", "submission_unknown")

    result = StartupRecovery(ProtectedAccount(), state).run()

    assert result["status"] == "position_reconciled_protection_active"
    assert result["entry_authorized"] is False
    assert result["exchange_writes"] == 0
    assert state.get_position_lock()["owner_client_order_id"] == "NBSTARTUPENTRY"


def test_account_read_failure_fails_closed_and_retains_position_lock(tmp_path):
    class FailingAccount(CleanAccount):
        def get_balance(self):
            raise TimeoutError("simulated account timeout")

    state = StateStore(str(tmp_path / "read-failure.db"))
    result = StartupRecovery(FailingAccount(), state).run()

    assert result["status"] == "blocked_reconciliation_failure"
    assert result["entry_authorized"] is False
    assert result["exchange_writes"] == 0
