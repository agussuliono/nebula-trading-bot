"""Deterministic, fail-closed Spot universe filtering and ranking."""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re


SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}-USDT$")


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
    thresholds = (min_quote_volume, max_spread_pct, min_volatility_pct, max_volatility_pct)
    if not all(math.isfinite(x) for x in thresholds):
        raise ValueError("Scanner thresholds must be finite")
    if (min_quote_volume <= 0 or max_spread_pct < 0 or min_volatility_pct < 0
            or max_volatility_pct <= min_volatility_pct or min_listing_age_days < 0
            or min_data_points < 1):
        raise ValueError("Invalid scanner thresholds")
    eligible = []
    counts = {}
    for item in items:
        if isinstance(item, MarketCandidate):
            counts[item.inst_id] = counts.get(item.inst_id, 0) + 1
    for x in items:
        if not isinstance(x, MarketCandidate) or not SYMBOL_RE.fullmatch(str(x.inst_id)):
            continue
        if counts.get(x.inst_id, 0) != 1:
            # Duplicate instrument records are ambiguous; exclude the symbol.
            continue
        values = (x.quote_volume, x.spread_pct, x.volatility_pct)
        if (not all(math.isfinite(v) for v in values)
                or isinstance(x.listing_age_days, bool) or not isinstance(x.listing_age_days, int)
                or isinstance(x.data_points, bool) or not isinstance(x.data_points, int)):
            continue
        if (x.quote_volume >= min_quote_volume and 0 <= x.spread_pct <= max_spread_pct
                and min_volatility_pct <= x.volatility_pct <= max_volatility_pct
                and x.listing_age_days >= min_listing_age_days and x.data_points >= min_data_points):
            eligible.append(x)
    volatility_midpoint = (min_volatility_pct + max_volatility_pct) / 2.0
    return sorted(eligible, key=lambda x: (
        -x.quote_volume, x.spread_pct,
        abs(x.volatility_pct - volatility_midpoint),
        -x.listing_age_days, -x.data_points, x.inst_id,
    ))


def build_candidates(instruments: list[dict], tickers: list[dict], *, now_ms: int | None = None,
                     max_ticker_age_ms: int = 30_000) -> list[MarketCandidate]:
    """Build live USDT Spot candidates from fresh, unambiguous OKX snapshots."""
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
        raise ValueError("now_ms must be a positive integer")
    if (isinstance(max_ticker_age_ms, bool) or not isinstance(max_ticker_age_ms, int)
            or max_ticker_age_ms <= 0):
        raise ValueError("max_ticker_age_ms must be a positive integer")
    if not isinstance(instruments, list) or not isinstance(tickers, list):
        raise ValueError("instruments and tickers must be lists")

    ticker_groups = {}
    for row in tickers:
        if not isinstance(row, dict):
            continue
        inst_id = row.get("instId")
        if inst_id:
            ticker_groups.setdefault(str(inst_id), []).append(row)

    out = []
    instrument_groups = {}
    for inst in instruments:
        if isinstance(inst, dict) and inst.get("instId"):
            instrument_groups.setdefault(str(inst["instId"]), []).append(inst)
    for inst in instruments:
        if not isinstance(inst, dict):
            continue
        inst_id = str(inst.get("instId") or "")
        if not SYMBOL_RE.fullmatch(inst_id) or len(instrument_groups.get(inst_id, [])) != 1:
            continue
        matches = ticker_groups.get(inst_id, [])
        # Duplicate tickers are ambiguous; never select one arbitrarily.
        if len(matches) != 1:
            continue
        tick = matches[0]
        if inst.get("instType", "SPOT") != "SPOT" or inst.get("state") != "live" or inst.get("quoteCcy") != "USDT":
            continue
        try:
            bid = float(tick["bidPx"])
            ask = float(tick["askPx"])
            quote_volume = float(tick["volCcy24h"])
            bid_size = float(tick["bidSz"])
            ask_size = float(tick["askSz"])
            ticker_ts = int(tick["ts"])
            list_time = int(inst["listTime"])
            values = (bid, ask, quote_volume, bid_size, ask_size)
            if not all(math.isfinite(v) for v in values):
                continue
            if (min(bid, ask, quote_volume, bid_size, ask_size) <= 0 or ask < bid
                    or list_time <= 0 or list_time > now_ms
                    or ticker_ts <= 0 or ticker_ts > now_ms
                    or now_ms - ticker_ts > max_ticker_age_ms):
                continue
            spread_pct = ((ask - bid) / ((ask + bid) / 2)) * 100.0
            age_days = (now_ms - list_time) // 86_400_000
            if not math.isfinite(spread_pct):
                continue
            out.append(MarketCandidate(inst_id, quote_volume, spread_pct, 0.0,
                                       int(age_days), 0))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return out


def valid_traded_candle_count(candles: list[list[str]]) -> int:
    """Count confirmed candles with finite positive OHLCV suitable for analysis."""
    from .strategy import closed_candles
    count = 0
    for row in closed_candles(candles):
        try:
            open_px, high, low, close, volume = (float(row[i]) for i in range(1, 6))
            if (all(math.isfinite(v) and v > 0 for v in
                    (open_px, high, low, close, volume))
                    and high >= max(open_px, close, low)
                    and low <= min(open_px, close, high)):
                count += 1
        except (ValueError, TypeError, IndexError, OverflowError):
            continue
    return count


def candle_range_volatility(candles: list[list[str]], window: int = 24) -> tuple[float, int]:
    """Mean true range as % of close over confirmed, traded candles."""
    from .strategy import closed_candles
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("window must be a positive integer")
    valid = []
    previous_close = None
    for row in closed_candles(candles):
        try:
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
