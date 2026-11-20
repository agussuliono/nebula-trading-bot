from datetime import datetime, timezone
from types import SimpleNamespace
from decimal import Decimal

import pytest

from nebula.runtime import ReadOnlyRuntime, RuntimePolicy, RuntimeErrorSafe
from nebula.entry_plan import EntryPlanPolicy
from nebula.state import StateStore


class FakePublicClient:
    def __init__(self, *, stale=False, fail=False):
        self.stale = stale
        self.fail = fail
        self.calls = []

    def get_instruments(self):
        self.calls.append("instruments")
        if self.fail:
            raise OSError("network unavailable")
        return [{"instId": "AAA-USDT", "instType": "SPOT", "baseCcy": "AAA",
                 "quoteCcy": "USDT", "tickSz": "0.1", "lotSz": "0.001",
                 "minSz": "0.001", "state": "live",
                 "listTime": "1600000000000"}]

    def get_tickers(self):
        self.calls.append("tickers")
        return [{"instId": "AAA-USDT", "bidPx": "99", "askPx": "99.1",
                 "volCcy24h": "5000000", "bidSz": "100", "askSz": "100", "ts": "1799999999000", "last": "99.05"}]

    def get_candles(self, inst_id, bar, limit):
        self.calls.append(("candles", inst_id, bar, limit))
        now_ms = 1_800_000_000_000
        latest = now_ms - (3 * 3_600_000 if self.stale else 3_600_000)
        # Closed candle history, returned newest-first as on OKX.
        rows = []
        for i in range(80):
            ts = latest - i * 3_600_000
            close = 100 + i * 0.01
            rows.append([str(ts), str(close), str(close * 1.01), str(close * .99),
                         str(close), "10", "1000", "1000", "1"])
        return rows


def _runtime(tmp_path, client, *, now=None, entry_plan_policy=None):
    state = StateStore(str(tmp_path / "runtime.sqlite3"))
    policy = RuntimePolicy(bar="1H", candle_limit=100, max_symbols_to_analyze=5,
                           min_quote_volume=1_000_000, max_spread_pct=.5,
                           min_volatility_pct=.1, max_volatility_pct=5,
                           min_listing_age_days=30, min_data_points=51,
                           fast_ema=3, slow_ema=5,
                           entry_plan_policy=entry_plan_policy)
    clock = lambda: now or datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
    return ReadOnlyRuntime(client, state, policy=policy, clock=clock), state


def test_runtime_is_read_only_and_records_cycle(tmp_path):
    client = FakePublicClient()
    runtime, state = _runtime(tmp_path, client)
    result = runtime.run_once()
    assert result["mode"] == "read_only_analysis"
    assert result["orders_enabled"] is False
    assert result["order_submission_attempts"] == 0
    assert result["eligible_count"] == 1
    assert len(result["signals"]) == 1
    assert result["signals"][0]["execution"] == "not_submitted_read_only_runtime"
    assert client.calls.count("instruments") == 1
    assert client.calls.count("tickers") == 1
    assert len([x for x in client.calls if isinstance(x, tuple) and x[0] == "candles"]) == 1
    assert any(e["event_type"] == "runtime_cycle_completed" for e in state.list_events())


def test_runtime_signal_is_idempotent_for_same_closed_candle(tmp_path):
    runtime, _ = _runtime(tmp_path, FakePublicClient())
    first = runtime.run_once()
    second = runtime.run_once()
    assert first["signals"][0]["new_signal"] is True
    assert second["signals"][0]["new_signal"] is False


def test_runtime_aborts_when_public_universe_fails(tmp_path):
    runtime, state = _runtime(tmp_path, FakePublicClient(fail=True))
    with pytest.raises(RuntimeErrorSafe):
        runtime.run_once()
    assert any(e["event_type"] == "runtime_market_data_error" for e in state.list_events())


def test_policy_rejects_invalid_ema_order():
    with pytest.raises(ValueError):
        RuntimePolicy(fast_ema=50, slow_ema=20)


