import csv
import math

import pytest

from nebula.backtest import (BacktestConfig, BacktestError, Backtester, Candle,
                             load_csv, validate_candles, walk_forward, stress_test,
                             _net_stop_risk_per_unit)


def series(n=240):
    out = []
    price = 100.0
    for i in range(n):
        # Repeated trend reversals produce both bullish and bearish EMA crosses.
        drift = 0.55 if (i // 35) % 2 == 0 else -0.48
        price = max(10.0, price + drift + math.sin(i * 0.7) * 0.25)
        out.append(Candle(i + 1, price, price * 1.025, price * 0.975,
                          price, 1000.0, True))
    return out


def config(**kw):
    values = dict(initial_equity=1000, risk_per_trade_pct=0.01,
                  max_position_pct=0.25, stop_loss_pct=0.015,
                  take_profit_pct=0.03, fee_bps=10, slippage_bps=10,
                  fast_ema=5, slow_ema=15, max_drawdown_halt_pct=0.5)
    values.update(kw)
    return BacktestConfig(**values)


def test_backtest_is_offline_and_never_submits_orders():
    result = Backtester(config()).run(series())
    assert result["orders_submitted"] == 0
    assert result["bars_evaluated"] > 100
    assert result["final_equity"] > 0
    assert result["trade_count"] > 0
    assert all(t["entry_ts"] <= t["exit_ts"] for t in result["trades"])
    assert all(t["signal_ts"] < t["entry_ts"] for t in result["trades"])


def test_fee_and_slippage_reduce_results():
    data = series()
    free = Backtester(config(fee_bps=0, slippage_bps=0)).run(data)
    costly = Backtester(config(fee_bps=25, slippage_bps=25)).run(data)
    assert costly["final_equity"] <= free["final_equity"]


def test_trade_size_is_capped_by_notional():
    result = Backtester(config(max_position_pct=0.10)).run(series())
    for trade in result["trades"]:
        assert trade["quantity"] * trade["entry_price"] <= 1000 * 0.1001


def test_stop_has_priority_when_stop_and_target_hit_same_bar():
    data = series(100)
    cfg = config(fast_ema=3, slow_ema=5, stop_loss_pct=0.01, take_profit_pct=0.02)
    baseline = Backtester(cfg).run(data)
    if not baseline["trades"]:
        pytest.skip("Synthetic path did not open a position")
    trade = baseline["trades"][0]
    idx = next(i for i, c in enumerate(data) if c.ts == trade["entry_ts"])
    # Force both levels to be touched in the entry candle after a valid signal.
    modified = list(data)
    c = modified[idx]
    modified[idx] = Candle(c.ts, c.open, max(c.high, trade["take_profit"] * 1.01),
                           min(c.low, trade["stop_loss"] * 0.99), c.close, c.volume)
    result = Backtester(cfg).run(modified)
    assert any(t["exit_reason"] == "stop_loss_same_bar_priority" for t in result["trades"])


def test_walk_forward_uses_chronological_out_of_sample_folds():
    result = walk_forward(series(300), config=config(), folds=3)
    assert result["fold_count"] == 3
    starts = [x["test_start_ts"] for x in result["folds"]]
    ends = [x["test_end_ts"] for x in result["folds"]]
    assert starts[0] < ends[0] < starts[1] < ends[1] < starts[2]
    assert result["orders_submitted"] == 0


def test_invalid_candles_fail_closed():
    data = series()
    data[10] = Candle(data[10].ts, 100, 90, 95, 100)
    with pytest.raises(BacktestError):
        validate_candles(data)


def test_duplicate_or_unsorted_timestamps_rejected():
    data = series()
    data[20] = Candle(data[19].ts, 100, 101, 99, 100)
    with pytest.raises(BacktestError):
        validate_candles(data)


def test_csv_import_and_unconfirmed_filter(tmp_path):
    p = tmp_path / "candles.csv"
    with p.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "open", "high", "low", "close", "volume", "confirm"])
        for c in series():
            writer.writerow([c.ts, c.open, c.high, c.low, c.close, c.volume, "1"])
        writer.writerow([241, 100, 101, 99, 100, 10, "0"])
    loaded = load_csv(p)
    assert len(loaded) == 240


