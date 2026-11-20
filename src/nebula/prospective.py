"""Append-only collector for genuinely prospective OKX Spot candles.

This module uses public market data only. It never reads credentials or calls
private account/order endpoints. Existing research data is immutable.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .backtest import BacktestError, Candle, load_csv, validate_candles
from .history import BAR_MS, SYMBOL_RE


# Last timestamp in the previously inspected dataset (2026-11-20 01:00 UTC).
PROSPECTIVE_CUTOFF_TS = 1_795_136_400_000
FROZEN_BASELINE_SHA256 = {
    "BTC-USDT": "bb7b6fe47bf08a75947e5c7b23d7d2e24cea1bb73cd0c9c6c56d8fac54656aca",
    "ETH-USDT": "bf5555498c8d36d8d03c954cc075320cb835e78d696ebcc1624e1e83316ca3ee",
    "SOL-USDT": "ffb364c8ee77e11e88850f66762f5840db525054935e4f3b364821059065f739",
}


def append_prospective_candles(
    path: str | Path,
    symbol: str,
    bar: str,
    new_candles: list[Candle],
    *,
    cutoff_ts: int = PROSPECTIVE_CUTOFF_TS,
) -> dict:
    """Append only confirmed, contiguous candles after the frozen research cutoff.

    The existing CSV and manifest must agree exactly. Duplicate, overlapping,
    conflicting, stale, unconfirmed, or missing intervals fail closed.
    """
    target = Path(path)
    manifest_path = target.with_suffix(target.suffix + ".manifest.json")
    normalized = str(symbol).strip().upper()
    if not SYMBOL_RE.fullmatch(normalized) or bar != "1H":
        raise BacktestError("Prospective research currently supports only the frozen 1H basket")
    if isinstance(cutoff_ts, bool) or not isinstance(cutoff_ts, int) or cutoff_ts != PROSPECTIVE_CUTOFF_TS:
        raise BacktestError("The prospective cutoff is frozen and cannot be changed")
    if not target.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Existing CSV and manifest are both required")
    if not isinstance(new_candles, list) or not new_candles:
        raise BacktestError("No new confirmed candles supplied")
    if any(not isinstance(c, Candle) or c.confirmed is not True for c in new_candles):
        raise BacktestError("Prospective collector accepts confirmed candles only")

    existing = load_csv(target)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise BacktestError("Existing manifest must be a JSON object")
    digest_before = hashlib.sha256(target.read_bytes()).hexdigest()
    if manifest.get("csv_sha256") != digest_before:
        raise BacktestError("Existing CSV hash does not match its manifest")
    expected_baseline = FROZEN_BASELINE_SHA256.get(normalized)
    if expected_baseline is None:
        raise BacktestError("Symbol is not part of the frozen prospective research basket")
    if manifest.get("collection_mode") == "append_only_prospective":
        if (manifest.get("baseline_sha256") != expected_baseline
                or manifest.get("frozen_cutoff_ts") != cutoff_ts):
            raise BacktestError("Prospective manifest does not trace to the frozen baseline")
    elif digest_before != expected_baseline:
        raise BacktestError("Initial dataset is not the exact frozen research baseline")
    if (manifest.get("symbol") != normalized or manifest.get("bar") != bar
            or manifest.get("last_ts") != existing[-1].ts
            or manifest.get("first_ts") != existing[0].ts
            or manifest.get("candle_count") != len(existing)):
        raise BacktestError("Existing manifest provenance does not match the CSV")
    if existing[-1].ts < cutoff_ts:
        raise BacktestError("Existing dataset does not reach the frozen cutoff")
    if any(c.ts <= cutoff_ts for c in new_candles):
        raise BacktestError("New candle overlaps the frozen historical sample")

    additions = list(new_candles)
    interval = BAR_MS[bar]
    expected = existing[-1].ts + interval
    for candle in additions:
        if candle.ts != expected:
            raise BacktestError("Prospective candles must be contiguous, unique, and strictly newer")
        expected += interval

    combined = existing + additions
    # Validate the full series; the new batch itself may be a single candle.
    combined = validate_candles(combined)
    import io
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["ts", "open", "high", "low", "close", "volume", "confirm"])
    for c in combined:
        writer.writerow([c.ts, repr(c.open), repr(c.high), repr(c.low), repr(c.close), repr(c.volume), "1"])
    payload = buffer.getvalue().encode("utf-8")
    digest_after = hashlib.sha256(payload).hexdigest()
    updated = {
        **manifest,
        "collection_mode": "append_only_prospective",
        "prospective_source": "OKX V5 GET /api/v5/market/candles",
        "baseline_sha256": manifest.get("baseline_sha256", digest_before),
        "frozen_cutoff_ts": cutoff_ts,
        "last_collection_at_utc": datetime.now(timezone.utc).isoformat(),
        "appended_candle_count": int(manifest.get("appended_candle_count", 0)) + len(additions),
        "last_appended_first_ts": additions[0].ts,
        "last_appended_last_ts": additions[-1].ts,
        "last_appended_count": len(additions),
        "last_ts": combined[-1].ts,
        "candle_count": len(combined),
        "csv_sha256": digest_after,
    }
    manifest_payload = (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode("utf-8")

    staged = []
    try:
        for destination, content in ((target, payload), (manifest_path, manifest_payload)):
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent,
                                             prefix=destination.name + ".", suffix=".tmp",
                                             delete=False) as tmp:
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
                staged.append((Path(tmp.name), destination))
        for temporary, destination in staged:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in staged:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {
        "symbol": normalized, "bar": bar, "appended": len(additions),
        "first_appended_ts": additions[0].ts, "last_ts": combined[-1].ts,
        "candle_count": len(combined), "csv_sha256": digest_after,
        "orders_submitted": 0, "private_api_calls": 0,
    }
