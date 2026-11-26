"""Fase 0C outcome observer tests."""

import sqlite3

import pytest

from nebula.outcome_observer import legacy_episode_report, observe_all, observe_episode
from nebula.state import StateStore

RISK_PLAN = {
    "instrument": "BTC-USDT",
    "size": "2",
    "entry_price": "100",
    "stop_loss": "90",
    "take_profit": "130",
    "risk_to_stop_quote": "20",
}


def _fill(order_id, trade_id, side, size, price, client_id=""):
    return {
        "instId": "BTC-USDT",
        "ordId": order_id,
        "tradeId": trade_id,
        "clOrdId": client_id,
        "side": side,
        "fillSz": size,
        "fillPx": price,
        "fee": "-0.1",
        "feeCcy": "USDT",
        "fillTime": "2026-11-26T00:00:00Z",
    }


def _advance(state, client_id, status, order_id):
    for step in ("submitting", "acknowledged", "live"):
        state.update_intent(client_id, step, order_id)
    state.update_intent(client_id, status, order_id)


def _open_episode(state):
    assert state.reserve_entry_intent(
        "NBentry", "BTC-USDT", 1, "buy", RISK_PLAN, signal_key="sig-1"
    )
    episode = state.get_trade_episode("NBentry")
    assert episode and episode["trade_episode_id"]
    return episode


def test_episode_created_once_at_reservation(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)
    assert state.reserve_entry_intent("NBentry", "BTC-USDT", 1, "buy", RISK_PLAN) is False
    assert state.get_trade_episode("NBentry")["trade_episode_id"] == episode["trade_episode_id"]


def test_full_lifecycle_reconstruction_and_idempotence(tmp_path):
    path = str(tmp_path / "state.sqlite3")
    state = StateStore(path)
    episode = _open_episode(state)
    state.save_order_snapshot("NBentry", {"ordId": "111", "state": "filled", "sz": "2",
                                          "accFillSz": "2", "avgPx": "100", "fee": "-0.1", "feeCcy": "USDT"})
    _advance(state, "NBentry", "filled", "111")
    assert state.save_fill(_fill("111", "1", "buy", "2", "100", "NBentry"))
    state.reserve_protection_intent(entry_client_order_id="NBentry", instrument="BTC-USDT",
                                    algo_client_order_id="NBalgo", filled_size="2",
                                    take_profit="130", stop_loss="90")
    state.update_protection_intent("NBentry", "active", "999")
    state.save_exit_child_orders("NBentry", "BTC-USDT", ["222"])
    assert state.save_fill(_fill("222", "2", "sell", "1", "110"))
    assert state.save_fill(_fill("222", "3", "sell", "1", "120"))
    state.update_exit_child_order("NBentry", "222",
                                  {"state": "filled", "sz": "2", "accFillSz": "2", "avgPx": "115"})
    assert state.release_position_lock_after_flat_check(
        position_client_order_id="NBentry", base_balance="0", dust_limit="0",
        open_orders_checked=True)

    recorded = observe_episode(state, episode)
    types = [event["event_type"] for event in recorded]
    assert types == ["entry_intent_created", "entry_filled", "protection_activated",
                     "exit_partial_filled", "exit_filled", "position_closed",
                     "accounting_finalized"]
    finalized = recorded[-1]
    assert finalized["net_pnl_quote"] == "29.7"
    for event in recorded:
        assert "exchange_order_id" not in event
        assert "client_order_id" not in event
        assert "trade_id" not in event
        assert not ({"111", "222", "NBentry", "999"} & {str(value) for value in event.values()})
    assert state.get_trade_episode("NBentry")["status"] == "accounting_finalized"

    reopened = StateStore(path)
    assert observe_all(reopened) == {"recorded_events": [], "legacy_episode_unavailable": []}
    assert len(reopened.list_outcome_events(episode["trade_episode_id"])) == 7


def test_partial_fill_remainder_cancelled_without_false_close(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)
    state.save_order_snapshot("NBentry", {"ordId": "111", "state": "canceled", "sz": "2",
                                          "accFillSz": "1", "avgPx": "100", "fee": "-0.1", "feeCcy": "USDT"})
    _advance(state, "NBentry", "canceled", "111")
    assert state.save_fill(_fill("111", "1", "buy", "1", "100", "NBentry"))
    types = [event["event_type"] for event in observe_episode(state, episode)]
    assert "entry_remainder_cancelled" in types
    assert "position_closed" not in types
    assert "accounting_finalized" not in types


def test_entry_not_filled_closes_with_zero_pnl(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)
    state.save_order_snapshot("NBentry", {"ordId": "111", "state": "canceled", "sz": "2",
                                          "accFillSz": "0", "avgPx": "0", "fee": "0", "feeCcy": "USDT"})
    _advance(state, "NBentry", "canceled", "111")
    assert state.release_terminal_zero_fill_entry_lock("NBentry")
    recorded = observe_episode(state, episode)
    types = [event["event_type"] for event in recorded]
    assert "entry_not_filled" in types
    assert recorded[-1]["event_type"] == "accounting_finalized"
    assert recorded[-1]["net_pnl_quote"] == "0"


