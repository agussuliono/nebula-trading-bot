from nebula.state import StateStore


def test_exit_intent_fills_are_linked_to_active_entry_only(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite"))
    assert state.reserve_entry_intent("entry001", "AAA-USDT", 100, "buy")
    state.update_intent("entry001", "acknowledged", "1001")
    assert state.reserve_exit_intent("exit001", "AAA-USDT", 101, "sell", "entry001")
    state.update_intent("exit001", "acknowledged", "1002")
    # Fill rows are associated through exchange order IDs; unrelated same-symbol
    # historical orders are not included in this active position query.
    state.save_fill({"instId":"AAA-USDT","ordId":"1001","tradeId":"1","side":"buy",
                     "fillSz":"1","fillPx":"100","fee":"-0.1","feeCcy":"USDT"})
    state.save_fill({"instId":"AAA-USDT","ordId":"1002","tradeId":"2","side":"sell",
                     "fillSz":"0.2","fillPx":"110","fee":"-0.02","feeCcy":"USDT"})
    state.save_fill({"instId":"AAA-USDT","ordId":"9999","tradeId":"3","side":"buy",
                     "fillSz":"5","fillPx":"10","fee":"-0.01","feeCcy":"USDT"})
    fills = state.get_position_fills("entry001")
    assert {x["exchange_order_id"] for x in fills} == {"1001", "1002"}
    assert len(fills) == 2


def test_legacy_sell_intent_is_not_guessed_for_current_locked_position(tmp_path):
    import sqlite3
    db_path = str(tmp_path / "legacy.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE intents (
        client_order_id TEXT PRIMARY KEY, instrument TEXT NOT NULL, candle_ts INTEGER NOT NULL,
        side TEXT NOT NULL, status TEXT NOT NULL, exchange_order_id TEXT,
        last_update TEXT DEFAULT CURRENT_TIMESTAMP, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE position_lock (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner_client_order_id TEXT NOT NULL,
        instrument TEXT NOT NULL, acquired_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("INSERT INTO intents(client_order_id,instrument,candle_ts,side,status) VALUES('entry1','AAA-USDT',1,'buy','filled')")
    conn.execute("INSERT INTO intents(client_order_id,instrument,candle_ts,side,status) VALUES('exit1','AAA-USDT',2,'sell','filled')")
    conn.execute("INSERT INTO position_lock(singleton,owner_client_order_id,instrument) VALUES(1,'entry1','AAA-USDT')")
    conn.commit()
    conn.close()
    state = StateStore(db_path)
    with state._connect() as db:
        row = db.execute("SELECT position_client_order_id FROM intents WHERE client_order_id='exit1'").fetchone()
    assert row[0] is None


def test_unlinked_legacy_sell_fill_blocks_position_attribution(tmp_path):
    state = StateStore(str(tmp_path / "legacy-fill.sqlite"))
    assert state.reserve_entry_intent("entry1", "AAA-USDT", 1, "buy")
    state.update_intent("entry1", "acknowledged", "1001")
    # Simulate a legacy sell intent created before v0.12.0 without a position link.
    with state._connect() as db:
        db.execute("INSERT INTO intents(client_order_id,instrument,candle_ts,side,status,exchange_order_id) VALUES('legacy-sell','AAA-USDT',2,'sell','filled','1002')")
    state.save_fill({"instId":"AAA-USDT","ordId":"1002","tradeId":"2","side":"sell",
                     "fillSz":"0.1","fillPx":"110","fee":"-0.01","feeCcy":"USDT"})
    import pytest
    with pytest.raises(ValueError, match="Unlinked legacy sell fills"):
        state.get_position_fills("entry1")


def test_position_context_exposes_terminal_entry_and_unresolved_exit_state(tmp_path):
    state = StateStore(str(tmp_path / "context.sqlite"))
    assert state.reserve_entry_intent("entry1", "AAA-USDT", 1, "buy")
    state.update_intent("entry1", "filled", "1001")
    with state._connect() as db:
        db.execute("""INSERT INTO order_snapshots(client_order_id,exchange_order_id,exchange_state,
            requested_size,cumulative_fill_size,average_fill_price,fee,fee_currency)
            VALUES('entry1','1001','filled','1','1','100','-0.1','USDT')""")
    assert state.reserve_exit_intent("exit1", "AAA-USDT", 2, "sell", "entry1")
    state.update_intent("exit1", "live", "1002")
    context = state.get_position_context("entry1")
    assert context["entry_status"] == "filled"
    assert context["entry_exchange_state"] == "filled"
    assert context["entry_snapshot_filled_size"] == "1"
    assert context["unresolved_exit_intents"] == 1
