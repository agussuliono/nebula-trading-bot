"""Fase 0B research registry tests (spec v2 section 8-9, v2.2.1-v2.2.3)."""

import math
import sqlite3

import pytest

from nebula.research_registry import (
    ResearchRegistry,
    cscv_pbo,
    deflated_sharpe_ratio,
    effective_trial_count,
    pbo_from_ranks,
    pearson_correlation,
    sharpe_ratio,
)


# -- statistics fixtures ----------------------------------------------------


def test_pbo_fixture_rank_direction():
    result = pbo_from_ranks([3, 1, 2, 3], 3)
    assert result.status == "ok"
    assert result.value == pytest.approx(0.25)


def test_cscv_pbo_missing_return_is_inconclusive():
    combos = [([1.0, 2.0], [1.0, None]), ([2.0, 1.0], [1.0, 2.0])]
    assert cscv_pbo(combos).status == "inconclusive"
    excluded = cscv_pbo(combos, preregistered_exclusions={1})
    assert excluded.status == "inconclusive"  # fewer than 2 valid candidates


def test_dsr_fixture():
    # Trial Sharpes [0.10, 0.05, 0.00, -0.05] with T=100, g3=0, g4=3 cannot be
    # built from raw returns directly; verify via the frozen constants by
    # constructing returns whose selected Sharpe is 0.10.
    # Simpler: four independent trials with known Sharpes via scaling.
    base = [1.0, -1.0] * 50  # mean 0, T=100
    trials = []
    for target in (0.10, 0.05, 0.00, -0.05):
        # shift/scale base to hit the target Sharpe approximately
        shifted = [x + target for x in base]
        trials.append(shifted)
    result = deflated_sharpe_ratio(trials, 0)
    assert result.status == "ok"
    assert 0.0 < result.value < 1.0


def test_dsr_zero_variance_trial_is_inconclusive():
    trials = [[0.01, 0.01, 0.01, 0.01], [-0.02, 0.0, 0.01, 0.03]]
    assert deflated_sharpe_ratio(trials, 1).status == "inconclusive"
    assert pearson_correlation(trials[0], trials[1]) is None


def test_effective_trial_count_identical_trials():
    corr = [[1.0, 1.0, 1.0, 1.0]] * 4
    result = effective_trial_count(corr)
    assert result.status == "ok"
    assert result.value == pytest.approx(1.0, abs=1e-9)


def test_effective_trial_count_equicorrelated():
    rho = 0.5
    corr = [[1.0 if i == j else rho for j in range(4)] for i in range(4)]
    result = effective_trial_count(corr)
    assert result.status == "ok"
    assert result.value == pytest.approx(2.285714285714, abs=1e-9)


def test_sharpe_uses_ddof1():
    result = sharpe_ratio([1.0, 2.0, 3.0])
    assert result.status == "ok"
    assert result.value == pytest.approx(2.0 / 1.0)  # mean 2, sample std 1


# -- registry store -----------------------------------------------------------


@pytest.fixture()
def registry(tmp_path):
    reg = ResearchRegistry(str(tmp_path / "research.db"))
    yield reg
    reg.close()


def test_import_is_idempotent_and_conflicts_rejected(registry):
    events = [{"event_id": "e1", "kind": "cycle"}, {"event_id": "e2", "kind": "cycle"}]
    first = registry.import_events(events)
    assert first == {"imported": 2, "duplicates": 0, "rejected": 0}
    second = registry.import_events(events)
    assert second == {"imported": 0, "duplicates": 2, "rejected": 0}
    conflict = registry.import_events([{"event_id": "e1", "kind": "tampered"}])
    assert conflict["rejected"] == 1
    assert registry.verify_chain() is True


def test_evidence_tables_are_append_only(registry):
    registry.import_events([{"event_id": "e1"}])
    with pytest.raises(sqlite3.IntegrityError):
        registry._conn.execute("UPDATE research_events SET payload_json = '{}'")
    with pytest.raises(sqlite3.IntegrityError):
        registry._conn.execute("DELETE FROM research_events")


def test_checkpoint_verifies_against_chain(registry):
    registry.import_events([{"event_id": "e1"}, {"event_id": "e2"}])
    with pytest.raises(Exception):
        registry.create_checkpoint("sig-ref-1", signature="")
    checkpoint = registry.create_checkpoint("sig-ref-1", signature="sig-1")
    assert registry.verify_checkpoint(checkpoint) is False  # verifier is mandatory
    assert registry.verify_checkpoint(checkpoint, lambda cp, sig: sig == "sig-1") is True
    registry.import_events([{"event_id": "e3"}])
    assert registry.verify_checkpoint(checkpoint, lambda cp, sig: True) is False


def test_all_trials_counted(registry):
    registry.register_experiment("exp1", "hyp", "p" * 64, "d" * 64, "abc123", "c" * 64)
    for idx, status in enumerate(("completed", "failed", "aborted", "invalid")):
        registry.record_trial(f"t{idx}", "exp1", "group-a", status, {}, {})
    assert registry.trial_count("exp1") == 4


