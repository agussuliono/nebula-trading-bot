import pytest
from nebula.state import StateStore


def setup(tmp_path):
    store = StateStore(str(tmp_path / "fills.db"))
    store.reserve_intent("NB1", "BTC-USDT", 1, "buy")
    return store


def test_cumulative_fill_snapshot_is_persisted(tmp_path):
    store = setup(tmp_path)
    row = store.save_order_snapshot("NB1", {"ordId": "1", "state": "partially_filled", "sz": "1",
        "accFillSz": "0.4", "avgPx": "100", "fee": "-0.02", "feeCcy": "USDT"})
    assert row["cumulative_fill_size"] == "0.4"
    assert row["fee"] == "-0.02"


def test_fill_snapshot_cannot_regress(tmp_path):
    store = setup(tmp_path)
    store.save_order_snapshot("NB1", {"ordId": "1", "state": "partially_filled", "sz": "1",
        "accFillSz": "0.4", "avgPx": "100"})
    with pytest.raises(ValueError, match="regressed"):
        store.save_order_snapshot("NB1", {"ordId": "1", "state": "partially_filled", "sz": "1",
            "accFillSz": "0.3", "avgPx": "100"})


def test_fill_snapshot_cannot_exceed_order_size(tmp_path):
    store = setup(tmp_path)
    with pytest.raises(ValueError, match="exceeds"):
        store.save_order_snapshot("NB1", {"ordId": "1", "state": "filled", "sz": "1",
            "accFillSz": "1.1", "avgPx": "100"})
