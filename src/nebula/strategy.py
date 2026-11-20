from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    action: str
    reason: str
    candle_ts: int


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return []
    alpha = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for value in values[period:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def closed_candles(raw_candles: list[list[str]]) -> list[list[str]]:
    """OKX candle rows are newest-first; confirm flag '1' means closed."""
    return [row for row in reversed(raw_candles) if len(row) > 8 and row[8] == "1"]


def trend_signal(candles: list[list[str]], fast: int = 20, slow: int = 50) -> Signal:
    rows = closed_candles(candles)
    closes = [float(row[4]) for row in rows]
    if len(closes) < slow + 1:
        return Signal("hold", "insufficient_closed_candles", int(rows[-1][0]) if rows else 0)
    fast_ema, slow_ema = ema(closes, fast), ema(closes, slow)
    # Align EMA series by their final timestamps; only completed bars are supplied.
    if fast_ema[-1] > slow_ema[-1] and fast_ema[-2] <= slow_ema[-2]:
        action, reason = "buy", "ema_bullish_cross"
    elif fast_ema[-1] < slow_ema[-1] and fast_ema[-2] >= slow_ema[-2]:
        action, reason = "sell", "ema_bearish_cross"
    else:
        action, reason = "hold", "no_crossover"
    return Signal(action, reason, int(rows[-1][0]))
