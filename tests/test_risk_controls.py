from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from nebula.config import SafetyError
from nebula.risk_controls import CircuitBreaker
from nebula.state import StateStore


def test_daily_loss_circuit_breaker_trips_and_blocks_new_entries(tmp_path):
    store = StateStore(str(tmp_path / "risk.db"))
    breaker = CircuitBreaker(store, "10")
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    first = breaker.record_closed_trade("NBPOS1", "-6", now=now)
    assert first["tripped"] is False
    second = breaker.record_closed_trade("NBPOS2", "-4", now=now)
    assert second["tripped"] is True
    with pytest.raises(SafetyError, match="circuit breaker"):
        breaker.check_entry_allowed(now=now)
    assert breaker.status(now=now)["daily_realized_pnl_quote"] == "-10"


def test_closed_trade_event_is_idempotent_and_conflicts_fail_closed(tmp_path):
    breaker = CircuitBreaker(StateStore(str(tmp_path / "risk.db")), "10")
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    assert breaker.record_closed_trade("NBPOS1", "-3", now=now)["inserted"] is True
    assert breaker.record_closed_trade("NBPOS1", "-3", now=now)["inserted"] is False
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        breaker.record_closed_trade("NBPOS1", "-4", now=now)
    assert breaker.status(now=now)["daily_realized_pnl_quote"] == "-3"


def test_daily_counter_rolls_over_at_utc_midnight_but_manual_stop_persists(tmp_path):
    breaker = CircuitBreaker(StateStore(str(tmp_path / "risk.db")), "5")
    before = datetime(2026, 11, 19, 23, 59, tzinfo=timezone.utc)
    after = before + timedelta(minutes=2)
    breaker.record_closed_trade("NBPOS1", "-5", now=before)
    with pytest.raises(SafetyError):
        breaker.check_entry_allowed(now=before)
    assert breaker.check_entry_allowed(now=after)["halted"] is False
    breaker.engage_emergency_stop("operator requested stop")
    with pytest.raises(SafetyError):
        breaker.check_entry_allowed(now=after)
    assert breaker.status(now=after)["manual_stop"] is True


def test_emergency_stop_clear_requires_explicit_confirmation(tmp_path):
    breaker = CircuitBreaker(StateStore(str(tmp_path / "risk.db")), "5")
    breaker.engage_emergency_stop()
    with pytest.raises(SafetyError, match="explicit confirmation"):
        breaker.clear_emergency_stop()
    assert breaker.clear_emergency_stop(explicit_confirmation=True)["halted"] is False


def test_utc_day_requires_timezone_aware_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        CircuitBreaker.utc_day(datetime(2026, 11, 19))


def test_manual_emergency_stop_blocks_both_buy_and_sell(tmp_path):
    breaker = CircuitBreaker(StateStore(str(tmp_path / "risk.db")), "10")
    breaker.engage_emergency_stop("operator stop")
    with pytest.raises(SafetyError, match="emergency stop"):
        breaker.check_order_allowed("buy")
    with pytest.raises(SafetyError, match="emergency stop"):
        breaker.check_order_allowed("sell")


def test_daily_loss_halt_allows_risk_reducing_sell(tmp_path):
    breaker = CircuitBreaker(StateStore(str(tmp_path / "risk.db")), "5")
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    breaker.record_closed_trade("NBPOS1", "-5", now=now)
    assert breaker.check_order_allowed("sell", now=now)["halted"] is True
    with pytest.raises(SafetyError, match="daily loss"):
        breaker.check_order_allowed("buy", now=now)


def test_idempotent_closed_trade_retry_after_midnight_keeps_original_day(tmp_path):
    breaker = CircuitBreaker(StateStore(str(tmp_path / "risk.db")), "10")
    before = datetime(2026, 11, 19, 23, 59, tzinfo=timezone.utc)
    after = before + timedelta(minutes=2)
    first = breaker.record_closed_trade("NBPOS1", "2", now=before)
    replay = breaker.record_closed_trade("NBPOS1", "2", now=after)
    assert replay["inserted"] is False
    assert replay["trading_day"] == first["trading_day"]
    assert breaker.status(now=after)["daily_realized_pnl_quote"] == "0"
