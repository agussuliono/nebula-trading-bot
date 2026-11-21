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


def _order_snapshot(**overrides):
    row = {
        "ordId": "1", "clOrdId": "NB1", "instId": "BTC-USDT",
        "instType": "SPOT", "tdMode": "cash", "side": "buy",
        "state": "partially_filled", "sz": "1",
        "accFillSz": "0.4", "avgPx": "100", "fee": "-0.02",
        "feeCcy": "USDT",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("field,value", [
    ("clOrdId", "NBOTHER"),
    ("instId", "ETH-USDT"),
    ("instType", "MARGIN"),
    ("tdMode", "cross"),
    ("side", "sell"),
])
def test_order_snapshot_must_match_persisted_intent_identity(tmp_path, field, value):
    store = setup(tmp_path)
    with pytest.raises(ValueError, match=field):
        store.save_order_snapshot("NB1", _order_snapshot(**{field: value}))


def test_order_snapshot_order_id_and_requested_size_are_immutable(tmp_path):
    store = setup(tmp_path)
    store.save_order_snapshot("NB1", _order_snapshot())
    with pytest.raises(ValueError, match="order ID changed"):
        store.save_order_snapshot("NB1", _order_snapshot(ordId="2"))
    with pytest.raises(ValueError, match="requested order size changed"):
        store.save_order_snapshot("NB1", _order_snapshot(sz="2"))


def test_order_snapshot_terminal_state_cannot_regress(tmp_path):
    store = setup(tmp_path)
    store.save_order_snapshot("NB1", _order_snapshot(
        state="filled", accFillSz="1", avgPx="100",
    ))
    with pytest.raises(ValueError, match="terminal state"):
        store.save_order_snapshot("NB1", _order_snapshot(
            state="partially_filled", accFillSz="1",
        ))


def test_order_snapshot_allows_partial_fill_to_terminal_cancel(tmp_path):
    store = setup(tmp_path)
    store.save_order_snapshot("NB1", _order_snapshot())
    row = store.save_order_snapshot("NB1", _order_snapshot(state="canceled"))
    assert row["exchange_state"] == "canceled"
    assert row["cumulative_fill_size"] == "0.4"
