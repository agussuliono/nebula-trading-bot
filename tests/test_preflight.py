from decimal import Decimal

from nebula.config import Settings
from nebula.entry_plan import EntryPlanPolicy
from nebula.preflight import _redact_financials, run_read_only_preflight
from nebula.state import StateStore


NOW_MS = 1_800_000_000_000
CANDLE_TS = NOW_MS - 3_600_000


class FakeRuntime:
    analysis = {"signals": []}

    def __init__(self, client, state_store, *, policy):
        self.client = client
        self.state = state_store
        self.policy = policy

    def run_once(self):
        return self.analysis


class ReadOnlySnapshotClient:
    def __init__(self):
        self.calls = []

    def get_spot_instrument(self, inst_id):
        self.calls.append("instrument_rules")
        return {
            "instType": "SPOT", "instId": inst_id, "baseCcy": "AAA",
            "quoteCcy": "USDT", "tickSz": "0.1", "lotSz": "0.001",
            "minSz": "0.001", "state": "live",
        }

    def get_account_config(self):
        self.calls.append("account_config")
        return [{
            "acctLv": "1", "autoLoan": False, "enableSpotBorrow": False,
            "spotBorrowAutoRepay": False,
            "perm": "read_only,trade", "uid": "test-demo-account-uid",
        }]

    def get_balance(self):
        self.calls.append("balance")
        return [{
            "uTime": str(NOW_MS),
            "details": [{
                "ccy": "USDT", "eq": "1000",
                "availBal": "1000", "frozenBal": "0", "liab": "0",
            }],
        }]

    def get_tickers(self):
        self.calls.append("tickers")
        return [{
            "instId": "AAA-USDT", "bidPx": "99.95", "askPx": "100",
            "bidSz": "100", "askSz": "100", "ts": str(NOW_MS),
        }]

    def get_account_bills(self, *, begin=None, end=None, limit=100):
        self.calls.append("account_bills")
        from nebula.bill_reconciliation import _create_complete_account_bills_snapshot
        return _create_complete_account_bills_snapshot(
            [], begin_ms=int(begin), end_ms=int(end),
            page_count=1, terminal_page_seen=True,
        )

    def get_pending_orders(self, inst_type):
        self.calls.append("pending_regular")
        assert inst_type == "SPOT"
        return []

    def get_pending_algo_orders(self, ord_type):
        self.calls.append("pending_" + ord_type)
        return []


def settings_read_only():
    return Settings(
        api_key="demo-key", secret_key="demo-secret", passphrase="demo-pass",
        demo_account_exclusive=True, enable_demo_orders=False, kill_switch=True,
        max_daily_loss_quote=20, max_risk_quote=2, max_position_quote=100,
    )


def plan():
    return {
        "instrument": "AAA-USDT",
        "signal_action": "buy",
        "candle_closed": True,
        "candle_ts": CANDLE_TS,
        "entry_price": "100",
        "size": "0.1",
        "stop_loss": "90",
        "take_profit": "120",
        "orders_submitted": 0,
    }


def policy():
    return EntryPlanPolicy(
        risk_budget_quote=Decimal("10"),
        max_notional_quote=Decimal("1000"),
    )