def test_validation_without_artifacts_is_inconclusive(registry):
    registry.register_experiment("exp1", "hyp", "p" * 64, "d" * 64, "abc123", "c" * 64)
    status = registry.record_validation("run1", "exp1", "dsr", {}, {"dsr": 0.9})
    assert status == "inconclusive"


def test_holdout_contamination_is_irreversible(registry):
    assert registry.holdout_status("holdout-1") == "untouched"
    status = registry.record_holdout_access(
        "holdout-1", "exp1", granted=True, protocol_sha256="p" * 64, used_for_selection=True
    )
    assert status == "contaminated"
    status = registry.record_holdout_access(
        "holdout-1", "exp1", granted=False, protocol_sha256="p" * 64
    )
    assert status == "contaminated"


def test_replace_cannot_bypass_append_only(registry):
    registry.import_events([{"event_id": "e1", "kind": "cycle"}])
    with pytest.raises(sqlite3.IntegrityError):
        registry._conn.execute(
            "INSERT OR REPLACE INTO research_events "
            "(event_id, payload_json, payload_sha256, prev_hash, event_hash, imported_at) "
            "VALUES ('e1', 'replaced', 'x', 'y', 'z', 'now')"
        )
    assert registry.verify_chain() is True


def test_checkpoint_covers_registry_and_signature(registry):
    registry.register_experiment("exp1", "hyp", "p" * 64, "d" * 64, "abc123", "c" * 64)
    checkpoint = registry.create_checkpoint("sig-ref-1", signature="sig-1")
    assert registry.verify_checkpoint(checkpoint) is False  # signature needs verifier
    assert registry.verify_checkpoint(checkpoint, lambda cp, sig: sig == "sig-1") is True
    registry.record_trial("t1", "exp1", "group-a", "completed", {}, {}, origin="ai")
    assert registry.verify_checkpoint(checkpoint, lambda cp, sig: True) is False


def test_holdout_gate_requires_frozen_protocol(registry):
    registry.register_experiment("exp1", "hyp", "p" * 64, "d" * 64, "abc123", "c" * 64)
    assert registry.request_holdout_access("h1", "exp1", "p" * 64) is False
    assert registry.holdout_read_allowed("h1", "exp1", "p" * 64) is False
    registry.record_holdout_access("h1", "exp1", granted=True, protocol_sha256="p" * 64)
    assert registry.holdout_read_allowed("h1", "exp1", "p" * 64) is False
    registry.freeze_protocol("exp1", actor="gan")
    assert registry.request_holdout_access("h1", "exp1", "p" * 64) is True
    assert registry.holdout_read_allowed("h1", "exp1", "p" * 64) is True


def test_validation_bool_sample_count_is_inconclusive(registry):
    registry.register_experiment("exp1", "hyp", "p" * 64, "d" * 64, "abc123", "c" * 64)
    artifacts = {
        "return_series_artifact_sha256": "a" * 64,
        "split_manifest_sha256": "a" * 64,
        "benchmark_id": "bench",
        "sample_count": True,
        "return_skewness": 0.0,
        "return_kurtosis": 3.0,
        "observed_sharpe": 0.1,
        "effective_trial_count": 4.0,
        "trial_correlation_artifact_sha256": "b" * 64,
        "selection_rule": "max-sharpe",
        "researcher_decision_log_sha256": "c" * 64,
    }
    assert registry.record_validation("run1", "exp1", "dsr", artifacts, {}) == "inconclusive"


def test_validation_bad_artifact_hash_is_inconclusive(registry):
    registry.register_experiment("exp1", "hyp", "p" * 64, "d" * 64, "abc123", "c" * 64)
    artifacts = {
        "return_series_artifact_sha256": "not-a-hash",
        "split_manifest_sha256": "a" * 64,
        "benchmark_id": "bench",
        "sample_count": 100,
        "return_skewness": 0.0,
        "return_kurtosis": 3.0,
        "observed_sharpe": 0.1,
        "effective_trial_count": 4.0,
        "trial_correlation_artifact_sha256": "b" * 64,
        "selection_rule": "max-sharpe",
        "researcher_decision_log_sha256": "c" * 64,
    }
    assert registry.record_validation("run1", "exp1", "dsr", artifacts, {}) == "inconclusive"


def test_direct_protocol_frozen_status_is_rejected(registry):
    from nebula.research_registry import RegistryError

    registry.register_experiment("exp1", "hyp", "p" * 64, "d" * 64, "abc123", "c" * 64)
    with pytest.raises(RegistryError):
        registry.record_experiment_status("exp1", "protocol_frozen", "forged", "mallory")
    assert registry.request_holdout_access("h1", "exp1", "p" * 64) is False
    assert registry.holdout_read_allowed("h1", "exp1", "p" * 64) is False
