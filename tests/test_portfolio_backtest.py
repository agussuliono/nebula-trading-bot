import math
import pytest
from nebula.backtest import BacktestConfig, BacktestError, Candle, PortfolioBacktester


def series(n=260, phase=0, drift_scale=1.0):
    out=[]
    p=100.0
    for i in range(n):
        drift = (0.60 if ((i + phase) // 38) % 2 == 0 else -0.52) * drift_scale
        p=max(10.0, p + drift + math.sin((i+phase)*0.65)*0.22)
        out.append(Candle(i+1, p, p*1.018, p*0.982, p, 1000, True))
    return out


def cfg(**kw):
    x=dict(initial_equity=1000, risk_per_trade_pct=.01, max_position_pct=.25,
           stop_loss_pct=.015, take_profit_pct=.03, fee_bps=10, slippage_bps=10,
           fast_ema=5, slow_ema=15, max_drawdown_halt_pct=.8, max_daily_loss_pct=.4)
    x.update(kw)
    return BacktestConfig(**x)


def test_portfolio_uses_one_shared_position_and_cash():
    markets={"BTC-USDT":series(), "ETH-USDT":series(phase=7, drift_scale=1.04), "SOL-USDT":series(phase=13, drift_scale=.97)}
    report=PortfolioBacktester(cfg()).run(markets)
    assert report["orders_submitted"] == 0
    assert report["max_concurrent_positions"] == 1
    assert report["symbols"] == ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    assert all(t["symbol"] in markets for t in report["trades"])
    assert all(t["signal_ts"] < t["entry_ts"] <= t["exit_ts"] for t in report["trades"])
    assert sum(report["selection_count_by_symbol"].values()) == report["trade_count"]


def test_portfolio_requires_multiple_assets_and_synchronized_timestamps():
    with pytest.raises(BacktestError):
        PortfolioBacktester(cfg()).run({"BTC-USDT":series()})
    bad=series()
    bad[80]=Candle(999, bad[80].open, bad[80].high, bad[80].low, bad[80].close)
    with pytest.raises(BacktestError):
        PortfolioBacktester(cfg()).run({"BTC-USDT":series(), "ETH-USDT":bad})


def test_portfolio_rejects_duplicate_normalized_symbols():
    with pytest.raises(BacktestError):
        PortfolioBacktester(cfg()).run({"btc-usdt":series(), "BTC-USDT":series()})


def test_portfolio_selection_is_deterministic():
    markets={"ETH-USDT":series(), "BTC-USDT":series()}
    a=PortfolioBacktester(cfg()).run(markets)
    b=PortfolioBacktester(cfg()).run(dict(reversed(list(markets.items()))))
    assert a["trades"] == b["trades"]
    assert a["final_equity"] == b["final_equity"]


def test_portfolio_costs_reduce_or_equal_final_equity():
    markets={"BTC-USDT":series(), "ETH-USDT":series(phase=5)}
    low=PortfolioBacktester(cfg(fee_bps=0, slippage_bps=0)).run(markets)
    high=PortfolioBacktester(cfg(fee_bps=30, slippage_bps=30)).run(markets)
    assert high["final_equity"] <= low["final_equity"]


def test_portfolio_walk_forward_and_stress_are_read_only():
    from nebula.backtest import portfolio_walk_forward, portfolio_stress_test
    markets={"BTC-USDT":series(320), "ETH-USDT":series(320, phase=6)}
    wf=portfolio_walk_forward(markets, config=cfg(), folds=3)
    stress=portfolio_stress_test(markets, config=cfg())
    assert wf["fold_count"] == 3 and wf["orders_submitted"] == 0
    assert stress["scenario_count"] == 4 and stress["orders_submitted"] == 0


def test_intrabar_exit_does_not_allow_retroactive_same_open_reentry():
    # The one-slot engine only evaluates another asset's candidate if the slot
    # was free at the current bar open or the prior position exited at that open.
    markets={"BTC-USDT":series(), "ETH-USDT":series(phase=11)}
    report=PortfolioBacktester(cfg()).run(markets)
    trades=report["trades"]
    for previous, current in zip(trades, trades[1:]):
        if previous["exit_reason"] in {"stop_loss", "take_profit", "stop_loss_same_bar_priority"}:
            assert current["entry_ts"] > previous["exit_ts"]


def test_portfolio_stress_can_replay_generator_inputs():
    from nebula.backtest import portfolio_stress_test
    markets={"BTC-USDT":(c for c in series()), "ETH-USDT":(c for c in series(phase=4))}
    report=portfolio_stress_test(markets, config=cfg())
    assert report["scenario_count"] == 4


def test_portfolio_walk_forward_rejects_case_colliding_symbols():
    from nebula.backtest import portfolio_walk_forward
    with pytest.raises(BacktestError):
        portfolio_walk_forward({"btc-usdt":series(320), "BTC-USDT":series(320)}, config=cfg())


def test_portfolio_does_not_trade_signal_from_candle_that_tripped_daily_halt(monkeypatch):
    from nebula import backtest as bt
    from datetime import datetime, timezone

    n = 80
    base = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc).timestamp() * 1000
    data = []
    for i in range(n):
        open_px = 98.0 if i == 19 else 100.0
        data.append(Candle(int(base + i * 3_600_000), open_px,
                           max(open_px, 100.5), min(open_px, 97.5), open_px, 1000, True))

    fast = [99.0] * n
    slow = [100.0] * n
    fast[16] = 101.0  # initial entry signal
    fast[17] = 101.0
    fast[18] = 99.0   # bearish cross, exit next open
    fast[19] = 101.0  # signal forms as realized loss trips daily halt
    fast[20] = 101.0
    original_ema = bt._ema
    calls = {"count": 0}
    def controlled_ema(values, period):
        calls["count"] += 1
        # two assets x (fast, slow); identical signals are sufficient to
        # exercise the one-slot selector and daily-halt rollover.
        return fast if period == 5 else slow
    monkeypatch.setattr(bt, "_ema", controlled_ema)

    cfg = BacktestConfig(initial_equity=1000, risk_per_trade_pct=.01,
                         max_position_pct=.25, stop_loss_pct=.10,
                         take_profit_pct=.20, fee_bps=0, slippage_bps=0,
                         fast_ema=5, slow_ema=15,
                         max_drawdown_halt_pct=.9, max_daily_loss_pct=.001)
    result = PortfolioBacktester(cfg).run({"BTC-USDT": data, "ETH-USDT": data})
    assert result["trade_count"] == 1
    assert result["trades"][0]["exit_reason"] == "ema_bearish_cross"
    assert all(t["entry_ts"] != data[20].ts for t in result["trades"])
