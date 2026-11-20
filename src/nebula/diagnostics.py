"""Offline, read-only diagnostics for synchronized Spot portfolio backtests."""
from __future__ import annotations

import csv
from dataclasses import asdict, replace
from pathlib import Path
from statistics import mean
from typing import Iterable

from .backtest import BacktestConfig, BacktestError, Candle, PortfolioBacktester, _ema, validate_candles


def _metrics(trades: list[dict]) -> dict:
    pnls = [float(t["net_pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    return {
        "trade_count": len(pnls),
        "win_count": len(wins),
        "loss_count": len(losses),
        "breakeven_count": sum(1 for p in pnls if p == 0),
        "win_rate_pct": (len(wins) / len(pnls) * 100.0) if pnls else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else (None if gross_profit else 0.0),
        "profit_factor_unbounded": bool(gross_profit and not gross_loss),
        "gross_profit_quote": gross_profit,
        "gross_loss_quote": gross_loss,
        "net_pnl_quote": sum(pnls),
        "expectancy_quote_per_trade": mean(pnls) if pnls else 0.0,
        "average_win_quote": mean(wins) if wins else 0.0,
        "average_loss_quote": mean(losses) if losses else 0.0,
        "average_bars_held": mean([int(t.get("bars_held", 0)) for t in trades]) if trades else 0.0,
        "total_entry_fees_quote": sum(float(t.get("entry_fee", 0)) for t in trades),
        "total_exit_fees_quote": sum(float(t.get("exit_fee", 0)) for t in trades),
    }


def _regime_at_signal(candles: list[Candle], fast: list[float], slow: list[float], index: int) -> str:
    # Uses only the signal candle and earlier closed candles. This is descriptive,
    # not a signal filter, and never reads a candle after `index`.
    if index < 5 or not all(map(lambda x: x == x, (fast[index], slow[index], slow[index - 5]))):
        return "insufficient_history"
    if fast[index] > slow[index] and slow[index] > slow[index - 5]:
        return "uptrend"
    return "sideways_or_non_uptrend"


def _benchmark(markets: dict[str, list[Candle]], cfg: BacktestConfig, start: int, end: int) -> dict:
    """Equal-weight buy-and-hold, bought at first test open and liquidated at last close."""
    fee, slip = cfg.fee_bps / 10000.0, cfg.slippage_bps / 10000.0
    per_symbol = {}
    for symbol in sorted(markets):
        rows = markets[symbol]
        entry = rows[start].open * (1.0 + slip)
        exit_px = rows[end].close * (1.0 - slip)
        entry_cash = cfg.initial_equity / len(markets)
        qty = entry_cash / (entry * (1.0 + fee))
        final_value = qty * exit_px * (1.0 - fee)
        per_symbol[symbol] = {
            "initial_allocation_quote": entry_cash,
            "final_value_quote": final_value,
            "net_return_pct": (final_value / entry_cash - 1.0) * 100.0,
            "fee_bps_each_side": cfg.fee_bps,
            "slippage_bps_each_side": cfg.slippage_bps,
        }
    final_equity = sum(x["final_value_quote"] for x in per_symbol.values())
    return {
        "method": "equal_weight_buy_and_hold; buy_at_test_start_open; liquidate_at_test_end_close; fees_and_slippage_both_sides",
        "initial_equity": cfg.initial_equity,
        "final_equity": final_equity,
        "net_return_pct": (final_equity / cfg.initial_equity - 1.0) * 100.0,
        "per_symbol": per_symbol,
        "caveat": "Benchmark is a simple passive reference, not risk-matched to the one-position strategy; drawdown comparison is not supplied.",
    }


def diagnose_portfolio(markets: dict[str, Iterable[Candle]], result: dict,
                       config: BacktestConfig | None = None,
                       *, sensitivity: bool = False) -> dict:
    cfg = config or BacktestConfig()
    normalized = {str(s).strip().upper(): validate_candles(rows) for s, rows in markets.items()}
    if len(normalized) < 2:
        raise BacktestError("Diagnostics require at least two synchronized assets")
    symbols = sorted(normalized)
    ts = [c.ts for c in normalized[symbols[0]]]
    if any([c.ts for c in normalized[s]] != ts for s in symbols[1:]):
        raise BacktestError("Diagnostics require identical synchronized timestamps")
    start, end = max(cfg.slow_ema, 0), len(ts) - 1
    if len(ts) <= start + 2:
        raise BacktestError("Insufficient candles for diagnostics")

    trades = list(result.get("trades", []))
    by_symbol = {s: _metrics([t for t in trades if t.get("symbol") == s]) for s in symbols}
    by_exit = {}
    for reason in sorted({str(t.get("exit_reason", "unknown")) for t in trades}):
        group = [t for t in trades if str(t.get("exit_reason", "unknown")) == reason]
        by_exit[reason] = _metrics(group)

    fast_map, slow_map = {}, {}
    for s in symbols:
        closes = [c.close for c in normalized[s]]
        fast_map[s], slow_map[s] = _ema(closes, cfg.fast_ema), _ema(closes, cfg.slow_ema)
    index_by_ts = {value: i for i, value in enumerate(ts)}
    regime_groups = {}
    annotated = []
    for trade in trades:
        row = dict(trade)
        signal_index = index_by_ts.get(int(row.get("signal_ts", -1)))
        symbol = row.get("symbol")
        regime = (_regime_at_signal(normalized[symbol], fast_map[symbol], slow_map[symbol], signal_index)
                  if symbol in normalized and signal_index is not None else "unknown")
        row["entry_regime_at_signal"] = regime
        annotated.append(row)
        regime_groups.setdefault(regime, []).append(row)

    report = {
        "summary": {
            **_metrics(trades),
            "net_return_pct": result.get("net_return_pct"),
            "max_drawdown_pct": result.get("max_drawdown_pct"),
            "initial_equity": result.get("initial_equity"),
            "final_equity": result.get("final_equity"),
            "selection_count_by_symbol": result.get("selection_count_by_symbol", {}),
        },
        "by_symbol": by_symbol,
        "by_exit_reason": by_exit,
        "by_entry_regime": {name: _metrics(group) for name, group in sorted(regime_groups.items())},
        "buy_and_hold_benchmark": _benchmark(normalized, cfg, start, end),
        "trade_records": annotated,
        "methodology": {
            "regime_definition": "uptrend only when fast EMA > slow EMA and slow EMA is above its value 5 closed bars earlier; otherwise sideways_or_non_uptrend",
            "regime_timestamp": "signal_ts (previous completed candle); no future candles used",
            "benchmark_window": {"start_ts": ts[start], "end_ts": ts[end], "start_index": start, "end_index": end},
            "portfolio_constraint": "inherited from PortfolioBacktester; maximum one concurrent position",
            "orders_submitted": 0,
        },
        "limitations": [
            "Regime labels are descriptive and do not prove causation.",
            "Buy-and-hold is equal-weight and not risk-matched to the active strategy.",
            "Results depend on OHLCV candle assumptions; intrabar ordering is approximated conservatively with stop-first priority.",
            "The report does not establish future profitability or readiness for Demo order execution.",
        ],
    }
    if sensitivity:
        # Exploratory parameter sensitivity only on the first 70% of the post-warmup
        # interval. The final 30% is deliberately untouched and not used to rank or
        # validate candidates in this command.
        warm = max(cfg.slow_ema, 0)
        usable = len(ts) - warm
        research_end = warm + max(10, int(usable * 0.70)) - 1
        research_end = min(research_end, len(ts) - 2)
        pairs = [(f, sl) for f in (10, 20, 30) for sl in (40, 50, 80) if f < sl]
        rows = []
        for fast_period, slow_period in pairs:
            candidate_start = max(slow_period, warm)
            if research_end - candidate_start < 2:
                rows.append({"fast_ema": fast_period, "slow_ema": slow_period,
                             "status": "skipped_insufficient_research_bars"})
                continue
            candidate = replace(cfg, fast_ema=fast_period, slow_ema=slow_period)
            tested = PortfolioBacktester(candidate).run(normalized, start_index=candidate_start, end_index=research_end)
            rows.append({"fast_ema": fast_period, "slow_ema": slow_period, "status": "evaluated",
                         "net_return_pct": tested["net_return_pct"],
                         "max_drawdown_pct": tested["max_drawdown_pct"],
                         "trade_count": tested["trade_count"],
                         "profit_factor": tested["profit_factor"]})
        report["exploratory_ema_sensitivity"] = {
            "research_end_ts": ts[research_end],
            "reserved_untouched_period_start_ts": ts[research_end + 1],
            "results": rows,
            "warning": "Exploratory research only. No best parameter is selected; the reserved final 30% is not evaluated here and must remain untouched until a candidate and protocol are frozen.",
        }
    return report


def export_trade_records(path: str | Path, records: list[dict]) -> None:
    fields = ["symbol", "signal_ts", "entry_ts", "exit_ts", "entry_price", "exit_price",
              "quantity", "stop_loss", "take_profit", "entry_fee", "exit_fee", "net_pnl",
              "exit_reason", "bars_held", "entry_regime_at_signal"]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
