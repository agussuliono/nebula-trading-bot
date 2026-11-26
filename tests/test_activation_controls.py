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
BASE_TS = 1_700_000_000_000


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
            "latest_candle_ts": BASE_TS + slot * HOUR_MS,
        })
    report = _cycle_gap_report(state)
    assert report["status"] == "cycle_gaps_detected"
    assert report["processed_cycles"] == 3
    assert report["gaps"] == [BASE_TS + 2 * HOUR_MS]


def test_outcome_observation_summary_on_empty_state(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    summary = _outcome_observation_summary(state)
    assert summary == {
        "recorded_events": 0,
        "accounting_finalized": 0,
        "legacy_episode_unavailable": 0,
    }
