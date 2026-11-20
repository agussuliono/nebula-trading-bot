from decimal import Decimal

from nebula.config import Settings
from nebula.entry_plan import EntryPlanPolicy
from nebula.preflight import run_read_only_preflight
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
            "perm": "read_only,trade",
        }]

    def get_balance(self):
        self.calls.append("balance")
        return [{
            "uTime": str(NOW_MS),
            "details": [{
                "ccy": "USDT", "eq": "1000",
                "availBal": "1000", "frozenBal": "0",
            }],
        }]

    def get_tickers(self):
        self.calls.append("tickers")
        return [{
            "instId": "AAA-USDT", "bidPx": "99.95", "askPx": "100",
            "bidSz": "100", "askSz": "100", "ts": str(NOW_MS),
        }]

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


def test_preflight_orchestrator_does_not_read_private_data_without_candidate(
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
    assert report["orders_enabled"] is False
    assert report["order_submission_attempts"] == 0
    assert client.calls == []


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
        "instrument_rules", "account_config", "balance", "tickers",
        "pending_regular", "pending_oco", "pending_conditional",
    ]
    assert not hasattr(client, "place_spot_limit")
    assert not hasattr(client, "place_spot_oco_exit")



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
