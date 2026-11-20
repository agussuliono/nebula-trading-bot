from nebula.state import StateStore


def test_intent_is_idempotent(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    cid = store.make_client_order_id("ema", "BTC-USDT", 12345, "buy")
    assert store.reserve_intent(cid, "BTC-USDT", 12345, "buy") is True
    assert store.reserve_intent(cid, "BTC-USDT", 12345, "buy") is False



def test_rejected_entry_lock_is_not_released_for_tiny_decimal_partial_fill(tmp_path):
    store = StateStore(str(tmp_path / "tiny-fill.db"))
    store.reserve_entry_intent("NBENTRY1", "BTC-USDT", 123, "buy")
    store.save_order_snapshot("NBENTRY1", {
        "ordId": "10001",
        "state": "canceled",
        "sz": "1",
        "accFillSz": "1e-500",
        "avgPx": "50000",
        "fee": "0",
        "feeCcy": "USDT",
    })
    store.update_intent("NBENTRY1", "rejected", "10001")

    assert store.release_rejected_entry_lock("NBENTRY1") is False
    assert store.get_position_lock()["owner_client_order_id"] == "NBENTRY1"


def test_rejected_zero_fill_entry_lock_can_still_be_released(tmp_path):
    store = StateStore(str(tmp_path / "zero-fill.db"))
    store.reserve_entry_intent("NBENTRY1", "BTC-USDT", 123, "buy")
    store.save_order_snapshot("NBENTRY1", {
        "ordId": "10001",
        "state": "canceled",
        "sz": "1",
        "accFillSz": "0",
        "avgPx": "0",
        "fee": "0",
        "feeCcy": "USDT",
    })
    store.update_intent("NBENTRY1", "rejected", "10001")

    assert store.release_rejected_entry_lock("NBENTRY1") is True
    assert store.get_position_lock() is None
