import json
import sys

import pytest

from nebula.cli import (
    _cycle_gap_report,
    _emergency_status,
    _operator_breaker,
    _outcome_observation_summary,
    _redact_execution_report,
    main,
)
from nebula.risk_controls import SafetyError
from nebula.state import StateStore

HOUR_MS = 3_600_000
BASE_TS = 1_699_999_200_000  # aligned to an exact hourly slot


def test_execution_report_redaction_removes_order_and_account_detail():
    report = {
        "mode": "demo_execution",
        "status": "entry_and_protection_active",
        "candidate": {"size": "0.1", "price": "50000", "instrument": "BTC-USDT"},
        "entry": {"status": "filled", "order_id": "123", "sz": "0.1"},
        "order_submission_attempts": 2,
        "nested": {"equity": "73273", "reason": "ok"},
    }
    serialized = json.dumps(_redact_execution_report(report), sort_keys=True)
    for forbidden in ("0.1", "50000", "123", "73273", '"size"', '"order_id"'):
        assert forbidden not in serialized
    redacted = json.loads(serialized)
    assert redacted["status"] == "entry_and_protection_active"
    assert redacted["candidate"] == {"present": True}
    assert redacted["entry"] == {"present": True, "status": "filled"}
    assert redacted["order_submission_attempts"] == 2
    assert "nested" not in redacted


def test_execution_redaction_allowlist_covers_orchestrator_branches():
    branches = [
        {  # entry + protection active
            "mode": "demo_execution", "status": "entry_and_protection_active",
            "candidate": {"candidate_entry_plan": {"size": "0.5", "entry_price": "61000"}},
            "entry": {"exchange_order_id": "9001", "cumulative_fill_size": "0.5",
                      "average_fill_price": "61001"},
            "entry_reconciliation": {"status": "filled", "requested_size": "0.5"},
            "protection": {"status": "active", "stop_loss": "59000", "take_profit": "65000"},
            "position_lock_retained": True, "order_submission_attempts": 2,
        },
        {  # interrupted execution
            "mode": "demo_execution", "status": "execution_interrupted",
            "stage": "entry_submission", "error_type": "TimeoutError",
            "entry_submission_attempted": True, "oco_submission_attempted": False,
            "recovery": {"available_usdt": "70000", "account_equity_usdt": "73273"},
            "equity_observation": {"equity": "73273"},
            "exit_reconciliation": {"status": "pending", "exchange_order_id": "9002"},
            "order_submission_attempts": 1,
        },
        {  # shadow candidate ready
            "mode": "shadow_only", "status": "candidate_ready_but_execution_disabled",
            "candidate": {"size": "0.5"}, "execution": "not_submitted",
            "order_submission_attempts": 0,
        },
    ]
    for report in branches:
        serialized = json.dumps(_redact_execution_report(report), sort_keys=True)
        for forbidden in ("0.5", "61000", "9001", "59000", "65000", "70000",
                          "73273", "9002", "exchange_order_id", "stop_loss",
                          "available_usdt", "entry_price"):
            assert forbidden not in serialized, (report["status"], forbidden)
        redacted = json.loads(serialized)
        assert redacted["status"] == report["status"]
        assert redacted["order_submission_attempts"] == report["order_submission_attempts"]


