import math

from nebula.backtest import BacktestConfig, Candle, PortfolioBacktester
from nebula.diagnostics import diagnose_portfolio, export_trade_records, _metrics


def series(n=260, phase=0):
    rows=[]
    price=100.0
    for i in range(n):
        drift=0.55 if ((i+phase)//35)%2==0 else -0.48
        price=max(10.0, price+drift+math.sin((i+phase)*0.61)*0.2)
        rows.append(Candle(i+1, price, price*1.018, price*0.982, price, 1000, True))
    return rows


def config(**overrides):
    values=dict(initial_equity=1000, risk_per_trade_pct=.01, max_position_pct=.25,
                stop_loss_pct=.015, take_profit_pct=.03, fee_bps=10, slippage_bps=10,
                fast_ema=5, slow_ema=15, max_drawdown_halt_pct=.8, max_daily_loss_pct=.4)
    values.update(overrides)
    return BacktestConfig(**values)


def test_metrics_handles_no_trades_all_wins_all_losses_and_breakeven():
    assert _metrics([])["profit_factor"] == 0.0
    wins = _metrics([{"net_pnl": 2, "bars_held": 2}])
    assert wins["profit_factor"] is None and wins["profit_factor_unbounded"] is True
    losses = _metrics([{"net_pnl": -2, "bars_held": 1}])
    assert losses["profit_factor"] == 0.0 and losses["expectancy_quote_per_trade"] == -2
    flat = _metrics([{"net_pnl": 0, "bars_held": 0}])
    assert flat["breakeven_count"] == 1 and flat["win_rate_pct"] == 0


def test_diagnostics_groups_symbols_exit_reasons_and_benchmark():
    markets={"BTC-USDT":series(), "ETH-USDT":series(phase=7), "SOL-USDT":series(phase=13)}
    cfg=config()
    result=PortfolioBacktester(cfg).run(markets)
    report=diagnose_portfolio(markets, result, cfg)
    assert set(report["by_symbol"]) == set(markets)
    assert sum(x["trade_count"] for x in report["by_symbol"].values()) == result["trade_count"]
    assert report["summary"]["trade_count"] == result["trade_count"]
    assert report["buy_and_hold_benchmark"]["method"].startswith("equal_weight_buy_and_hold")
    assert report["methodology"]["orders_submitted"] == 0
    assert all("entry_regime_at_signal" in trade for trade in report["trade_records"])


def test_regime_assignment_uses_signal_timestamp_not_future_candle():
    markets={"BTC-USDT":series(), "ETH-USDT":series(phase=4)}
    cfg=config()
    result=PortfolioBacktester(cfg).run(markets)
    report=diagnose_portfolio(markets, result, cfg)
    by_ts={t["signal_ts"]:t for t in report["trade_records"]}
    assert all(t["entry_regime_at_signal"] in {"uptrend", "sideways_or_non_uptrend", "insufficient_history"} for t in by_ts.values())


def test_sensitivity_reserves_final_period_without_using_it():
    markets={"BTC-USDT":series(320), "ETH-USDT":series(320, phase=3)}
    cfg=config()
    result=PortfolioBacktester(cfg).run(markets)
    report=diagnose_portfolio(markets, result, cfg, sensitivity=True)
    sensitivity=report["exploratory_ema_sensitivity"]
    assert len(sensitivity["results"]) == 9
    assert sensitivity["reserved_untouched_period_start_ts"] > sensitivity["research_end_ts"]
    assert "not evaluated here" in sensitivity["warning"]


def test_trade_csv_export(tmp_path):
    target=tmp_path/"reports"/"trades.csv"
    export_trade_records(target, [{"symbol":"BTC-USDT", "net_pnl":1, "entry_regime_at_signal":"uptrend"}])
    content=target.read_text()
    assert "entry_regime_at_signal" in content and "BTC-USDT" in content


def test_regime_label_is_independent_of_future_values():
    from nebula.backtest import _ema
    from nebula.diagnostics import _regime_at_signal
    rows=series(120)
    closes=[c.close for c in rows]
    fast=_ema(closes, 5)
    slow=_ema(closes, 15)
    idx=70
    before=_regime_at_signal(rows, fast, slow, idx)
    # Change only candles strictly after the signal index; the label must not move.
    changed=rows[:]
    for j in range(idx+1, len(changed)):
        c=changed[j]
        changed[j]=Candle(c.ts,c.open*1.8,c.high*1.8,c.low*1.8,c.close*1.8,c.volume,c.confirmed)
    changed_closes=[c.close for c in changed]
    after=_regime_at_signal(changed, _ema(changed_closes,5), _ema(changed_closes,15), idx)
    assert before == after
