from nebula.state import StateStore


def test_intent_is_idempotent(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    cid = store.make_client_order_id("ema", "BTC-USDT", 12345, "buy")
    assert store.reserve_intent(cid, "BTC-USDT", 12345, "buy") is True
    assert store.reserve_intent(cid, "BTC-USDT", 12345, "buy") is False
