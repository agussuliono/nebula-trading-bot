from decimal import Decimal

import pytest

from nebula.config import Settings
from nebula.risk_authorizer import (
    CandidateRiskPreflight,
    PortfolioRiskAuthorizer,
    RiskAuthorizationError,
)
from nebula.state import StateStore


NOW_MS = 1_800_000_000_000
CANDLE_TS = NOW_MS - 3_600_000


def candidate(**overrides):
    value = {
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
    value.update(overrides)
    return value


class SnapshotClient:
    def __init__(self, ask="100", min_size="0.001", bid_offset="0.05", bills=None):
        self.ask = ask
        self.min_size = min_size
        self.bid_offset = bid_offset
        self.read_calls = []
        self.bills = bills or []

    def get_spot_instrument(self, inst_id):
        self.read_calls.append("instrument_rules")
        return {
            "instType": "SPOT",
            "instId": inst_id,
            "baseCcy": "AAA",
            "quoteCcy": "USDT",
            "tickSz": "0.1",
            "lotSz": "0.001",
            "minSz": self.min_size,
            "state": "live",
        }

    def get_account_config(self):
        self.read_calls.append("account_config")
        return [{
            "acctLv": "1",
            "autoLoan": False,
            "enableSpotBorrow": False,
            "perm": "read_only,trade",
            "uid": "test-demo-account-uid",
        }]

    def get_balance(self):
        self.read_calls.append("balance")
        return [{
            "uTime": str(NOW_MS),
            "details": [{
                "ccy": "USDT",
                "eq": "1000",
                "availBal": "1000",
                "frozenBal": "0",
                "liab": "0",
            }],
        }]

    def get_tickers(self):
        self.read_calls.append("tickers")
        return [{
            "instId": "AAA-USDT",
            "bidPx": str(Decimal(self.ask) - Decimal(self.bid_offset)),
            "askPx": self.ask,
            "bidSz": "100",
            "askSz": "100",
            "ts": str(NOW_MS),
        }]

    def get_account_bills(self, *, begin=None, end=None, limit=100):
        self.read_calls.append("account_bills")
        from nebula.bill_reconciliation import _create_complete_account_bills_snapshot
        return _create_complete_account_bills_snapshot(
            self.bills, begin_ms=int(begin), end_ms=int(end),
            page_count=1, terminal_page_seen=True,
        )

    def get_pending_orders(self, inst_type):
        self.read_calls.append("pending_regular")
        assert inst_type == "SPOT"
        return []

    def get_pending_algo_orders(self, ord_type):
        self.read_calls.append("pending_" + ord_type)
        return []


def read_only_settings():
    # Private Demo reads are permitted, but exchange writes remain disabled.
    return Settings(
        api_key="demo-key",
        secret_key="demo-secret",
        passphrase="demo-pass",
        demo_account_exclusive=True,
        enable_demo_orders=False,
        kill_switch=True,
        max_daily_loss_quote=20,
        max_risk_quote=2,
        max_position_quote=100,
        market_data_max_age_ms=30_000,
    )


def make_preflight(tmp_path, *, ask="100"):
    client = SnapshotClient(ask=ask)
    state = StateStore(str(tmp_path / "preflight.sqlite3"))
    authorizer = PortfolioRiskAuthorizer(
        client,
        state,
        read_only_settings(),
        # The preflight uses the immutable candidate supplied to its own method.
        plan_provider=lambda **kwargs: pytest.fail("plan_provider must not replace candidate"),
        clock_ms=lambda: NOW_MS,
    )
    return CandidateRiskPreflight(authorizer, state), client, state


def test_candidate_preflight_uses_private_reads_without_enabling_writes(tmp_path):
    preflight, client, state = make_preflight(tmp_path)
    result = preflight.evaluate(candidate())

    assert result["status"] == "approved"
    assert result["entry_allowed"] is True
    assert result["execution"] == "not_submitted_preflight_only"
    assert result["order_submission_attempts"] == 0
    assert result["authorization"]["planned_entry"]["instrument"] == "AAA-USDT"
    assert Decimal(result["authorization"]["planned_entry"]["risk_to_stop_quote"]) <= Decimal("2")
    assert client.read_calls == [
        "instrument_rules", "account_config", "balance", "tickers",
        "pending_regular", "pending_oco", "pending_conditional", "account_bills",
        "account_bills",
    ]
    assert any(
        event["event_type"] == "entry_risk_preflight_approved"
        for event in state.list_events()
    )


@pytest.mark.parametrize("overrides", [
    {"signal_action": "sell"},
    {"candle_closed": False},
    {"orders_submitted": 1},
    {"size": "NaN"},
    {"stop_loss": "100"},
])
def test_invalid_candidate_is_rejected_before_private_reads(tmp_path, overrides):
    preflight, client, state = make_preflight(tmp_path)
    result = preflight.evaluate(candidate(**overrides))

    assert result["status"] == "rejected"
    assert result["entry_allowed"] is False
    assert result["execution"] == "not_submitted_preflight_only"
    assert result["order_submission_attempts"] == 0
    assert client.read_calls == []
    assert any(
        event["event_type"] == "entry_risk_preflight_rejected"
        for event in state.list_events()
    )


def test_candidate_preflight_rejects_price_that_moved_beyond_slippage_budget(tmp_path):
    preflight, client, _ = make_preflight(tmp_path, ask="100.5")
    result = preflight.evaluate(candidate())

    assert result["status"] == "rejected"
    assert "planned_entry_invalid" in result["reason"].lower()
    assert "tickers" in client.read_calls


def test_order_manager_authorizer_still_requires_demo_write_gate(tmp_path):
    client = SnapshotClient()
    state = StateStore(str(tmp_path / "write-gate.sqlite3"))
    authorizer = PortfolioRiskAuthorizer(
        client,
        state,
        read_only_settings(),
        plan_provider=lambda **kwargs: candidate(
            instrument=kwargs["instrument"],
            size=kwargs["size"],
            entry_price=kwargs["entry_price"],
            candle_ts=kwargs["candle_ts"],
        ),
        clock_ms=lambda: NOW_MS,
    )

    with pytest.raises(RiskAuthorizationError, match="failed closed"):
        authorizer(
            instrument="AAA-USDT",
            requested_size="0.1",
            requested_price="100",
            candle_ts=CANDLE_TS,
        )
    assert client.read_calls == []



def test_candidate_rejects_boolean_order_count_even_though_false_equals_zero(tmp_path):
    preflight, client, _ = make_preflight(tmp_path)
    result = preflight.evaluate(candidate(orders_submitted=False))
    assert result["status"] == "rejected"
    assert client.read_calls == []


@pytest.mark.parametrize(("field", "wrong_value"), [
    ("instrument", "BBB-USDT"),
    ("size", "0.2"),
    ("entry_price", "100.1"),
])
def test_order_authorizer_rejects_mismatched_plan_identity_before_reads(
    tmp_path, field, wrong_value
):
    client = SnapshotClient()
    state = StateStore(str(tmp_path / "mismatched-plan.sqlite3"))
    settings = Settings(
        api_key="demo-key", secret_key="demo-secret", passphrase="demo-pass",
        demo_account_exclusive=True,
        enable_demo_orders=True, max_drawdown_pct=0.05, kill_switch=False,
        max_daily_loss_quote=20, max_risk_quote=2, max_position_quote=100,
    )

    def plan_provider(**kwargs):
        plan = {
            "instrument": kwargs["instrument"],
            "size": kwargs["size"],
            "entry_price": kwargs["entry_price"],
            "signal_action": "buy",
            "candle_closed": True,
            "candle_ts": kwargs["candle_ts"],
            "stop_loss": "90",
            "take_profit": "120",
        }
        plan[field] = wrong_value
        return plan

    authorizer = PortfolioRiskAuthorizer(
        client, state, settings,
        plan_provider=plan_provider,
        clock_ms=lambda: NOW_MS,
    )

    with pytest.raises(RiskAuthorizationError, match=field):
        authorizer(
            instrument="AAA-USDT",
            requested_size="0.1",
            requested_price="100",
            candle_ts=CANDLE_TS,
        )
    assert client.read_calls == []


def test_candidate_preflight_rechecks_current_minimum_size_before_private_reads(tmp_path):
    client = SnapshotClient(min_size="0.2")
    state = StateStore(str(tmp_path / "changed-rules.sqlite3"))
    authorizer = PortfolioRiskAuthorizer(
        client,
        state,
        read_only_settings(),
        plan_provider=lambda **kwargs: pytest.fail("plan_provider must not replace candidate"),
        clock_ms=lambda: NOW_MS,
    )
    result = CandidateRiskPreflight(authorizer, state).evaluate(candidate())

    assert result["status"] == "rejected"
    assert "instrument_rules" in client.read_calls
    assert "account_config" not in client.read_calls



def test_candidate_preflight_rejects_widened_current_spread(tmp_path):
    client = SnapshotClient(bid_offset="1")
    state = StateStore(str(tmp_path / "wide-spread.sqlite3"))
    authorizer = PortfolioRiskAuthorizer(
        client,
        state,
        read_only_settings(),
        plan_provider=lambda **kwargs: pytest.fail("plan_provider must not replace candidate"),
        clock_ms=lambda: NOW_MS,
    )
    result = CandidateRiskPreflight(authorizer, state).evaluate(candidate())

    assert result["status"] == "rejected"
    assert "planned_entry_invalid" in result["reason"].lower()
    assert "tickers" in client.read_calls



def test_preflight_refuses_unconfirmed_demo_account_exclusivity_before_reads(tmp_path):
    from dataclasses import replace

    client = SnapshotClient()
    state = StateStore(str(tmp_path / "non-exclusive.sqlite3"))
    settings = replace(read_only_settings(), demo_account_exclusive=False)
    authorizer = PortfolioRiskAuthorizer(
        client,
        state,
        settings,
        plan_provider=lambda **kwargs: pytest.fail("plan_provider must not replace candidate"),
        clock_ms=lambda: NOW_MS,
    )
    result = CandidateRiskPreflight(authorizer, state).evaluate(candidate())

    assert result["status"] == "rejected"
    assert "exclusivity" in result["reason"].lower()
    assert client.read_calls == []



def test_daily_baseline_refuses_to_start_after_account_activity(tmp_path):
    client = SnapshotClient(bills=[{
        "billId": "123", "ccy": "USDT", "balChg": "-1", "bal": "999",
        "type": "2", "subType": "1", "ts": str(NOW_MS - 1000),
        "instId": "AAA-USDT",
    }])
    state = StateStore(str(tmp_path / "activity-before-baseline.sqlite3"))
    authorizer = PortfolioRiskAuthorizer(
        client, state, read_only_settings(),
        plan_provider=lambda **kwargs: pytest.fail("candidate plan should not be reached"),
        clock_ms=lambda: NOW_MS,
    )
    result = CandidateRiskPreflight(authorizer, state).evaluate(candidate())
    assert result["status"] == "rejected"
    assert "baseline missing after account activity" in result["reason"].lower()
    assert client.read_calls.count("account_bills") == 1


def test_non_spot_or_transfer_bill_blocks_authorization(tmp_path):
    client = SnapshotClient(bills=[{
        "billId": "124", "ccy": "USDT", "balChg": "100", "bal": "1100",
        "type": "1", "subType": "11", "ts": str(NOW_MS - 1000),
        "instId": "",
    }])
    state = StateStore(str(tmp_path / "transfer-bill.sqlite3"))
    authorizer = PortfolioRiskAuthorizer(
        client, state, read_only_settings(),
        plan_provider=lambda **kwargs: pytest.fail("candidate plan should not be reached"),
        clock_ms=lambda: NOW_MS,
    )
    result = CandidateRiskPreflight(authorizer, state).evaluate(candidate())
    assert result["status"] == "rejected"
    assert "baseline missing after account activity" in result["reason"].lower()


def test_daily_pnl_reconciliation_rejects_type_two_margin_bill(tmp_path):
    import hashlib
    from nebula.account_ledger import AccountLedger

    client = SnapshotClient(bills=[{
        "billId": "991", "ccy": "USDT", "balChg": "-0.1", "bal": "999.9",
        "type": "2", "subType": "1", "ts": str(NOW_MS - 500),
        "instType": "MARGIN", "instId": "AAA-USDT",
    }])
    state = StateStore(str(tmp_path / "margin-bill.sqlite3"))
    fingerprint = hashlib.sha256(b"test-demo-account-uid").hexdigest()
    AccountLedger(state).establish_daily_baseline(
        equity_quote="1000", timestamp_ms=NOW_MS - 1000,
        account_fingerprint=fingerprint,
    )
    authorizer = PortfolioRiskAuthorizer(
        client, state, read_only_settings(),
        plan_provider=lambda **kwargs: pytest.fail("plan provider must not be reached"),
        clock_ms=lambda: NOW_MS,
    )
    with pytest.raises(RiskAuthorizationError, match="non-Spot"):
        authorizer._daily_account_pnl(
            account_config=client.get_account_config(),
            balance=client.get_balance(),
            tickers=client.get_tickers(),
            regular=[], oco=[], conditional=[], lock=None, unresolved=[],
            now_ms=NOW_MS, daily_realized="0",
        )
