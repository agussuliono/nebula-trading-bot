from dataclasses import dataclass
from datetime import datetime, timezone
import math


@dataclass(frozen=True)
class MarketCandidate:
    inst_id: str
    quote_volume: float
    spread_pct: float
    volatility_pct: float
    listing_age_days: int
    data_points: int


def rank_candidates(items: list[MarketCandidate], *, min_quote_volume: float,
                    max_spread_pct: float, min_volatility_pct: float,
                    max_volatility_pct: float, min_listing_age_days: int,
                    min_data_points: int) -> list[MarketCandidate]:
    eligible = []
    for x in items:
        values = (x.quote_volume, x.spread_pct, x.volatility_pct)
        if not all(math.isfinite(v) for v in values):
            continue
        if (x.inst_id and x.quote_volume >= min_quote_volume and
            0 <= x.spread_pct <= max_spread_pct and
            min_volatility_pct <= x.volatility_pct <= max_volatility_pct and
            x.listing_age_days >= min_listing_age_days and
            x.data_points >= min_data_points):
            eligible.append(x)
    # Explicit, deterministic priority: liquidity first, then execution cost,
    # then prefer volatility near the middle of the accepted band, older listings,
    # and finally more complete candle history. Every configured selector input
    # therefore participates in ranking, not merely eligibility filtering.
    volatility_midpoint = (min_volatility_pct + max_volatility_pct) / 2.0
    return sorted(
        eligible,
        key=lambda x: (
            -x.quote_volume,
            x.spread_pct,
            abs(x.volatility_pct - volatility_midpoint),
            -x.listing_age_days,
            -x.data_points,
            x.inst_id,
        ),
    )


def build_candidates(instruments: list[dict], tickers: list[dict], *, now_ms: int | None = None) -> list[MarketCandidate]:
    """Normalize OKX SPOT instrument/ticker fields. Volatility is left for candle enrichment."""
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    by_ticker = {x.get("instId"): x for x in tickers if x.get("instId")}
    out = []
    for inst in instruments:
        inst_id = inst.get("instId", "")
        tick = by_ticker.get(inst_id)
        if not tick or inst.get("state") != "live" or inst.get("quoteCcy") != "USDT":
            continue
        try:
            bid, ask = float(tick["bidPx"]), float(tick["askPx"])
            quote_volume = float(tick["volCcy24h"])
            list_time = int(inst.get("listTime") or 0)
            if min(bid, ask, quote_volume) <= 0 or ask < bid or list_time <= 0:
                continue
            spread_pct = ((ask - bid) / ((ask + bid) / 2)) * 100
            age_days = max(0, (now_ms - list_time) // 86_400_000)
            out.append(MarketCandidate(inst_id, quote_volume, spread_pct, 0.0, int(age_days), 0))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return out


def candle_range_volatility(candles: list[list[str]], window: int = 24) -> tuple[float, int]:
    """Mean true range as % of close over confirmed, traded candles.

    OKX rows are newest-first. Zero-volume candles are excluded so no-trade
    bars cannot artificially suppress the volatility estimate. True range
    includes gaps relative to the previous valid close.
    """
    from .strategy import closed_candles
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("window must be a positive integer")

    valid = []
    previous_close = None
    for row in closed_candles(candles):
        try:
            if len(row) < 9:
                continue
            high, low, close = float(row[2]), float(row[3]), float(row[4])
            volume = float(row[5])
            if not all(math.isfinite(v) for v in (high, low, close, volume)):
                continue
            if close <= 0 or low <= 0 or high < low or volume <= 0:
                continue
            true_range = high - low
            if previous_close is not None:
                true_range = max(true_range, abs(high - previous_close), abs(low - previous_close))
            valid.append((true_range / close) * 100.0)
            previous_close = close
        except (ValueError, TypeError, IndexError, OverflowError):
            continue

    selected = valid[-window:]
    return (sum(selected) / len(selected), len(selected)) if selected else (0.0, 0)
