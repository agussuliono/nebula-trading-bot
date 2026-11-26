"""Fase 0C outcome observer tests."""

from nebula.outcome_observer import observe_all, observe_episode
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
    assert observe_all(reopened) == []
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
