from pathlib import Path

from nebula.state import StateStore


def test_intent_is_idempotent(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    cid = store.make_client_order_id("ema", "BTC-USDT", 12345, "buy")
    assert store.reserve_intent(cid, "BTC-USDT", 12345, "buy") is True
    assert store.reserve_intent(cid, "BTC-USDT", 12345, "buy") is False



def test_rejected_entry_lock_is_not_released_for_tiny_decimal_partial_fill(tmp_path):
    store = StateStore(str(tmp_path / "tiny-fill.db"))
    store.reserve_entry_intent("NBENTRY1", "BTC-USDT", 123, "buy")
    store.update_intent("NBENTRY1", "submitting")
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
    store.update_intent("NBENTRY1", "submitting")
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



def test_partially_failed_protection_state_cannot_be_resubmitted_or_regressed(tmp_path):
    import pytest

    store = StateStore(str(tmp_path / "partial-oco.db"))
    store.reserve_entry_intent("NBENTRY1", "BTC-USDT", 123, "buy")
    store.reserve_protection_intent(
        entry_client_order_id="NBENTRY1",
        instrument="BTC-USDT",
        algo_client_order_id="NBPTEST1",
        filled_size="0.004",
        take_profit="52000",
        stop_loss="48000",
    )
    store.update_protection_intent("NBENTRY1", "partially_failed", exchange_algo_id="9988")

    with pytest.raises(ValueError, match="terminal OCO state"):
        store.update_protection_intent("NBENTRY1", "active", exchange_algo_id="9988")
    with pytest.raises(ValueError, match="terminal OCO state"):
        store.update_protection_intent("NBENTRY1", "triggered", exchange_algo_id="9988")
    assert store.get_protection_intent("NBENTRY1")["status"] == "partially_failed"


def test_intent_state_machine_rejects_skipped_and_unknown_transitions(tmp_path):
    import pytest
    store = StateStore(str(tmp_path / "intent-transitions.db"))
    store.reserve_entry_intent("NB1", "BTC-USDT", 1, "buy")

    with pytest.raises(ValueError, match="Invalid order intent transition"):
        store.update_intent("NB1", "filled", "1001")
    with pytest.raises(ValueError, match="Unsupported order intent state"):
        store.update_intent("NB1", "mystery")

    store.update_intent("NB1", "submitting")
    store.update_intent("NB1", "submission_unknown")
    store.update_intent("NB1", "partially_filled", "1001")
    store.update_intent("NB1", "filled", "1001")
    assert store.get_intent("NB1")["status"] == "filled"


def test_terminal_intent_and_exchange_order_id_cannot_be_rewritten(tmp_path):
    import pytest
    store = StateStore(str(tmp_path / "intent-terminal.db"))
    store.reserve_entry_intent("NB1", "BTC-USDT", 1, "buy")
    store.update_intent("NB1", "submitting")
    store.update_intent("NB1", "acknowledged", "1001")
    store.update_intent("NB1", "filled", "1001")

    with pytest.raises(ValueError, match="Invalid order intent transition"):
        store.update_intent("NB1", "live", "1001")
    with pytest.raises(ValueError, match="cannot change"):
        store.update_intent("NB1", "filled", "1002")


def test_database_and_active_wal_sidecars_are_owner_only(tmp_path):
    import os
    import stat

    path = tmp_path / "private-state.db"
    old_umask = os.umask(0o022)
    try:
        store = StateStore(str(path))
        connection = store._connect()
        try:
            connection.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?)",
                ("private-test", "private-value"),
            )
            connection.commit()

            expected = 0o600
            assert stat.S_IMODE(path.stat().st_mode) == expected
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(path) + suffix)
                assert sidecar.exists()
                assert stat.S_IMODE(sidecar.stat().st_mode) == expected
        finally:
            connection.close()
    finally:
        os.umask(old_umask)


def test_state_store_refuses_symlinked_database_path(tmp_path):
    import pytest

    real_path = tmp_path / "real-state.db"
    real_path.write_bytes(b"not a sqlite database")
    link_path = tmp_path / "linked-state.db"
    link_path.symlink_to(real_path)

    with pytest.raises(RuntimeError, match="securely open NEBULA state file"):
        StateStore(str(link_path))
