"""Deterministic, fee/slippage-aware OHLCV backtesting for NEBULA V1.

Signals use confirmed candles only; entries occur at the next candle open.
When stop and target are both touched in one candle, stop is assumed first.
This module is offline and has no exchange/order-manager dependency.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class BacktestError(ValueError):
    pass


def _num(value, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BacktestError(f"Invalid {field}") from exc
    if not math.isfinite(result):
        raise BacktestError(f"Non-finite {field}")
    return result


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    confirmed: bool = True

    @classmethod
    def from_okx(cls, row: list[str]) -> "Candle":
        if not isinstance(row, (list, tuple)) or len(row) not in {8, 9}:
            raise BacktestError("OKX candle row must contain 8 or 9 fields")
        return cls(int(row[0]), *(_num(row[i], ("open", "high", "low", "close")[i-1]) for i in range(1, 5)),
                   _num(row[7] if len(row) == 9 else row[6], "quote volume"), str(row[-1]) == "1")


def validate_candles(rows: Iterable[Candle]) -> list[Candle]:
    candles = list(rows)
    if len(candles) < 60:
        raise BacktestError("At least 60 candles are required for EMA warm-up")
    previous = None
    out = []
    for c in candles:
        if (not isinstance(c, Candle) or isinstance(c.ts, bool)
                or not isinstance(c.ts, int) or c.ts <= 0 or not isinstance(c.confirmed, bool)):
            raise BacktestError("Invalid candle timestamp, confirmation flag, or type")
        values = (c.open, c.high, c.low, c.close, c.volume)
        if not all(math.isfinite(x) for x in values) or min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
            raise BacktestError("Invalid OHLCV values")
        if c.high < max(c.open, c.close, c.low) or c.low > min(c.open, c.close, c.high):
            raise BacktestError("Inconsistent OHLC range")
        if previous is not None and c.ts <= previous:
            raise BacktestError("Candles must be strictly ascending and unique")
        previous = c.ts
        if c.confirmed:
            out.append(c)
    if len(out) < 60:
        raise BacktestError("At least 60 confirmed candles are required")
    return out


def load_csv(path: str | Path) -> list[Candle]:
    p = Path(path)
    if not p.is_file():
        raise BacktestError("CSV data file does not exist")
    rows = []
    with p.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"ts", "open", "high", "low", "close"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise BacktestError("CSV requires columns: ts,open,high,low,close; optional volume,confirm")
        for row in reader:
            confirm = str(row.get("confirm", "1")).strip().lower()
            rows.append(Candle(int(row["ts"]), _num(row["open"], "open"), _num(row["high"], "high"),
                               _num(row["low"], "low"), _num(row["close"], "close"),
                               _num(row.get("volume") or 0, "volume"), confirm in {"1", "true", "yes"}))
    return validate_candles(rows)


def _net_stop_risk_per_unit(entry_price: float, stop_price: float,
                            fee_rate: float, slippage_rate: float) -> float:
    """Cash-at-risk per unit after entry/exit fees and adverse execution.

    ``entry_price`` is already the slippage-adjusted buy fill. Do not charge
    entry slippage again. The stop liquidation assumes an adverse sell fill
    and then applies the exit fee.
    """
    values = (entry_price, stop_price, fee_rate, slippage_rate)
    if not all(math.isfinite(x) for x in values):
        raise BacktestError("Risk inputs must be finite")
    if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
        raise BacktestError("Stop must be positive and below entry price")
    if not (0 <= fee_rate < 1 and 0 <= slippage_rate < 1):
        raise BacktestError("Fee and slippage rates must be in [0,1)")
    net_stop_proceeds = stop_price * (1.0 - slippage_rate) * (1.0 - fee_rate)
    entry_cash_outlay = entry_price * (1.0 + fee_rate)
    risk = entry_cash_outlay - net_stop_proceeds
    if not math.isfinite(risk) or risk <= 0:
        raise BacktestError("Calculated stop risk must be finite and positive")
    return risk


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    out = [math.nan] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: float = 1000.0
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.25
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    fee_bps: float = 10.0
    slippage_bps: float = 10.0
    fast_ema: int = 20
    slow_ema: int = 50
    max_drawdown_halt_pct: float = 0.20
    max_daily_loss_pct: float = 0.03

    def __post_init__(self):
        for name in ("initial_equity", "risk_per_trade_pct", "max_position_pct",
                     "stop_loss_pct", "take_profit_pct", "fee_bps", "slippage_bps",
                     "max_drawdown_halt_pct", "max_daily_loss_pct"):
            value = getattr(self, name)
            if isinstance(value, bool):
                raise BacktestError(f"{name} must be numeric, not boolean")
            object.__setattr__(self, name, _num(value, name))
        if any(x <= 0 for x in (self.initial_equity, self.risk_per_trade_pct,
                                self.max_position_pct, self.stop_loss_pct,
                                self.take_profit_pct)):
            raise BacktestError("Equity, risk, position cap, SL and TP must be positive")
        if isinstance(self.fast_ema, bool) or isinstance(self.slow_ema, bool) or not isinstance(self.fast_ema, int) or not isinstance(self.slow_ema, int):
            raise BacktestError("EMA periods must be integers")
        if self.risk_per_trade_pct > 0.05 or self.max_position_pct > 1:
            raise BacktestError("Risk per trade must be <=5% and position cap <=100%")
        if self.stop_loss_pct >= 1.0 or self.take_profit_pct > 5.0:
            raise BacktestError("Stop-loss must be below 100%; take-profit cannot exceed 500%")
        if self.take_profit_pct <= self.stop_loss_pct:
            raise BacktestError("Take-profit distance must exceed stop-loss distance")
        if not (0 <= self.fee_bps < 1000 and 0 <= self.slippage_bps < 1000):
            raise BacktestError("Fee/slippage bps outside supported conservative range")
        if not (0 < self.fast_ema < self.slow_ema <= 500):
            raise BacktestError("EMA periods must satisfy 0 < fast < slow <= 500")
        if not (0 < self.max_drawdown_halt_pct <= 1):
            raise BacktestError("max_drawdown_halt_pct must be in (0,1]")
        if not (0 < self.max_daily_loss_pct <= 0.5):
            raise BacktestError("max_daily_loss_pct must be in (0,0.5]")


class Backtester:
    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()

    def run(self, candles: Iterable[Candle], *, start_index: int | None = None,
            end_index: int | None = None) -> dict:
        data = validate_candles(candles)
        cfg = self.config
        start = max(cfg.slow_ema, start_index if start_index is not None else cfg.slow_ema)
        end = min(len(data) - 1, end_index if end_index is not None else len(data) - 1)
        if end - start < 2:
            raise BacktestError("Evaluation window is too short after EMA warm-up")
        closes = [c.close for c in data]
        fast, slow = _ema(closes, cfg.fast_ema), _ema(closes, cfg.slow_ema)
        cash = cfg.initial_equity
        position = None
        trades = []
        curve = []
        peak = cash
        max_dd = 0.0
        halted = False
        daily_halted = False
        daily_start_equity = cash
        current_day = None
        last_close_equity = cash
        fee_rate = cfg.fee_bps / 10000.0
        slip = cfg.slippage_bps / 10000.0
        pending_entry_signal = False

        def close_position(index: int, raw_price: float, reason: str):
            nonlocal cash, position
            # Sell execution suffers adverse slippage; fee is charged on proceeds.
            exit_px = raw_price * (1.0 - slip)
            proceeds = position["qty"] * exit_px
            exit_fee = proceeds * fee_rate
            cash += proceeds - exit_fee
            net = cash - position["cash_before_entry"]
            trades.append({"signal_ts": position["signal_ts"], "entry_ts": position["entry_ts"], "exit_ts": data[index].ts,
                           "entry_price": position["entry_px"], "exit_price": exit_px,
                           "quantity": position["qty"], "stop_loss": position["stop"],
                           "take_profit": position["target"], "entry_fee": position["entry_fee"],
                           "exit_fee": exit_fee, "net_pnl": net, "exit_reason": reason,
                           "bars_held": index - position["entry_index"]})
            position = None

        for i in range(start, end + 1):
            c = data[i]
            day = datetime.fromtimestamp(c.ts / 1000.0, tz=timezone.utc).date()
            if current_day != day:
                current_day = day
                daily_start_equity = last_close_equity
                daily_halted = False
            # First manage any open position using this candle's OHLC.
            if position is not None and position.get("exit_at_open"):
                position.pop("exit_at_open", None)
                close_position(i, c.open, "ema_bearish_cross")
            if position is not None:
                stop_hit = c.low <= position["stop"]
                target_hit = c.high >= position["target"]
                if stop_hit:
                    # Gap through stop is filled at the worse opening price.
                    raw = min(c.open, position["stop"])
                    close_position(i, raw, "stop_loss" if not target_hit else "stop_loss_same_bar_priority")
                elif target_hit:
                    close_position(i, position["target"], "take_profit")
            # Entry signal on candle i-1; execute at candle i open only.
            if position is None and pending_entry_signal and not halted and not daily_halted:
                raw_entry = c.open
                entry_px = raw_entry * (1.0 + slip)
                stop = entry_px * (1.0 - cfg.stop_loss_pct)
                target = entry_px * (1.0 + cfg.take_profit_pct)
                # Size against conservative stop loss, both-side fees and slippage.
                risk_per_unit = _net_stop_risk_per_unit(entry_px, stop, fee_rate, slip)
                risk_budget = cash * cfg.risk_per_trade_pct
                qty_by_risk = risk_budget / risk_per_unit
                qty_by_cap = (cash * cfg.max_position_pct) / (entry_px * (1.0 + fee_rate))
                qty = min(qty_by_risk, qty_by_cap)
                notional = qty * entry_px
                entry_fee = notional * fee_rate
                total_cost = notional + entry_fee
                if qty > 0 and total_cost <= cash:
                    before = cash
                    cash -= total_cost
                    position = {"qty": qty, "entry_px": entry_px, "stop": stop, "target": target,
                                "signal_ts": data[i - 1].ts, "entry_ts": c.ts, "entry_index": i, "entry_fee": entry_fee,
                                "cash_before_entry": before}
                    # The entry is at this candle's open, so its subsequent high/low
                    # must be checked for same-bar stop/target execution.
                    same_bar_stop = c.low <= stop
                    same_bar_target = c.high >= target
                    if same_bar_stop:
                        close_position(i, min(c.open, stop),
                                       "stop_loss_same_bar_priority" if same_bar_target else "stop_loss")
                    elif same_bar_target:
                        close_position(i, target, "take_profit")
            bullish_cross = (i < end and fast[i] > slow[i] and fast[i - 1] <= slow[i - 1])
            # Do not carry a signal formed on a candle that tripped the daily
            # loss circuit breaker into the next UTC day. The entry gate below
            # resets daily_halted at day rollover, so the pending signal itself
            # must be suppressed here.
            pending_entry_signal = bool(
                position is None and bullish_cross and not halted and not daily_halted
            )
            # A bearish cross detected at this close is acted on at the next open.
            if position is not None and i < end and fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]:
                position["exit_at_open"] = True
            # Mark open inventory at the candle low, net of estimated liquidation
            # slippage/fee, so close-only equity cannot hide intrabar drawdown.
            liquidation_factor = (1.0 - slip) * (1.0 - fee_rate)
            curve_equity = cash + (position["qty"] * c.low * liquidation_factor if position else 0.0)
            high_equity = cash + (position["qty"] * c.high * liquidation_factor if position else 0.0)
            peak = max(peak, high_equity)
            dd = (peak - curve_equity) / peak if peak else 0.0
            max_dd = max(max_dd, dd)
            if dd >= cfg.max_drawdown_halt_pct:
                halted = True
            if curve_equity <= daily_start_equity * (1.0 - cfg.max_daily_loss_pct):
                daily_halted = True
            last_close_equity = cash + (position["qty"] * c.close * liquidation_factor if position else 0.0)
            curve.append({"ts": c.ts, "equity": curve_equity, "drawdown_pct": dd * 100.0,
                          "daily_loss_halted": daily_halted})

        if position is not None:
            close_position(end, data[end].close, "end_of_test")
            final_dd = (peak - cash) / peak if peak else 0.0
            max_dd = max(max_dd, final_dd)
            curve.append({"ts": data[end].ts, "equity": cash,
                          "drawdown_pct": max_dd * 100.0,
                          "daily_loss_halted": daily_halted})
        final_equity = cash
        pnls = [t["net_pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss else (None if gross_profit else 0.0)
        return {
            "config": asdict(cfg), "bars_evaluated": end - start + 1,
            "initial_equity": cfg.initial_equity, "final_equity": final_equity,
            "net_return_pct": (final_equity / cfg.initial_equity - 1.0) * 100.0,
            "max_drawdown_pct": max_dd * 100.0, "trade_count": len(trades),
            "win_rate_pct": (len(wins) / len(trades) * 100.0) if trades else 0.0,
            "profit_factor": profit_factor, "profit_factor_unbounded": bool(gross_profit and not gross_loss),
            "gross_profit": gross_profit, "gross_loss": gross_loss,
            "halted_by_drawdown": halted, "daily_loss_halted_at_end": daily_halted,
            "trades": trades, "equity_curve": curve,
            "execution_model": "next_open_entry; conservative_stop_first; fees_and_slippage_both_sides",
            "orders_submitted": 0,
        }


def walk_forward(candles: Iterable[Candle], *, config: BacktestConfig | None = None,
                 folds: int = 3) -> dict:
    data = validate_candles(candles)
    cfg = config or BacktestConfig()
    if isinstance(folds, bool) or not isinstance(folds, int) or not 2 <= folds <= 10:
        raise BacktestError("folds must be an integer in 2..10")
    warmup = cfg.slow_ema + 1
    available = len(data) - warmup
    if available < folds * 10:
        raise BacktestError("Insufficient candles for requested walk-forward folds")
    fold_size = available // folds
    results = []
    for n in range(folds):
        test_start = warmup + n * fold_size
        test_end = (warmup + (n + 1) * fold_size - 1) if n < folds - 1 else len(data) - 1
        result = Backtester(cfg).run(data, start_index=test_start, end_index=test_end)
        results.append({"fold": n + 1, "train_history_bars": test_start,
                        "test_start_ts": data[test_start].ts, "test_end_ts": data[test_end].ts,
                        "test_bars": test_end - test_start + 1,
                        "net_return_pct": result["net_return_pct"],
                        "max_drawdown_pct": result["max_drawdown_pct"],
                        "trade_count": result["trade_count"],
                        "profit_factor": result["profit_factor"]})
    returns = [x["net_return_pct"] for x in results]
    return {"folds": results, "fold_count": folds,
            "mean_oos_return_pct": sum(returns) / len(returns),
            "positive_oos_folds": sum(1 for x in returns if x > 0),
            "orders_submitted": 0,
            "warning": "Walk-forward results are historical simulations, not a guarantee of future performance."}


def stress_test(candles: Iterable[Candle], *, config: BacktestConfig | None = None) -> dict:
    """Re-run identical signals under progressively worse transaction costs."""
    base = config or BacktestConfig()
    scenarios = [
        ("base", base.fee_bps, base.slippage_bps),
        ("moderate_cost", min(999.0, base.fee_bps * 1.5), min(999.0, base.slippage_bps * 1.5)),
        ("high_slippage", base.fee_bps, min(999.0, base.slippage_bps * 2.0)),
        ("severe_cost", min(999.0, base.fee_bps * 2.0), min(999.0, base.slippage_bps * 2.0)),
    ]
    reports = []
    data = validate_candles(candles)
    for name, fee, slippage in scenarios:
        cfg = BacktestConfig(**{**asdict(base), "fee_bps": fee, "slippage_bps": slippage})
        result = Backtester(cfg).run(data)
        reports.append({"scenario": name, "fee_bps": fee, "slippage_bps": slippage,
                        "net_return_pct": result["net_return_pct"],
                        "max_drawdown_pct": result["max_drawdown_pct"],
                        "trade_count": result["trade_count"],
                        "profit_factor": result["profit_factor"]})
    return {"scenarios": reports, "scenario_count": len(reports), "orders_submitted": 0,
            "warning": "Cost stress scenarios are assumptions, not observed execution costs."}


class PortfolioBacktester:
    """Synchronized multi-symbol backtest with one shared cash balance and one slot.

    Inputs must share an identical timestamp grid (same bar size and session).
    At each bar open, only signals confirmed on the previous bar are eligible.
    If multiple symbols qualify, the strongest positive EMA separation wins;
    ties are broken by symbol for deterministic results.
    """

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()

    def run(self, markets: dict[str, Iterable[Candle]], *, start_index: int | None = None,
            end_index: int | None = None) -> dict:
        if not isinstance(markets, dict) or len(markets) < 2:
            raise BacktestError("Portfolio backtest requires at least two symbol datasets")
        normalized: dict[str, list[Candle]] = {}
        for raw_symbol, rows in markets.items():
            symbol = str(raw_symbol).strip().upper()
            if not symbol or symbol in normalized or len(symbol) > 40:
                raise BacktestError("Invalid or duplicate portfolio symbol")
            normalized[symbol] = validate_candles(rows)
        symbols = sorted(normalized)
        reference_ts = [c.ts for c in normalized[symbols[0]]]
        if any([c.ts for c in normalized[s]] != reference_ts for s in symbols[1:]):
            raise BacktestError("All symbols must have identical ascending candle timestamps")

        cfg = self.config
        n = len(reference_ts)
        start = max(cfg.slow_ema, start_index if start_index is not None else cfg.slow_ema)
        end = min(n - 1, end_index if end_index is not None else n - 1)
        if end - start < 2:
            raise BacktestError("Insufficient synchronized candles after EMA warm-up")
        fast = {s: _ema([c.close for c in normalized[s]], cfg.fast_ema) for s in symbols}
        slow = {s: _ema([c.close for c in normalized[s]], cfg.slow_ema) for s in symbols}
        cash = cfg.initial_equity
        position = None
        trades: list[dict] = []
        curve: list[dict] = []
        peak = cash
        max_dd = 0.0
        halted = False
        daily_halted = False
        current_day = None
        daily_start_equity = cash
        last_equity = cash
        fee = cfg.fee_bps / 10000.0
        slip = cfg.slippage_bps / 10000.0
        selected_counts = {s: 0 for s in symbols}
        # Remember whether the previous completed candle ended in a daily halt.
        # This prevents a signal generated during a halted day from being traded
        # immediately after the UTC-day reset.
        previous_close_daily_halted = False

        def close_position(i: int, raw_price: float, reason: str):
            nonlocal cash, position
            px = raw_price * (1.0 - slip)
            proceeds = position["qty"] * px
            exit_fee = proceeds * fee
            cash += proceeds - exit_fee
            net = cash - position["cash_before"]
            trades.append({
                "symbol": position["symbol"], "signal_ts": position["signal_ts"],
                "entry_ts": position["entry_ts"], "exit_ts": normalized[position["symbol"]][i].ts,
                "entry_price": position["entry_px"], "exit_price": px,
                "quantity": position["qty"], "stop_loss": position["stop"],
                "take_profit": position["target"], "entry_fee": position["entry_fee"],
                "exit_fee": exit_fee, "net_pnl": net, "exit_reason": reason,
                "bars_held": i - position["entry_index"],
            })
            position = None

        for i in range(start, end + 1):
            signal_bar_was_daily_halted = previous_close_daily_halted
            # A position stopped/targeted intrabar cannot be replaced retroactively
            # at that same bar open. Re-entry is allowed only if flat at the open.
            slot_available_at_open = position is None
            ts = reference_ts[i]
            day = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).date()
            if day != current_day:
                current_day = day
                daily_start_equity = last_equity
                daily_halted = False

            if position is not None and position.get("exit_at_open"):
                position.pop("exit_at_open", None)
                close_position(i, normalized[position["symbol"]][i].open, "ema_bearish_cross")
                slot_available_at_open = True

            if position is not None:
                c = normalized[position["symbol"]][i]
                stop_hit = c.low <= position["stop"]
                target_hit = c.high >= position["target"]
                if stop_hit:
                    close_position(i, min(c.open, position["stop"]),
                                   "stop_loss_same_bar_priority" if target_hit else "stop_loss")
                elif target_hit:
                    close_position(i, position["target"], "take_profit")

            # Rank all confirmed bullish crosses from the previous completed bar.
            if (position is None and slot_available_at_open and not halted
                    and not daily_halted and not signal_bar_was_daily_halted and i > start):
                candidates = []
                signal_i = i - 1
                for symbol in symbols:
                    f, sl = fast[symbol], slow[symbol]
                    if (math.isfinite(f[signal_i]) and math.isfinite(sl[signal_i])
                            and math.isfinite(f[signal_i - 1]) and math.isfinite(sl[signal_i - 1])
                            and f[signal_i] > sl[signal_i] and f[signal_i - 1] <= sl[signal_i - 1]):
                        strength = (f[signal_i] / sl[signal_i]) - 1.0
                        candidates.append((strength, symbol))
                if candidates:
                    # Highest EMA separation wins; stable symbol tie-break.
                    _, chosen = sorted(candidates, key=lambda x: (-x[0], x[1]))[0]
                    c = normalized[chosen][i]
                    entry_px = c.open * (1.0 + slip)
                    stop = entry_px * (1.0 - cfg.stop_loss_pct)
                    target = entry_px * (1.0 + cfg.take_profit_pct)
                    risk_per_unit = _net_stop_risk_per_unit(entry_px, stop, fee, slip)
                    risk_budget = cash * cfg.risk_per_trade_pct
                    qty_risk = risk_budget / risk_per_unit
                    qty_cap = cash * cfg.max_position_pct / (entry_px * (1.0 + fee))
                    qty = min(qty_risk, qty_cap)
                    notional = qty * entry_px
                    entry_fee = notional * fee
                    total_cost = notional + entry_fee
                    if qty > 0 and total_cost <= cash:
                        before = cash
                        cash -= total_cost
                        position = {"symbol": chosen, "qty": qty, "entry_px": entry_px,
                                    "stop": stop, "target": target, "signal_ts": reference_ts[signal_i],
                                    "entry_ts": ts, "entry_index": i, "entry_fee": entry_fee,
                                    "cash_before": before}
                        selected_counts[chosen] += 1
                        same_stop = c.low <= stop
                        same_target = c.high >= target
                        if same_stop:
                            close_position(i, min(c.open, stop),
                                           "stop_loss_same_bar_priority" if same_target else "stop_loss")
                        elif same_target:
                            close_position(i, target, "take_profit")

            # Bearish cross is observed at this close and executed next bar open.
            if position is not None:
                symbol = position["symbol"]
                f, sl = fast[symbol], slow[symbol]
                if (i < end and f[i] < sl[i] and f[i - 1] >= sl[i - 1]):
                    position["exit_at_open"] = True

            # Conservative portfolio mark: use the held symbol's low.
            if position:
                c = normalized[position["symbol"]][i]
                equity = cash + position["qty"] * c.low * (1.0 - slip) * (1.0 - fee)
                high_equity = cash + position["qty"] * c.high * (1.0 - slip) * (1.0 - fee)
                close_equity = cash + position["qty"] * c.close * (1.0 - slip) * (1.0 - fee)
            else:
                equity = high_equity = close_equity = cash
            peak = max(peak, high_equity)
            dd = (peak - equity) / peak if peak else 0.0
            max_dd = max(max_dd, dd)
            if dd >= cfg.max_drawdown_halt_pct:
                halted = True
            if equity <= daily_start_equity * (1.0 - cfg.max_daily_loss_pct):
                daily_halted = True
            last_equity = close_equity
            previous_close_daily_halted = daily_halted
            curve.append({"ts": ts, "equity": equity, "drawdown_pct": dd * 100.0,
                          "daily_loss_halted": daily_halted,
                          "active_symbol": position["symbol"] if position else None})

        if position is not None:
            close_position(end, normalized[position["symbol"]][end].close, "end_of_test")
            final_dd = (peak - cash) / peak if peak else 0.0
            max_dd = max(max_dd, final_dd)
            curve.append({"ts": reference_ts[end], "equity": cash,
                          "drawdown_pct": max_dd * 100.0, "active_symbol": None,
                          "daily_loss_halted": daily_halted})
        pnls = [t["net_pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_profit, gross_loss = sum(wins), abs(sum(losses))
        pf = gross_profit / gross_loss if gross_loss else (None if gross_profit else 0.0)
        return {
            "symbols": symbols, "bars_evaluated": end - start + 1,
            "initial_equity": cfg.initial_equity, "final_equity": cash,
            "net_return_pct": (cash / cfg.initial_equity - 1.0) * 100.0,
            "max_drawdown_pct": max_dd * 100.0, "trade_count": len(trades),
            "win_rate_pct": len(wins) / len(trades) * 100.0 if trades else 0.0,
            "profit_factor": pf, "gross_profit": gross_profit, "gross_loss": gross_loss,
            "halted_by_drawdown": halted, "daily_loss_halted_at_end": daily_halted,
            "selection_count_by_symbol": selected_counts, "trades": trades,
            "equity_curve": curve, "max_concurrent_positions": 1,
            "selection_rule": "strongest_positive_ema_separation; alphabetical_tiebreak",
            "execution_model": "shared_cash; synchronized_bars; previous_closed_candle_signal; next_open_entry; conservative_stop_first; fees_and_slippage_both_sides",
            "orders_submitted": 0,
        }


def portfolio_walk_forward(markets: dict[str, Iterable[Candle]], *,
                            config: BacktestConfig | None = None, folds: int = 3) -> dict:
    cfg = config or BacktestConfig()
    normalized = {}
    for raw_symbol, rows in markets.items():
        symbol = str(raw_symbol).strip().upper()
        if not symbol or symbol in normalized or len(symbol) > 40:
            raise BacktestError("Invalid or duplicate portfolio symbol")
        normalized[symbol] = validate_candles(rows)
    if len(normalized) < 2:
        raise BacktestError("Portfolio walk-forward requires at least two symbols")
    timestamps = [c.ts for c in next(iter(normalized.values()))]
    if any([c.ts for c in rows] != timestamps for rows in normalized.values()):
        raise BacktestError("All symbols must have identical ascending candle timestamps")
    if isinstance(folds, bool) or not isinstance(folds, int) or not 2 <= folds <= 10:
        raise BacktestError("folds must be an integer in 2..10")
    warmup = cfg.slow_ema + 1
    available = len(timestamps) - warmup
    if available < folds * 10:
        raise BacktestError("Insufficient synchronized candles for requested folds")
    fold_size = available // folds
    reports = []
    for fold in range(folds):
        first = warmup + fold * fold_size
        last = warmup + (fold + 1) * fold_size - 1 if fold < folds - 1 else len(timestamps) - 1
        result = PortfolioBacktester(cfg).run(normalized, start_index=first, end_index=last)
        reports.append({"fold": fold + 1, "test_start_ts": timestamps[first],
                        "test_end_ts": timestamps[last], "test_bars": last - first + 1,
                        "net_return_pct": result["net_return_pct"],
                        "max_drawdown_pct": result["max_drawdown_pct"],
                        "trade_count": result["trade_count"],
                        "selection_count_by_symbol": result["selection_count_by_symbol"]})
    returns = [r["net_return_pct"] for r in reports]
    return {"folds": reports, "fold_count": folds,
            "mean_oos_return_pct": sum(returns) / len(returns),
            "positive_oos_folds": sum(1 for x in returns if x > 0),
            "orders_submitted": 0,
            "warning": "Out-of-sample portfolio simulations are historical estimates, not guarantees."}


def portfolio_stress_test(markets: dict[str, Iterable[Candle]], *,
                           config: BacktestConfig | None = None) -> dict:
    base = config or BacktestConfig()
    scenarios = [("base", base.fee_bps, base.slippage_bps),
                 ("moderate_cost", min(999.0, base.fee_bps * 1.5), min(999.0, base.slippage_bps * 1.5)),
                 ("high_slippage", base.fee_bps, min(999.0, base.slippage_bps * 2.0)),
                 ("severe_cost", min(999.0, base.fee_bps * 2.0), min(999.0, base.slippage_bps * 2.0))]
    # Materialize iterables once so generators can be replayed across scenarios.
    cached = {}
    for raw_symbol, rows in markets.items():
        symbol = str(raw_symbol).strip().upper()
        if not symbol or symbol in cached or len(symbol) > 40:
            raise BacktestError("Invalid or duplicate portfolio symbol")
        cached[symbol] = validate_candles(rows)
    reports = []
    for name, fee_bps, slippage_bps in scenarios:
        cfg = BacktestConfig(**{**asdict(base), "fee_bps": fee_bps, "slippage_bps": slippage_bps})
        result = PortfolioBacktester(cfg).run(cached)
        reports.append({"scenario": name, "fee_bps": fee_bps, "slippage_bps": slippage_bps,
                        "net_return_pct": result["net_return_pct"],
                        "max_drawdown_pct": result["max_drawdown_pct"],
                        "trade_count": result["trade_count"]})
    return {"scenarios": reports, "scenario_count": len(reports),
            "orders_submitted": 0,
            "warning": "Cost stress scenarios are assumptions, not observed execution costs."}