def test_export_failure_never_changes_trading_state(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)

    class BrokenExporter:
        def export(self, payload):
            raise RuntimeError("export down")

    recorded = observe_episode(state, episode, BrokenExporter())
    assert [event["event_type"] for event in recorded] == ["entry_intent_created"]
    assert state.get_position_lock()["owner_client_order_id"] == "NBentry"
    assert len(state.list_outcome_events(episode["trade_episode_id"], unexported_only=True)) == 1


def test_snapshot_positive_without_fills_is_not_final(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)
    state.save_order_snapshot("NBentry", {"ordId": "111", "state": "canceled", "sz": "2",
                                          "accFillSz": "1", "avgPx": "100", "fee": "-0.1", "feeCcy": "USDT"})
    _advance(state, "NBentry", "canceled", "111")
    assert state.release_position_lock_after_flat_check(
        position_client_order_id="NBentry", base_balance="0", dust_limit="0",
        open_orders_checked=True)
    types = [event["event_type"] for event in observe_episode(state, episode)]
    assert "reconciliation_incomplete" in types
    assert "entry_not_filled" not in types
    assert "position_closed" not in types
    assert "accounting_finalized" not in types


def test_missing_snapshot_terminal_entry_is_not_final(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)
    _advance(state, "NBentry", "canceled", "111")
    types = [event["event_type"] for event in observe_episode(state, episode)]
    assert "reconciliation_incomplete" in types
    assert "position_closed" not in types
    assert "accounting_finalized" not in types


def test_corrupt_fill_value_is_not_final(tmp_path):
    path = str(tmp_path / "state.sqlite3")
    state = StateStore(path)
    episode = _open_episode(state)
    state.save_order_snapshot("NBentry", {"ordId": "111", "state": "filled", "sz": "2",
                                          "accFillSz": "2", "avgPx": "100", "fee": "-0.1", "feeCcy": "USDT"})
    _advance(state, "NBentry", "filled", "111")
    assert state.save_fill(_fill("111", "1", "buy", "2", "100", "NBentry"))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE fills SET fill_size='corrupt'")
    types = [event["event_type"] for event in observe_episode(state, episode)]
    assert "reconciliation_incomplete" in types
    assert "position_closed" not in types
    assert "accounting_finalized" not in types


def test_base_fee_exit_classified_by_net_inventory(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)
    state.save_order_snapshot("NBentry", {"ordId": "111", "state": "filled", "sz": "1",
                                          "accFillSz": "1", "avgPx": "100", "fee": "-0.01", "feeCcy": "BTC"})
    _advance(state, "NBentry", "filled", "111")
    buy = _fill("111", "1", "buy", "1", "100", "NBentry")
    buy["fee"], buy["feeCcy"] = "-0.01", "BTC"
    assert state.save_fill(buy)
    state.reserve_exit_intent("NBexit", "BTC-USDT", 2, "sell", "NBentry")
    state.save_order_snapshot("NBexit", {"ordId": "222", "state": "filled", "sz": "0.99",
                                         "accFillSz": "0.99", "avgPx": "110", "fee": "-0.001", "feeCcy": "USDT"})
    _advance(state, "NBexit", "filled", "222")
    sell = _fill("222", "2", "sell", "0.99", "110", "NBexit")
    sell["fee"] = "-0.001"
    assert state.save_fill(sell)
    assert state.release_position_lock_after_flat_check(
        position_client_order_id="NBentry", base_balance="0", dust_limit="0",
        open_orders_checked=True)
    recorded = observe_episode(state, episode)
    types = [event["event_type"] for event in recorded]
    assert "exit_filled" in types
    assert "exit_partial_filled" not in types
    assert recorded[-1]["event_type"] == "accounting_finalized"
    assert all(event["fee_currency"] in {"BTC", "USDT"}
               for event in recorded if event["event_type"].endswith("_filled"))


def test_protection_transition_payload_distinguishes_status(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)
    state.reserve_protection_intent(entry_client_order_id="NBentry", instrument="BTC-USDT",
                                    algo_client_order_id="NBalgo", filled_size="2",
                                    take_profit="130", stop_loss="90")
    state.update_protection_intent("NBentry", "active", "999")
    first = observe_episode(state, episode)
    state.update_protection_intent("NBentry", "triggered", "999")
    second = observe_episode(state, episode)
    assert [e["protection_status"] for e in first if e["event_type"] == "protection_activated"] == ["active"]
    assert [e["protection_status"] for e in second if e["event_type"] == "protection_activated"] == ["triggered"]


