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



def test_entry_risk_plan_is_persisted_with_single_position_reservation(tmp_path):
    store = StateStore(str(tmp_path / "risk-plan.db"))
    plan = {
        "instrument": "BTC-USDT",
        "size": "0.01",
        "entry_price": "50000",
        "stop_loss": "48000",
        "take_profit": "52000",
        "risk_to_stop_quote": "20",
    }

    assert store.reserve_entry_intent(
        "NBENTRY1", "BTC-USDT", 123, "buy", risk_plan=plan
    ) is True
    assert store.get_entry_risk_plan("NBENTRY1") == {
        "instrument": "BTC-USDT",
        "size": "0.01",
        "entry_price": "50000",
        "stop_loss": "48000",
        "take_profit": "52000",
        "risk_to_stop_quote": "20",
    }

    restarted = StateStore(str(tmp_path / "risk-plan.db"))
    assert restarted.get_entry_risk_plan("NBENTRY1") == store.get_entry_risk_plan("NBENTRY1")


def test_invalid_entry_risk_plan_does_not_reserve_position_slot(tmp_path):
    store = StateStore(str(tmp_path / "invalid-risk-plan.db"))
    invalid = {
        "instrument": "BTC-USDT",
        "size": "0.01",
        "entry_price": "50000",
        "stop_loss": "51000",
        "take_profit": "52000",
        "risk_to_stop_quote": "20",
    }

    import pytest
    with pytest.raises(ValueError, match="ordering"):
        store.reserve_entry_intent(
            "NBENTRY1", "BTC-USDT", 123, "buy", risk_plan=invalid
        )
    assert store.get_position_lock() is None


def test_legacy_intents_schema_migrates_risk_plan_column(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-risk-plan.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE intents (client_order_id TEXT PRIMARY KEY, "
            "instrument TEXT NOT NULL, candle_ts INTEGER NOT NULL, "
            "side TEXT NOT NULL, status TEXT NOT NULL, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    store = StateStore(str(path))
    assert store.reserve_entry_intent(
        "NBENTRY1", "BTC-USDT", 123, "buy",
        risk_plan={
            "instrument": "BTC-USDT", "size": "0.01",
            "entry_price": "50000", "stop_loss": "48000",
            "take_profit": "52000", "risk_to_stop_quote": "20",
        },
    )
    assert store.get_entry_risk_plan("NBENTRY1")["stop_loss"] == "48000"
