import csv
import hashlib
import json

import pytest

from nebula.backtest import BacktestError, Candle
from nebula.history import audit_dataset, fetch_history, save_dataset


def row(ts, confirm="1", close="10", fields=9):
    values = [str(ts), close, str(float(close) + 1), str(float(close) - 1), close, "2", "20"]
    if fields == 9:
        values.append("20")
    values.append(confirm)
    return values


class FakeHistoryClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_history_candles(self, symbol, bar="1H", limit=100, *, after=None, before=None):
        self.calls.append((symbol, bar, limit, after))
        return self.pages.pop(0) if self.pages else []


def test_fetch_history_normalizes_deduplicates_and_skips_unconfirmed():
    # First page newest-first, overlapping timestamp in page two is deduplicated.
    base = 1_700_000_000_000
    first = [row(base + ts * 3_600_000, "0" if ts == 61 else "1", fields=8) for ts in range(120, 20, -1)]
    second = [row(base + ts * 3_600_000) for ts in range(21, 0, -1)]
    client = FakeHistoryClient([first, second])
    candles, meta = fetch_history(client, "btc-usdt", "1H", 100, sleep_seconds=0)
    assert len(candles) == 100
    assert [c.ts for c in candles] == sorted(c.ts for c in candles)
    assert all(c.confirmed for c in candles)
    assert meta["pages_fetched"] == 2
    assert meta["incomplete_rows_skipped"] == 1
    assert client.calls[0] == ("BTC-USDT", "1H", 100, None)
    assert client.calls[1][3] == str(base + 21 * 3_600_000)


def test_fetch_rejects_nonprogressing_cursor():
    page = [row(1_700_000_000_000 + ts * 3_600_000) for ts in range(100, 0, -1)]
    client = FakeHistoryClient([page, page])
    with pytest.raises(BacktestError, match="did not move backwards"):
        fetch_history(client, "BTC-USDT", "1H", 150, sleep_seconds=0)


def test_fetch_rejects_non_usdt_and_bad_count():
    client = FakeHistoryClient([])
    with pytest.raises(BacktestError):
        fetch_history(client, "BTC-USDC", "1H", 100, sleep_seconds=0)
    with pytest.raises(BacktestError):
        fetch_history(client, "BTC-USDT", "1H", 10, sleep_seconds=0)


def test_save_manifest_hash_and_audit_gaps(tmp_path):
    candles = [Candle(1_700_000_000_000 + i * 3_600_000, 10, 11, 9, 10, 20, True) for i in range(1, 61)]
    # create a deliberate two-hour gap while keeping chronology valid
    candles[30:] = [Candle(c.ts + 3_600_000, c.open, c.high, c.low, c.close, c.volume, True)
                    for c in candles[30:]]
    path = tmp_path / "BTC.csv"
    manifest = save_dataset(path, "BTC-USDT", "1H", candles, {"pages_fetched": 1})
    assert manifest["csv_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    saved = json.loads((tmp_path / "BTC.csv.manifest.json").read_text())
    assert saved["symbol"] == "BTC-USDT"
    audit = audit_dataset(candles, "1H")
    assert audit["gap_count"] == 1
    assert audit["estimated_missing_bars"] == 1


def test_audit_rejects_wrong_interval():
    candles = [Candle(1_700_000_000_000 + i * 3_600_000, 10, 11, 9, 10, 20, True) for i in range(1, 61)]
    with pytest.raises(BacktestError, match="do not match"):
        audit_dataset(candles, "4H")


def test_fetch_rejects_conflicting_duplicate_timestamp():
    base = 1_700_000_000_000
    first = [row(base + ts * 3_600_000) for ts in range(120, 20, -1)]
    conflict = row(base + 120 * 3_600_000, close="99")
    second = [conflict] + [row(base + ts * 3_600_000) for ts in range(20, -1, -1)]
    client = FakeHistoryClient([first, second])
    with pytest.raises(BacktestError, match="Conflicting OHLC"):
        fetch_history(client, "BTC-USDT", "1H", 150, sleep_seconds=0)


def test_save_dataset_refuses_overwrite_without_explicit_flag(tmp_path):
    candles = [Candle(1_700_000_000_000 + i * 3_600_000, 10, 11, 9, 10, 20, True)
               for i in range(1, 61)]
    path = tmp_path / "BTC.csv"
    save_dataset(path, "BTC-USDT", "1H", candles, {})
    with pytest.raises(FileExistsError, match="already exists"):
        save_dataset(path, "BTC-USDT", "1H", candles, {})


def test_candle_from_okx_accepts_both_documented_row_shapes():
    assert Candle.from_okx(row(1_700_000_000_000, fields=8)).volume == 20
    assert Candle.from_okx(row(1_700_000_000_000, fields=9)).volume == 20


def test_save_manifest_canonical_fields_cannot_be_overridden_by_fetch_metadata(tmp_path):
    candles = [Candle(1_700_000_000_000 + i * 3_600_000, 10, 11, 9, 10, 20, True)
               for i in range(60)]
    path = tmp_path / "BTC.csv"
    manifest = save_dataset(path, "btc-usdt", "1H", candles, {
        "symbol": "FAKE-USDT", "bar": "1m", "source": "untrusted",
        "csv_sha256": "0" * 64, "candle_count": 1, "first_ts": 1,
        "last_ts": 2, "schema_version": 999, "pages_fetched": 1,
    })
    assert manifest["symbol"] == "BTC-USDT"
    assert manifest["bar"] == "1H"
    assert manifest["source"] == "OKX V5 GET /api/v5/market/history-candles"
    assert manifest["csv_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest["candle_count"] == 60
    assert manifest["first_ts"] == candles[0].ts
    assert manifest["last_ts"] == candles[-1].ts
    assert manifest["schema_version"] == 1
    assert manifest["pages_fetched"] == 1


def test_save_dataset_rejects_invalid_symbol_and_empty_candles_before_writing(tmp_path):
    path = tmp_path / "bad.csv"
    candles = [Candle(1_700_000_000_000 + i * 3_600_000, 10, 11, 9, 10, 20, True)
               for i in range(60)]
    with pytest.raises(BacktestError):
        save_dataset(path, "BTC-USDC", "1H", candles, {})
    with pytest.raises(BacktestError):
        save_dataset(path, "BTC-USDT", "1H", [], {})
    assert not path.exists()
    assert not (tmp_path / "bad.csv.manifest.json").exists()