def test_record_outcome_event_rejects_non_allowlisted_payload(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)
    payload = {
        "outcome_event_id": "x", "trade_episode_id": episode["trade_episode_id"],
        "instrument": "BTC-USDT", "event_type": "entry_filled",
        "exchange_order_id": "111",
    }
    with pytest.raises(ValueError):
        state.record_outcome_event("x", episode["trade_episode_id"], "entry_filled", payload)


def test_legacy_position_without_episode_is_reported(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    assert state.reserve_intent("NBlegacy", "BTC-USDT", 1, "buy")
    report = legacy_episode_report(state)
    assert report == [{
        "entry_client_order_id": "NBlegacy",
        "instrument": "BTC-USDT",
        "status": "reserved",
        "holds_position_lock": False,
        "reason": "legacy_episode_unavailable",
    }]
    assert state.get_trade_episode("NBlegacy") is None
    result = observe_all(state)
    assert result["recorded_events"] == []
    assert [item["entry_client_order_id"] for item in result["legacy_episode_unavailable"]] == ["NBlegacy"]


def _zero_fill_canceled(state):
    episode = _open_episode(state)
    state.save_order_snapshot("NBentry", {"ordId": "111", "state": "canceled", "sz": "2",
                                          "accFillSz": "0", "avgPx": "0", "fee": "0", "feeCcy": "USDT"})
    _advance(state, "NBentry", "canceled", "111")
    return episode


def test_missing_lock_without_release_evidence_is_not_final(tmp_path):
    path = str(tmp_path / "state.sqlite3")
    state = StateStore(path)
    episode = _zero_fill_canceled(state)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM position_lock WHERE singleton=1")
    types = [event["event_type"] for event in observe_episode(state, episode)]
    assert "reconciliation_incomplete" in types
    assert "position_closed" not in types
    assert "accounting_finalized" not in types


def test_transferred_lock_without_release_evidence_is_not_final(tmp_path):
    path = str(tmp_path / "state.sqlite3")
    state = StateStore(path)
    episode = _zero_fill_canceled(state)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE position_lock SET owner_client_order_id='NBother' WHERE singleton=1")
    types = [event["event_type"] for event in observe_episode(state, episode)]
    assert "reconciliation_incomplete" in types
    assert "position_closed" not in types
    assert "accounting_finalized" not in types


def test_rejected_entry_lock_release_evidence_finalizes(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)
    state.update_intent("NBentry", "submitting")
    state.update_intent("NBentry", "rejected")
    assert state.release_rejected_entry_lock("NBentry")
    recorded = observe_episode(state, episode)
    assert recorded[-1]["event_type"] == "accounting_finalized"
    assert recorded[-1]["net_pnl_quote"] == "0"


def test_record_event_cannot_forge_lock_release_evidence(tmp_path):
    path = str(tmp_path / "state.sqlite3")
    state = StateStore(path)
    episode = _zero_fill_canceled(state)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM position_lock WHERE singleton=1")
    with pytest.raises(ValueError):
        state.record_event("rejected_entry_lock_released", "NBentry")
    types = [event["event_type"] for event in observe_episode(state, episode)]
    assert "reconciliation_incomplete" in types
    assert "position_closed" not in types
    assert "accounting_finalized" not in types


def test_release_evidence_for_other_entry_does_not_finalize(tmp_path):
    path = str(tmp_path / "state.sqlite3")
    state = StateStore(path)
    episode = _zero_fill_canceled(state)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM position_lock WHERE singleton=1")
        db.execute(
            "INSERT INTO audit_events(event_type,client_order_id,details_json) VALUES(?,?,?)",
            ("rejected_entry_lock_released", "NBother", '{"intent_status":"rejected"}'),
        )
    types = [event["event_type"] for event in observe_episode(state, episode)]
    assert "position_closed" not in types
    assert "accounting_finalized" not in types


def test_rejected_with_order_id_without_snapshot_is_not_final(tmp_path):
    state = StateStore(str(tmp_path / "state.sqlite3"))
    episode = _open_episode(state)
    state.update_intent("NBentry", "submitting", "111")
    state.update_intent("NBentry", "rejected", "111")
    assert state.release_rejected_entry_lock("NBentry") is False
    types = [event["event_type"] for event in observe_episode(state, episode)]
    assert "reconciliation_incomplete" in types
    assert "position_closed" not in types
    assert "accounting_finalized" not in types


def test_export_rejects_row_identity_mismatch(tmp_path):
    path = str(tmp_path / "state.sqlite3")
    state = StateStore(path)
    episode = _open_episode(state)
    observe_episode(state, episode)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE outcome_events SET payload_json=replace(payload_json, ?, 'tampered-episode')",
            (episode["trade_episode_id"],),
        )
    exported = []

    class CollectingExporter:
        def export(self, payload):
            exported.append(payload)
            return True

    observe_episode(state, episode, CollectingExporter())
    assert exported == []
    assert len(state.list_outcome_events(episode["trade_episode_id"], unexported_only=True)) == 1
