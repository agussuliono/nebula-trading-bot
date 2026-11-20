"""Public OKX historical Spot candle collection and dataset quality checks.

Read-only market-data module. It never touches private account or order endpoints.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .backtest import BacktestError, Candle, validate_candles


BAR_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1H": 3_600_000, "2H": 7_200_000,
    "4H": 14_400_000, "1D": 86_400_000, "1Dutc": 86_400_000,
}
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}-USDT$")


def fetch_history(client, symbol: str, bar: str, candle_count: int, *, sleep_seconds: float = 0.11):
    """Fetch up to candle_count confirmed bars, paginating backwards safely."""
    symbol = str(symbol).strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise BacktestError("Only normalized USDT Spot symbols such as BTC-USDT are supported")
    if bar not in BAR_MS:
        raise BacktestError(f"Unsupported bar {bar}; supported: {', '.join(BAR_MS)}")
    if isinstance(candle_count, bool) or not isinstance(candle_count, int) or not 60 <= candle_count <= 100_000:
        raise BacktestError("candle_count must be an integer in 60..100000")
    if not (0 <= sleep_seconds <= 5):
        raise BacktestError("sleep_seconds must be in 0..5")

    by_ts: dict[int, list[str]] = {}
    cursor = None
    previous_oldest = None
    pages = 0
    max_pages = (candle_count + 99) // 100 + 3
    incomplete = 0
    while len(by_ts) < candle_count and pages < max_pages:
        rows = client.get_history_candles(symbol, bar=bar, limit=100, after=cursor)
        pages += 1
        if not isinstance(rows, list) or not rows:
            break
        parsed = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) not in {8, 9}:
                raise BacktestError("OKX returned malformed historical candle row")
            try:
                ts = int(row[0])
            except (TypeError, ValueError) as exc:
                raise BacktestError("OKX candle timestamp is invalid") from exc
            if ts <= 0:
                raise BacktestError("OKX candle timestamp must be positive")
            if str(row[-1]) != "1":
                incomplete += 1
                continue
            parsed.append((ts, list(row)))
        for ts, row in parsed:
            existing = by_ts.get(ts)
            if existing is not None and existing[:5] != row[:5]:
                raise BacktestError("Conflicting OHLC values for a duplicate candle timestamp")
            by_ts.setdefault(ts, row)
        oldest = min(int(row[0]) for row in rows)
        if previous_oldest is not None and oldest >= previous_oldest:
            raise BacktestError("History pagination did not move backwards; stopping to avoid duplicate loop")
        previous_oldest = oldest
        cursor = str(oldest)
        if len(rows) < 100:
            break
        if sleep_seconds:
            time.sleep(sleep_seconds)

    selected = sorted(by_ts.items())[-candle_count:]
    candles = []
    for _, row in selected:
        # Keep quote-volume (volCcyQuote) for USDT turnover comparability.
        candles.append(Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                              float(row[4]), float(row[7] if len(row) == 9 else row[6]), True))
    candles = validate_candles(candles)
    return candles, {"pages_fetched": pages, "incomplete_rows_skipped": incomplete,
                     "requested_candles": candle_count, "returned_candles": len(candles)}


def save_dataset(path: str | Path, symbol: str, bar: str, candles: list[Candle], fetch_meta: dict, *, overwrite: bool = False):
    """Persist a validated dataset and a provenance manifest.

    Canonical manifest fields are constructed after fetch metadata so callers
    cannot override symbol, source, timestamps, count, or the CSV digest.
    Both files are staged before either destination is replaced.
    """
    target = Path(path)
    normalized_symbol = str(symbol).strip().upper()
    if not SYMBOL_RE.fullmatch(normalized_symbol):
        raise BacktestError("Only normalized USDT Spot symbols such as BTC-USDT are supported")
    if bar not in BAR_MS:
        raise BacktestError(f"Unsupported bar {bar}")
    if not isinstance(fetch_meta, dict):
        raise BacktestError("fetch_meta must be a dictionary")
    data = validate_candles(candles)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = target.with_suffix(target.suffix + ".manifest.json")
    if not overwrite and (target.exists() or manifest_path.exists()):
        raise FileExistsError("Dataset or manifest already exists; choose another --output or pass --overwrite")

    import io
    import os
    import tempfile

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["ts", "open", "high", "low", "close", "volume", "confirm"])
    for c in data:
        writer.writerow([c.ts, repr(c.open), repr(c.high), repr(c.low), repr(c.close), repr(c.volume), "1"])
    csv_bytes = buffer.getvalue().encode("utf-8")
    digest = hashlib.sha256(csv_bytes).hexdigest()
    manifest = {
        **fetch_meta,
        "schema_version": 1,
        "symbol": normalized_symbol,
        "bar": bar,
        "source": "OKX V5 GET /api/v5/market/history-candles",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "first_ts": data[0].ts,
        "last_ts": data[-1].ts,
        "candle_count": len(data),
        "csv_sha256": digest,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    staged = []
    try:
        for destination, payload in ((target, csv_bytes), (manifest_path, manifest_bytes)):
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent,
                                             prefix=destination.name + ".", suffix=".tmp",
                                             delete=False) as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
                staged.append((Path(tmp.name), destination))
        # Stage complete: replace each file atomically on its own filesystem.
        for temporary, destination in staged:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in staged:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return manifest


def audit_dataset(candles: list[Candle], bar: str) -> dict:
    """Report gaps and basic OHLCV quality without silently repairing data."""
    if bar not in BAR_MS:
        raise BacktestError(f"Unsupported bar {bar}")
    data = validate_candles(candles)
    interval = BAR_MS[bar]
    gaps = []
    for left, right in zip(data, data[1:]):
        delta = right.ts - left.ts
        if delta > interval:
            missing = max(0, round(delta / interval) - 1)
            if missing:
                gaps.append({"after_ts": left.ts, "before_ts": right.ts,
                             "missing_bars_estimate": missing, "gap_ms": delta})
        elif delta < interval:
            raise BacktestError("Candle timestamps overlap or do not match the selected bar interval")
    volumes_zero = sum(1 for c in data if c.volume == 0)
    return {"bar": bar, "candle_count": len(data), "first_ts": data[0].ts,
            "last_ts": data[-1].ts, "estimated_missing_bars": sum(x["missing_bars_estimate"] for x in gaps),
            "gap_count": len(gaps), "zero_volume_bars": volumes_zero,
            "ohlc_validation": "passed", "chronology_validation": "passed",
            "gaps": gaps[:100], "warning": "Gaps are reported, not filled. Confirm listing start and exchange maintenance before interpreting gaps."}