def test_config_rejects_excessive_risk_and_bad_costs():
    with pytest.raises(BacktestError):
        config(risk_per_trade_pct=0.10)
    with pytest.raises(BacktestError):
        config(fee_bps=1000)


def test_walk_forward_rejects_too_few_folds_or_bars():
    with pytest.raises(BacktestError):
        walk_forward(series(80), config=config(), folds=10)
    with pytest.raises(BacktestError):
        walk_forward(series(), config=config(), folds=1)


def test_daily_loss_circuit_breaker_blocks_new_entries_for_utc_day():
    data = series(240)
    cfg = config(max_daily_loss_pct=0.001, max_drawdown_halt_pct=0.9)
    result = Backtester(cfg).run(data)
    assert any(point["daily_loss_halted"] for point in result["equity_curve"])


def test_okx_candle_parser_respects_confirm_flag():
    row = ["1700000000000", "100", "102", "99", "101", "5", "500", "505", "1"]
    candle = Candle.from_okx(row)
    assert candle.ts == 1700000000000
    assert candle.confirmed is True
    assert candle.volume == 505
    row[8] = "0"
    assert Candle.from_okx(row).confirmed is False


def test_config_rejects_non_numeric_and_boolean_values():
    with pytest.raises(BacktestError):
        config(initial_equity="not-a-number")
    with pytest.raises(BacktestError):
        config(fee_bps=True)


def test_cost_stress_scenarios_are_monotonic_and_read_only():
    report = stress_test(series(), config=config())
    assert report["scenario_count"] == 4
    assert report["orders_submitted"] == 0
    returns = [x["net_return_pct"] for x in report["scenarios"]]
    assert returns[0] >= returns[1] >= returns[3]
    assert returns[0] >= returns[2] >= returns[3]


def test_daily_loss_halt_does_not_carry_stale_entry_signal_into_next_utc_day(monkeypatch):
    from nebula import backtest as bt
    from datetime import datetime, timezone

    # Index 16: bullish cross -> entry at 17. Index 18: bearish cross ->
    # exit at 19 open. Index 19: a fresh bullish cross occurs on the same
    # candle that realizes a daily loss; it must not be carried into day 2.
    n = 80
    base = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc).timestamp() * 1000
    data = []
    for i in range(n):
        open_px = 98.0 if i == 19 else 100.0
        data.append(Candle(int(base + i * 3_600_000), open_px,
                           max(open_px, 100.5), min(open_px, 97.5), open_px, 1000, True))

    fast = [99.0] * n
    slow = [100.0] * n
    fast[16] = 101.0  # bullish cross
    fast[17] = 101.0
    fast[18] = 99.0   # bearish cross
    fast[19] = 101.0  # bullish cross while daily loss is realized
    fast[20] = 101.0
    calls = iter([fast, slow])
    monkeypatch.setattr(bt, "_ema", lambda values, period: next(calls))

    cfg = BacktestConfig(initial_equity=1000, risk_per_trade_pct=.01,
                         max_position_pct=.25, stop_loss_pct=.10,
                         take_profit_pct=.20, fee_bps=0, slippage_bps=0,
                         fast_ema=5, slow_ema=15,
                         max_drawdown_halt_pct=.9, max_daily_loss_pct=.001)
    result = Backtester(cfg).run(data)
    assert result["trade_count"] == 1
    assert result["trades"][0]["exit_reason"] == "ema_bearish_cross"
    assert result["daily_loss_halted_at_end"] is False
    assert all(t["entry_ts"] != data[20].ts for t in result["trades"])



def test_net_stop_risk_does_not_double_count_entry_slippage():
    entry = 100.10  # raw 100.00 open with 10 bps adverse buy slippage
    stop = entry * 0.98
    fee = 0.001
    slip = 0.001
    expected = entry * (1 + fee) - stop * (1 - slip) * (1 - fee)
    actual = _net_stop_risk_per_unit(entry, stop, fee, slip)
    assert actual == pytest.approx(expected, rel=1e-12)
    old_approximation = (entry - stop + entry * (fee + slip)
                         + stop * (fee + slip))
    assert actual < old_approximation


def test_net_stop_risk_rejects_invalid_inputs():
    with pytest.raises(BacktestError):
        _net_stop_risk_per_unit(100, 101, .001, .001)
    with pytest.raises(BacktestError):
        _net_stop_risk_per_unit(100, 98, 1.0, .001)