def test_preflight_orchestrator_verifies_private_auth_without_candidate(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("nebula.preflight.ReadOnlyRuntime", FakeRuntime)
    client = ReadOnlySnapshotClient()
    state = StateStore(str(tmp_path / "no-candidate.sqlite3"))

    report = run_read_only_preflight(
        client, state, settings_read_only(),
        bar="1H", entry_plan_policy=policy(), clock_ms=lambda: NOW_MS,
    )

    assert report["status"] == "no_candidate"
    assert report["private_api_auth"]["status"] == "auth_verified"
    assert report["private_api_auth"]["permissions_ok"] is True
    assert report["orders_enabled"] is False
    assert report["order_submission_attempts"] == 0
    assert client.calls == ["account_config", "balance"]


def test_preflight_orchestrator_fails_closed_when_key_permissions_wrong(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("nebula.preflight.ReadOnlyRuntime", FakeRuntime)
    client = ReadOnlySnapshotClient()
    original = client.get_account_config

    def wrong_permissions():
        rows = original()
        rows[0]["perm"] = "read_only,trade,withdraw"
        return rows

    client.get_account_config = wrong_permissions
    state = StateStore(str(tmp_path / "bad-key.sqlite3"))

    report = run_read_only_preflight(
        client, state, settings_read_only(),
        bar="1H", entry_plan_policy=policy(), clock_ms=lambda: NOW_MS,
    )

    assert report["status"] == "auth_failed"
    assert report["private_api_auth"]["permissions_ok"] is False
    assert "api_key_permissions_not_exactly_read_only_trade" in (
        report["private_api_auth"]["blockers"]
    )
    assert report["market_analysis"] is None
    assert report["order_submission_attempts"] == 0


def test_preflight_orchestrator_approves_candidate_without_exchange_writes(
    monkeypatch, tmp_path
):
    class CandidateRuntime(FakeRuntime):
        def run_once(self):
            return {
                "mode": "read_only_analysis",
                "orders_enabled": False,
                "order_submission_attempts": 0,
                "signals": [{
                    "instrument": "AAA-USDT",
                    "signal": "buy",
                    "candidate_entry_plan": plan(),
                }],
            }

    monkeypatch.setattr("nebula.preflight.ReadOnlyRuntime", CandidateRuntime)
    client = ReadOnlySnapshotClient()
    state = StateStore(str(tmp_path / "candidate.sqlite3"))

    report = run_read_only_preflight(
        client, state, settings_read_only(),
        bar="1H", entry_plan_policy=policy(), clock_ms=lambda: NOW_MS,
    )

    assert report["status"] == "approved"
    assert report["orders_enabled"] is False
    assert report["order_submission_attempts"] == 0
    assert report["risk_preflight"]["execution"] == "not_submitted_preflight_only"
    assert client.calls == [
        "account_config", "balance",
        "instrument_rules", "account_config", "balance", "tickers",
        "pending_regular", "pending_oco", "pending_conditional", "account_bills",
        "account_bills",
    ]
    assert "candidate" not in report["risk_preflight"]
    serialized = str(report)
    for financial_field in (
        "account_equity_usdt", "available_usdt",
        "projected_daily_pnl_after_planned_stop_quote",
        "'candidate':", "candidate_entry_plan", "entry_price", "stop_loss",
        "take_profit", "notional_quote", "'size':", "'0.1'",
    ):
        assert financial_field not in serialized
    assert not hasattr(client, "place_spot_limit")
    assert not hasattr(client, "place_spot_oco_exit")


def test_redaction_removes_nested_sensitive_fields_but_keeps_status():
    report = {
        "status": "approved",
        "selected_instrument": "AAA-USDT",
        "risk_preflight": {
            "execution": "not_submitted_preflight_only",
            "candidate": {
                "instrument": "AAA-USDT", "size": "0.1",
                "entry_price": "100", "stop_loss": "90",
            },
            "account_equity_usdt": "1000",
            "available_usdt": "900",
            "projected_daily_pnl_after_planned_stop_quote": "-10",
            "authorization": {
                "planned_entry": {"entry_price": "100", "size": "0.1"},
                "balance_details": [{"ccy": "USDT", "availBal": "900", "liab": "0"}],
                "position_size": "0.1",
                "notional_quote": "10",
            },
        },
        "market_analysis": {
            "signals": [{
                "instrument": "AAA-USDT",
                "candidate_entry_plan": {"entry_price": "100", "stop_loss": "90"},
                "account_id": "demo-account",
                "uid": "demo-uid",
                "api_key": "must-not-leak",
            }],
        },
    }

    redacted = _redact_financials(report)
    serialized = str(redacted)

    assert redacted["status"] == "approved"
    assert redacted["selected_instrument"] == "AAA-USDT"
    assert redacted["risk_preflight"]["execution"] == "not_submitted_preflight_only"
    for sensitive in (
        "account_equity_usdt", "available_usdt", "projected_daily_pnl",
        "'candidate':", "planned_entry", "candidate_entry_plan", "entry_price",
        "stop_loss", "balance_details", "availBal", "liab", "position_size",
        "notional_quote", "account_id", "uid", "api_key", "must-not-leak",
        "'size':", "'0.1'", "1000", "900",
    ):
        assert sensitive not in serialized



def test_preflight_orchestrator_uses_selected_four_hour_candle_interval(
    monkeypatch, tmp_path
):
    class FourHourRuntime(FakeRuntime):
        def run_once(self):
            candidate = plan()
            candidate["candle_ts"] = NOW_MS - 14_400_000
            return {
                "mode": "read_only_analysis",
                "orders_enabled": False,
                "order_submission_attempts": 0,
                "signals": [{
                    "instrument": "AAA-USDT",
                    "signal": "buy",
                    "candidate_entry_plan": candidate,
                }],
            }

    monkeypatch.setattr("nebula.preflight.ReadOnlyRuntime", FourHourRuntime)
    client = ReadOnlySnapshotClient()
    state = StateStore(str(tmp_path / "four-hour.sqlite3"))

    report = run_read_only_preflight(
        client, state, settings_read_only(),
        bar="4H", entry_plan_policy=policy(), clock_ms=lambda: NOW_MS,
    )

    assert report["status"] == "approved"
    assert report["order_submission_attempts"] == 0