def test_operator_emergency_stop_blocks_entry_until_confirmed_clear(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    breaker = _operator_breaker(state)
    breaker.engage_emergency_stop("operator test stop")
    assert _emergency_status(state)["engaged"] is True
    with pytest.raises(SafetyError):
        breaker.check_entry_allowed()
    with pytest.raises(SafetyError):
        breaker.clear_emergency_stop()
    breaker.clear_emergency_stop(explicit_confirmation=True)
    assert _emergency_status(state)["engaged"] is False


def test_emergency_clear_cli_requires_confirmation_token(tmp_path, monkeypatch):
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(sys, "argv", ["nebula", "emergency-stop", "--db", db])
    main()
    assert _emergency_status(StateStore(db))["engaged"] is True
    monkeypatch.setattr(sys, "argv", ["nebula", "emergency-clear", "--db", db])
    with pytest.raises(SystemExit):
        main()
    assert _emergency_status(StateStore(db))["engaged"] is True
    monkeypatch.setattr(
        sys, "argv",
        ["nebula", "emergency-clear", "--db", db, "--confirm", "clear-emergency-stop"],
    )
    main()
    assert _emergency_status(StateStore(db))["engaged"] is False


def test_state_backup_restore_preserves_safety_state(tmp_path):
    db = str(tmp_path / "state.db")
    backup = str(tmp_path / "backup.db")
    state = StateStore(db)
    _operator_breaker(state).engage_emergency_stop("before backup")
    state.record_event("marker_event", None, {"n": 1})
    assert state.backup_to(backup)["integrity"] == "ok"
    _operator_breaker(state).clear_emergency_stop(explicit_confirmation=True)
    state.record_event("after_backup_event", None, {"n": 2})
    restored = state.restore_from(backup)
    assert restored["emergency_stop_engaged"] is True
    event_types = {event["event_type"] for event in state.list_events()}
    assert "marker_event" in event_types
    assert "after_backup_event" not in event_types


def test_restore_never_clears_engaged_emergency_stop(tmp_path):
    db = str(tmp_path / "state.db")
    backup = str(tmp_path / "backup.db")
    state = StateStore(db)
    state.backup_to(backup)  # backup taken with stop disengaged
    _operator_breaker(state).engage_emergency_stop("engaged after backup")
    restored = state.restore_from(backup)
    assert restored["emergency_stop_engaged"] is True
    assert _emergency_status(state)["engaged"] is True
    with pytest.raises(SafetyError):
        _operator_breaker(state).check_entry_allowed()


def test_emergency_stop_blocks_entries_but_allows_risk_reducing_sells(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    breaker = _operator_breaker(state)
    breaker.engage_emergency_stop("operator test stop")
    with pytest.raises(SafetyError):
        breaker.check_order_allowed("buy")
    assert breaker.check_order_allowed("sell")["manual_stop"] is True


def test_restore_rejects_corrupt_source(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(Exception):
        state.restore_from(str(corrupt))


def test_cycle_gap_report_lists_missing_hourly_slots(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    for slot in (0, 1, 3):
        state.record_event("runtime_cycle_completed", None, {
            "latest_candle_ts": BASE_TS + slot * HOUR_MS, "bar": "1H",
        })
    report = _cycle_gap_report(state, now_ms=BASE_TS + 4 * HOUR_MS + 5 * 60_000)
    assert report["status"] == "cycle_gaps_detected"
    assert report["processed_cycles"] == 3
    assert report["gaps"] == [BASE_TS + 2 * HOUR_MS]


def test_cycle_gap_report_detects_trailing_gap_and_survives_event_volume(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    for slot in (0, 1):
        state.record_event("runtime_cycle_completed", None, {
            "latest_candle_ts": BASE_TS + slot * HOUR_MS, "bar": "1H",
        })
    for index in range(1005):
        state.record_event("noise_event", None, {"n": index})
    report = _cycle_gap_report(state, now_ms=BASE_TS + 4 * HOUR_MS + 5 * 60_000)
    assert report["processed_cycles"] == 2
    assert report["gaps"] == [BASE_TS + 2 * HOUR_MS, BASE_TS + 3 * HOUR_MS]


def test_outcome_observation_full_lifecycle_is_finalized_and_idempotent(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    risk_plan = {
        "instrument": "BTC-USDT", "size": "2", "entry_price": "100",
        "stop_loss": "90", "take_profit": "130", "risk_to_stop_quote": "20",
    }
    assert state.reserve_entry_intent("NBentry", "BTC-USDT", 1, "buy", risk_plan,
                                      signal_key="sig-1")
    state.save_order_snapshot("NBentry", {"ordId": "111", "state": "filled", "sz": "2",
                                          "accFillSz": "2", "avgPx": "100",
                                          "fee": "-0.1", "feeCcy": "USDT"})
    for step in ("submitting", "acknowledged", "live", "filled"):
        state.update_intent("NBentry", step, "111")
    fill = {"instId": "BTC-USDT", "ordId": "111", "tradeId": "1", "clOrdId": "NBentry",
            "side": "buy", "fillSz": "2", "fillPx": "100", "fee": "-0.1",
            "feeCcy": "USDT", "fillTime": "2026-11-26T00:00:00Z"}
    assert state.save_fill(fill)
    state.reserve_protection_intent(entry_client_order_id="NBentry", instrument="BTC-USDT",
                                    algo_client_order_id="NBalgo", filled_size="2",
                                    take_profit="130", stop_loss="90")
    state.update_protection_intent("NBentry", "active", "999")
    state.save_exit_child_orders("NBentry", "BTC-USDT", ["222"])
    exit_fill = dict(fill, ordId="222", tradeId="2", clOrdId="", side="sell",
                     fillSz="2", fillPx="115")
    assert state.save_fill(exit_fill)
    state.update_exit_child_order("NBentry", "222",
                                  {"state": "filled", "sz": "2", "accFillSz": "2",
                                   "avgPx": "115"})
    assert state.release_position_lock_after_flat_check(
        position_client_order_id="NBentry", base_balance="0", dust_limit="0",
        open_orders_checked=True)
    first = _outcome_observation_summary(state)
    assert first["accounting_finalized"] == 1
    assert first["recorded_events"] >= 1
    second = _outcome_observation_summary(state)
    assert second["recorded_events"] == 0
    assert second["accounting_finalized"] == 0


def test_restore_refused_while_position_lock_active(tmp_path):
    db = str(tmp_path / "state.db")
    backup = str(tmp_path / "backup.db")
    state = StateStore(db)
    state.backup_to(backup)
    risk_plan = {"instrument": "BTC-USDT", "size": "1", "entry_price": "100",
                 "stop_loss": "90", "take_profit": "130", "risk_to_stop_quote": "10"}
    assert state.reserve_entry_intent("NBentry", "BTC-USDT", 1, "buy", risk_plan)
    with pytest.raises(ValueError, match="active"):
        state.restore_from(backup)
    assert state.get_position_lock() is not None


def test_exit_size_cannot_exceed_reconciled_net_inventory(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    risk_plan = {"instrument": "BTC-USDT", "size": "2", "entry_price": "100",
                 "stop_loss": "90", "take_profit": "130", "risk_to_stop_quote": "20"}
    assert state.reserve_entry_intent("NBentry", "BTC-USDT", 1, "buy", risk_plan)
    state.update_intent("NBentry", "submitting", "111")
    state.update_intent("NBentry", "acknowledged", "111")
    state.update_intent("NBentry", "live", "111")
    state.update_intent("NBentry", "filled", "111")
    fill = {"instId": "BTC-USDT", "ordId": "111", "tradeId": "1", "clOrdId": "NBentry",
            "side": "buy", "fillSz": "2", "fillPx": "100", "fee": "-0.1",
            "feeCcy": "USDT", "fillTime": "2026-11-26T00:00:00Z"}
    assert state.save_fill(fill)
    with pytest.raises(RuntimeError, match="net inventory"):
        state.reserve_exit_intent("NBexit", "BTC-USDT", 2, "sell", "NBentry", size="3")
    assert state.reserve_exit_intent("NBexit", "BTC-USDT", 2, "sell", "NBentry", size="2")


def test_cycle_gap_report_detects_leading_gap_and_empty_coverage(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    state.record_event("runtime_cycle_completed", None, {
        "latest_candle_ts": BASE_TS + 2 * HOUR_MS, "bar": "1H",
    })
    report = _cycle_gap_report(
        state, now_ms=BASE_TS + 3 * HOUR_MS + 5 * 60_000, start_ms=BASE_TS,
    )
    assert report["gaps"] == [BASE_TS, BASE_TS + HOUR_MS]
    empty = StateStore(str(tmp_path / "empty.db"))
    report = _cycle_gap_report(
        empty, now_ms=BASE_TS + 2 * HOUR_MS + 5 * 60_000, start_ms=BASE_TS,
    )
    assert report["status"] == "cycle_gaps_detected"
    assert report["gaps"] == [BASE_TS, BASE_TS + HOUR_MS]
    unknown = _cycle_gap_report(empty, now_ms=BASE_TS)
    assert unknown["status"] == "cycle_coverage_unknown"


def test_outcome_observation_runs_when_cycle_fails(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "state.db")

    class ExplodingOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run_once(self, execute=False):
            raise RuntimeError("boom")

    monkeypatch.setattr("nebula.orchestrator.ExecutionOrchestrator", ExplodingOrchestrator)
    monkeypatch.setattr(sys, "argv", ["nebula", "orchestrate-once", "--db", db])
    with pytest.raises(SystemExit):
        main()
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "cycle_failed"
    assert output["outcome_observation"]["recorded_events"] == 0


def test_cycle_gap_report_rejects_mixed_timeframes_and_supports_4h(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    four_h = 4 * HOUR_MS
    base_4h = 1_699_992_000_000  # aligned to an exact 4H slot
    state.record_event("runtime_cycle_completed", None, {
        "latest_candle_ts": base_4h, "bar": "4H",
    })
    report = _cycle_gap_report(state, now_ms=base_4h + 2 * four_h + 60_000)
    assert report["bar"] == "4H"
    assert report["gaps"] == [base_4h + four_h]
    state.record_event("runtime_cycle_completed", None, {
        "latest_candle_ts": base_4h + HOUR_MS, "bar": "1H",
    })
    report = _cycle_gap_report(state, now_ms=base_4h + 2 * four_h)
    assert report["status"] == "mixed_timeframes_not_supported"


def test_outcome_observation_after_submission_failure(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "state.db")

    class FailingAfterEntryOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run_once(self, execute=False):
            state = StateStore(db)
            risk_plan = {"instrument": "BTC-USDT", "size": "1", "entry_price": "100",
                         "stop_loss": "90", "take_profit": "130", "risk_to_stop_quote": "10"}
            assert state.reserve_entry_intent("NBentry", "BTC-USDT", 1, "buy", risk_plan)
            raise RuntimeError("boom after submission")

    monkeypatch.setattr(
        "nebula.orchestrator.ExecutionOrchestrator", FailingAfterEntryOrchestrator,
    )
    monkeypatch.setattr(sys, "argv", ["nebula", "orchestrate-once", "--db", db])
    with pytest.raises(SystemExit):
        main()
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "cycle_failed"
    assert output["order_submission_attempts"] is None
    assert output["outcome_observation"]["recorded_events"] >= 1


def _filled_entry(state, size="2", fee="-0.1", fee_ccy="USDT"):
    risk_plan = {"instrument": "BTC-USDT", "size": size, "entry_price": "100",
                 "stop_loss": "90", "take_profit": "130", "risk_to_stop_quote": "20"}
    assert state.reserve_entry_intent("NBentry", "BTC-USDT", 1, "buy", risk_plan)
    state.update_intent("NBentry", "submitting", "111")
    state.update_intent("NBentry", "acknowledged", "111")
    state.update_intent("NBentry", "live", "111")
    state.update_intent("NBentry", "filled", "111")
    assert state.save_fill({"instId": "BTC-USDT", "ordId": "111", "tradeId": "1",
                            "clOrdId": "NBentry", "side": "buy", "fillSz": size,
                            "fillPx": "100", "fee": fee, "feeCcy": fee_ccy,
                            "fillTime": "2026-11-26T00:00:00Z"})


def test_protection_reservation_cannot_exceed_net_inventory(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    _filled_entry(state, size="2", fee="-0.1", fee_ccy="BTC")
    with pytest.raises(ValueError, match="net inventory"):
        state.reserve_protection_intent(
            entry_client_order_id="NBentry", instrument="BTC-USDT",
            algo_client_order_id="NBalgo", filled_size="2",
            take_profit="130", stop_loss="90",
        )
    intent = state.reserve_protection_intent(
        entry_client_order_id="NBentry", instrument="BTC-USDT",
        algo_client_order_id="NBalgo", filled_size="1.9",
        take_profit="130", stop_loss="90",
    )
    assert intent["filled_size"] == "1.9"


def test_sell_base_fee_reduces_inventory_for_next_sell(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    _filled_entry(state, size="2")
    assert state.reserve_exit_intent("NBexit", "BTC-USDT", 2, "sell", "NBentry", size="1")
    state.update_intent("NBexit", "submitting", "222")
    state.update_intent("NBexit", "acknowledged", "222")
    state.update_intent("NBexit", "live", "222")
    state.update_intent("NBexit", "filled", "222")
    assert state.save_fill({"instId": "BTC-USDT", "ordId": "222", "tradeId": "2",
                            "clOrdId": "NBexit", "side": "sell", "fillSz": "1",
                            "fillPx": "110", "fee": "-0.1", "feeCcy": "BTC",
                            "fillTime": "2026-11-26T01:00:00Z"})
    with pytest.raises(RuntimeError, match="net inventory"):
        state.reserve_exit_intent("NBexit2", "BTC-USDT", 3, "sell", "NBentry", size="1")
    assert state.reserve_exit_intent("NBexit2", "BTC-USDT", 3, "sell", "NBentry", size="0.9")


def test_restore_refused_while_writer_connection_open(tmp_path):
    db = str(tmp_path / "state.db")
    backup = str(tmp_path / "backup.db")
    state = StateStore(db)
    state.backup_to(backup)
    with state._connect() as held:
        held.execute("SELECT 1 FROM emergency_stop").fetchone()
        with pytest.raises(ValueError, match="restore refused"):
            state.restore_from(backup)
    assert state.restore_from(backup)["status"] == "state_restored"


def test_snapshot_base_fee_reduces_protection_inventory(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    risk_plan = {"instrument": "BTC-USDT", "size": "2", "entry_price": "100",
                 "stop_loss": "90", "take_profit": "130", "risk_to_stop_quote": "20"}
    assert state.reserve_entry_intent("NBentry", "BTC-USDT", 1, "buy", risk_plan)
    state.update_intent("NBentry", "submitting", "111")
    state.update_intent("NBentry", "acknowledged", "111")
    state.update_intent("NBentry", "filled", "111")
    state.save_order_snapshot("NBentry", {"ordId": "111", "state": "filled", "sz": "2",
                                          "accFillSz": "2", "avgPx": "100",
                                          "fee": "-0.1", "feeCcy": "BTC"})
    with pytest.raises(ValueError, match="net inventory"):
        state.reserve_protection_intent(
            entry_client_order_id="NBentry", instrument="BTC-USDT",
            algo_client_order_id="NBalgo", filled_size="2",
            take_profit="130", stop_loss="90",
        )
    intent = state.reserve_protection_intent(
        entry_client_order_id="NBentry", instrument="BTC-USDT",
        algo_client_order_id="NBalgo", filled_size="1.9",
        take_profit="130", stop_loss="90",
    )
    assert intent["filled_size"] == "1.9"


def test_snapshot_sell_without_ledger_reduces_inventory(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    _filled_entry(state, size="2")
    assert state.reserve_exit_intent("NBexit", "BTC-USDT", 2, "sell", "NBentry", size="1")
    state.update_intent("NBexit", "submitting", "222")
    state.update_intent("NBexit", "acknowledged", "222")
    state.update_intent("NBexit", "filled", "222")
    state.save_order_snapshot("NBexit", {"ordId": "222", "state": "filled", "sz": "1",
                                         "accFillSz": "1", "avgPx": "110",
                                         "fee": "-0.1", "feeCcy": "USDT"})
    with pytest.raises(RuntimeError, match="net inventory"):
        state.reserve_exit_intent("NBexit2", "BTC-USDT", 3, "sell", "NBentry", size="1.5")
    assert state.reserve_exit_intent("NBexit2", "BTC-USDT", 3, "sell", "NBentry", size="1")


def test_positive_base_rebate_increases_inventory(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    _filled_entry(state, size="2", fee="0.1", fee_ccy="BTC")
    with pytest.raises(RuntimeError, match="net inventory"):
        state.reserve_exit_intent("NBexit", "BTC-USDT", 2, "sell", "NBentry", size="2.2")
    assert state.reserve_exit_intent("NBexit", "BTC-USDT", 2, "sell", "NBentry", size="2.1")


def test_submitted_order_without_evidence_fails_closed(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    risk_plan = {"instrument": "BTC-USDT", "size": "2", "entry_price": "100",
                 "stop_loss": "90", "take_profit": "130", "risk_to_stop_quote": "20"}
    assert state.reserve_entry_intent("NBentry", "BTC-USDT", 1, "buy", risk_plan)
    state.update_intent("NBentry", "submitting", "111")
    state.update_intent("NBentry", "acknowledged", "111")
    with pytest.raises(ValueError, match="incomplete"):
        state.reserve_protection_intent(
            entry_client_order_id="NBentry", instrument="BTC-USDT",
            algo_client_order_id="NBalgo", filled_size="1",
            take_profit="130", stop_loss="90",
        )


def test_cycle_gap_report_unknown_for_invalid_candle_timestamp(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    state.record_event("runtime_cycle_completed", None, {
        "latest_candle_ts": None, "bar": "1H",
    })
    report = _cycle_gap_report(state, now_ms=BASE_TS + HOUR_MS)
    assert report["status"] == "cycle_coverage_unknown"


def test_outcome_observation_summary_on_empty_state(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    summary = _outcome_observation_summary(state)
    assert summary == {
        "recorded_events": 0,
        "accounting_finalized": 0,
        "legacy_episode_unavailable": 0,
    }