def test_policy_rejects_unsupported_bar():
    with pytest.raises(ValueError):
        RuntimePolicy(bar="5m")


def test_runtime_excludes_stale_candles(tmp_path):
    runtime, _ = _runtime(tmp_path, FakePublicClient(stale=True))
    result = runtime.run_once()
    assert result["eligible_count"] == 0
    assert result["signals"] == []
    assert result["stale_instruments"] == ["AAA-USDT"]



def test_runtime_builds_candidate_entry_plan_without_authorizing_or_submitting(monkeypatch, tmp_path):
    policy = EntryPlanPolicy(
        risk_budget_quote=Decimal("10"), max_notional_quote=Decimal("1000"),
        atr_period=14, stop_atr_multiple=Decimal("2"), reward_risk=Decimal("2"),
        fee_bps=Decimal("10"), slippage_bps=Decimal("10"),
    )
    runtime, state = _runtime(tmp_path, FakePublicClient(), entry_plan_policy=policy)
    # Use the latest confirmed candle timestamp from the exact feed.
    original = FakePublicClient().get_candles("AAA-USDT", "1H", 100)
    latest_ts = int(original[0][0])
    monkeypatch.setattr(
        "nebula.runtime.trend_signal",
        lambda candles, fast, slow: SimpleNamespace(
            action="buy", reason="test_buy_signal", candle_ts=latest_ts,
        ),
    )

    result = runtime.run_once()
    signal = result["signals"][0]
    assert signal["signal"] == "buy"
    assert signal["candidate_entry_plan"] is not None
    assert signal["candidate_entry_plan"]["candle_ts"] == latest_ts
    assert signal["candidate_entry_plan"]["orders_submitted"] == 0
    assert signal["entry_plan_status"] == "candidate_only_not_authorized"
    assert signal["authorization"] == "not_requested_read_only_runtime"
    assert signal["execution"] == "not_submitted_read_only_runtime"
    assert result["orders_enabled"] is False
    assert result["order_submission_attempts"] == 0
    assert any(e["event_type"] == "runtime_cycle_completed" for e in state.list_events())


def test_runtime_does_not_build_candidate_plan_without_explicit_risk_limits(monkeypatch, tmp_path):
    runtime, _ = _runtime(tmp_path, FakePublicClient())
    latest_ts = int(FakePublicClient().get_candles("AAA-USDT", "1H", 100)[0][0])
    monkeypatch.setattr(
        "nebula.runtime.trend_signal",
        lambda candles, fast, slow: SimpleNamespace(
            action="buy", reason="test_buy_signal", candle_ts=latest_ts,
        ),
    )
    result = runtime.run_once()
    signal = result["signals"][0]
    assert signal["candidate_entry_plan"] is None
    assert signal["entry_plan_status"] == "disabled_not_configured"
    assert result["order_submission_attempts"] == 0


def test_runtime_blocks_candidate_plan_when_circuit_breaker_halted(monkeypatch, tmp_path):
    class HaltedBreaker:
        def status(self, now=None):
            return {"halted": True}

    policy = EntryPlanPolicy(risk_budget_quote="10", max_notional_quote="1000")
    runtime, _ = _runtime(tmp_path, FakePublicClient(), entry_plan_policy=policy)
    runtime.circuit_breaker = HaltedBreaker()
    latest_ts = int(FakePublicClient().get_candles("AAA-USDT", "1H", 100)[0][0])
    monkeypatch.setattr(
        "nebula.runtime.trend_signal",
        lambda candles, fast, slow: SimpleNamespace(
            action="buy", reason="test_buy_signal", candle_ts=latest_ts,
        ),
    )
    result = runtime.run_once()
    signal = result["signals"][0]
    assert signal["blocked_reason"] == "risk_circuit_breaker_halted"
    assert signal["candidate_entry_plan"] is None
    assert signal["entry_plan_status"] == "not_applicable"
