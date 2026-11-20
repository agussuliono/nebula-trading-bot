import hashlib
import json

import pytest

from nebula.backtest import BacktestError, Candle
from nebula.history import save_dataset
from nebula.prospective import PROSPECTIVE_CUTOFF_TS, append_prospective_candles


HOUR = 3_600_000


def existing_dataset(tmp_path, monkeypatch):
    rows = [Candle(PROSPECTIVE_CUTOFF_TS - (59 - i) * HOUR,
                   100, 102, 99, 101, 1000, True) for i in range(60)]
    path = tmp_path / "BTC.csv"
    save_dataset(path, "BTC-USDT", "1H", rows, {"research_role": "frozen_prior_sample"})
    from nebula import prospective
    monkeypatch.setitem(prospective.FROZEN_BASELINE_SHA256, "BTC-USDT", hashlib.sha256(path.read_bytes()).hexdigest())
    return path, rows


def test_append_prospective_single_confirmed_candle_and_manifest(tmp_path, monkeypatch):
    path, rows = existing_dataset(tmp_path, monkeypatch)
    new = Candle(PROSPECTIVE_CUTOFF_TS + HOUR, 101, 103, 100, 102, 1200, True)
    result = append_prospective_candles(path, "BTC-USDT", "1H", [new])
    saved = json.loads(path.with_suffix(".csv.manifest.json").read_text())
    assert result["appended"] == 1
    assert result["orders_submitted"] == 0
    assert result["private_api_calls"] == 0
    assert saved["collection_mode"] == "append_only_prospective"
    assert saved["frozen_cutoff_ts"] == PROSPECTIVE_CUTOFF_TS
    assert saved["last_ts"] == new.ts
    assert saved["candle_count"] == len(rows) + 1
    assert saved["csv_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("new_candles, message", [
    ([Candle(PROSPECTIVE_CUTOFF_TS, 101, 103, 100, 102, 100, True)], "overlaps"),
    ([Candle(PROSPECTIVE_CUTOFF_TS + 2 * HOUR, 101, 103, 100, 102, 100, True)], "contiguous"),
    ([Candle(PROSPECTIVE_CUTOFF_TS + HOUR, 101, 103, 100, 102, 100, False)], "confirmed"),
])
def test_append_rejects_overlap_gap_or_unconfirmed(tmp_path, monkeypatch, new_candles, message):
    path, _ = existing_dataset(tmp_path, monkeypatch)
    with pytest.raises(BacktestError, match=message):
        append_prospective_candles(path, "BTC-USDT", "1H", new_candles)


def test_append_rejects_manifest_hash_mismatch(tmp_path, monkeypatch):
    path, _ = existing_dataset(tmp_path, monkeypatch)
    path.write_text(path.read_text() + "\n")
    new = Candle(PROSPECTIVE_CUTOFF_TS + HOUR, 101, 103, 100, 102, 100, True)
    with pytest.raises(BacktestError, match="hash"):
        append_prospective_candles(path, "BTC-USDT", "1H", [new])


def test_append_rejects_wrong_symbol_and_missing_manifest(tmp_path, monkeypatch):
    path, _ = existing_dataset(tmp_path, monkeypatch)
    new = Candle(PROSPECTIVE_CUTOFF_TS + HOUR, 101, 103, 100, 102, 100, True)
    with pytest.raises(BacktestError, match="[Ss]ymbol"):
        append_prospective_candles(path, "DOGE-USDT", "1H", [new])
    path.with_suffix(".csv.manifest.json").unlink()
    with pytest.raises(FileNotFoundError):
        append_prospective_candles(path, "BTC-USDT", "1H", [new])
