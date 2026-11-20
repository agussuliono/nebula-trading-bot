"""Closed-candle-only trend signal calculation.

This is a deterministic signal component, not evidence of a profitable strategy.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Signal:
    action: str
    reason: str
    candle_ts: int


def ema(values: list[float], period: int) -> list[float]:
    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise ValueError("period must be a positive integer")
    numbers = [float(value) for value in values]
    if any(not math.isfinite(value) or value <= 0 for value in numbers):
        raise ValueError("EMA inputs must be finite positive prices")
    if len(numbers) < period:
        return []
    alpha = 2 / (period + 1)
    out = [sum(numbers[:period]) / period]
    for value in numbers[period:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def closed_candles(raw_candles: list[list[str]]) -> list[list[str]]:
    """Validate newest-first OKX rows and return confirmed rows oldest-first."""
    if not isinstance(raw_candles, list):
        raise ValueError("candles must be a list")
    previous_ts = None
    validated = []
    for row in raw_candles:
        if not isinstance(row, (list, tuple)) or len(row) != 9:
            raise ValueError("Each OKX candle must contain exactly 9 fields")
        try:
            ts = int(row[0])
            values = [float(row[i]) for i in range(1, 5)]
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Candle timestamp and OHLC values must be numeric") from exc
        if ts <= 0 or (previous_ts is not None and ts >= previous_ts):
            raise ValueError("OKX candle rows must be strictly newest-first with unique timestamps")
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("Candle OHLC values must be finite and positive")
        open_px, high, low, close = values
        if high < max(open_px, close, low) or low > min(open_px, close, high):
            raise ValueError("Candle OHLC range is inconsistent")
        if str(row[8]) not in {"0", "1"}:
            raise ValueError("Candle confirmation flag must be 0 or 1")
        previous_ts = ts
        validated.append(list(row))
    return [row for row in reversed(validated) if row[8] == "1"]


def trend_signal(candles: list[list[str]], fast: int = 20, slow: int = 50) -> Signal:
    if (isinstance(fast, bool) or isinstance(slow, bool)
            or not isinstance(fast, int) or not isinstance(slow, int)
            or not 0 < fast < slow <= 500):
        raise ValueError("EMA periods must satisfy 0 < fast < slow <= 500")
    rows = closed_candles(candles)
    closes = [float(row[4]) for row in rows]
    if len(closes) < slow + 1:
        return Signal("hold", "insufficient_closed_candles", int(rows[-1][0]) if rows else 0)
    fast_ema, slow_ema = ema(closes, fast), ema(closes, slow)
    if fast_ema[-1] > slow_ema[-1] and fast_ema[-2] <= slow_ema[-2]:
        action, reason = "buy", "ema_bullish_cross"
    elif fast_ema[-1] < slow_ema[-1] and fast_ema[-2] >= slow_ema[-2]:
        action, reason = "sell", "ema_bearish_cross"
    else:
        action, reason = "hold", "no_crossover"
    return Signal(action, reason, int(rows[-1][0]))
