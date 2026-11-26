"""Fase 1 fixture harness acceptance tests (spec v1.1)."""

import hashlib
import json

import pytest

from nebula.fixture_harness import (
    BUILTIN_CATALOG,
    PINNED_CATALOG_SHA256,
    FixtureManifest,
    PromotionEvidence,
    Protocol,
    build_dsr_artifacts,
    catalog_digest,
    evaluate_dsr_gate,
    evaluate_pbo_gate,
    evaluate_promotion,
    evaluate_prospective,
    freeze_protocol,
    record_artifact_validation,
    run_dsr_and_record,
    run_pbo_and_record,
    synthetic_fixture_bytes,
    verify_fixture,
)
from nebula.research_registry import ResearchRegistry

SYNTH_BYTES = synthetic_fixture_bytes()
SYNTH_HASH = hashlib.sha256(SYNTH_BYTES).hexdigest()
TRIALS = [
    [0.03, 0.02, 0.01, 0.02, 0.03, 0.01, 0.02, 0.03],
    [0.02, 0.01, 0.02, 0.01, 0.02, 0.01, 0.02, 0.01],
    [0.01, 0.02, 0.01, 0.02, 0.01, 0.02, 0.01, 0.02],
]
PARAMS = [{"ema": 20, "atr": 14}, {"ema": 21, "atr": 14}, {"ema": 20, "atr": 15}]
def _snapshot_bytes(frozen):
    return json.dumps(
        {
            "schema": "snapshot-v1",
            "trial_ids": ["t0", "t1", "t2"],
            "protocol_sha256": frozen.sha256,
            "returns_sha256": hashlib.sha256(
                json.dumps(TRIALS, separators=(",", ":")).encode()
            ).hexdigest(),
            "experiment_id": "exp1",
            "dataset_sha256": SYNTH_HASH,
        }
    ).encode()


def _wf_bytes(frozen):
    returns = TRIALS[0]
    folds = [returns[i : i + 2] for i in range(0, len(returns), 2)]
    return json.dumps(
        {
            "schema": "walk-forward-v1",
            "trial_id": "t0",
            "protocol_sha256": frozen.sha256,
            "fold_returns": folds,
        }
    ).encode()


def _stress_bytes(frozen):
    return json.dumps(
        {
            "schema": "stress-v1",
            "trial_id": "t0",
            "protocol_sha256": frozen.sha256,
            "fee_bps": 10.0,
            "slippage_bps": 5.0,
            "gross_returns": TRIALS[0],
        }
    ).encode()


def protocol(**overrides):
    base = dict(
        trial_group="group-a",
        hypothesis_family="regime_filter",
        dataset_hashes={"exploration": SYNTH_HASH, "prospective": SYNTH_HASH},
        catalog_sha256=PINNED_CATALOG_SHA256,
        prospective_cutoff="2026-12-01T00:00:00Z",
        fee_bps=10.0,
        slippage_bps=5.0,
        promotion_threshold=0.5,
        prospective_min_observations=30,
        prospective_min_duration_days=14,
        prospective_min_completeness=0.95,
        preregistered_exclusions={},
        ema_step=1,
        atr_step=1,
    )
    base.update(overrides)
    return Protocol(**base)


@pytest.fixture()
def frozen():
    return freeze_protocol(protocol())


@pytest.fixture()
def registry(tmp_path, frozen):
    reg = ResearchRegistry(str(tmp_path / "research.db"))
    reg.register_experiment("exp1", "hyp", frozen.sha256, "d" * 64, "abc123", "c" * 64)
    reg.freeze_protocol("exp1", "test")
    for idx, returns in enumerate(TRIALS):
        reg.record_trial(f"t{idx}", "exp1", "group-a", "completed", PARAMS[idx], {"returns": returns})
    yield reg
    reg.close()


def synth_manifest(**overrides):
    base = dict(dataset_id="ds1", source_id="synth-trend-v1", kind="synthetic", sha256=SYNTH_HASH)
    base.update(overrides)
    return FixtureManifest(**base)


def test_builtin_catalog_matches_pinned_digest():
    assert catalog_digest(BUILTIN_CATALOG) == PINNED_CATALOG_SHA256


def test_builtin_synthetic_fixture_is_accepted(frozen):
    assert verify_fixture(synth_manifest(), SYNTH_BYTES, frozen).accepted is True


def test_unpinned_catalog_protocol_is_rejected():
    fake_frozen = freeze_protocol(protocol(catalog_sha256="f" * 64))
    assert verify_fixture(synth_manifest(), SYNTH_BYTES, fake_frozen).accepted is False


def test_pbo_exclusion_after_performance_seen_event(registry, frozen):
    registry.record_performance_seen("exp1", "t1", "test")
    registry.record_candidate_exclusion("exp1", "t1", "weak", "test")
    assert evaluate_pbo_gate(frozen, registry, "exp1").status == "inconclusive"


def test_pbo_reading_returns_marks_exposure(tmp_path):
    frozen = freeze_protocol(protocol(preregistered_exclusions={"t1": "weak"}))
    reg = ResearchRegistry(str(tmp_path / "r.db"))
    reg.register_experiment("exp1", "hyp", frozen.sha256, "d" * 64, "abc123", "c" * 64)
    reg.freeze_protocol("exp1", "test")
    for idx, returns in enumerate(TRIALS):
        reg.record_trial(f"t{idx}", "exp1", "group-a", "completed", PARAMS[idx], {"returns": returns})
    reg.trial_returns("exp1")
    reg.record_candidate_exclusion("exp1", "t1", "weak", "test")
    assert evaluate_pbo_gate(frozen, reg, "exp1").status == "inconclusive"
    reg.close()


def test_pbo_conflicting_exclusion_reasons(registry):
    frozen = freeze_protocol(protocol(preregistered_exclusions={"t1": "weak"}))
    registry.record_candidate_exclusion("exp1", "t1", "other", "test")
    registry.record_candidate_exclusion("exp1", "t1", "weak", "test")
    assert registry.exclusion_reasons("exp1") == {"t1": {"weak", "other"}}
    assert evaluate_pbo_gate(frozen, registry, "exp1").status == "inconclusive"


def test_pbo_unregistered_pre_performance_exclusion(registry, frozen):
    registry.record_candidate_exclusion("exp1", "t1", "weak", "test")
    assert evaluate_pbo_gate(frozen, registry, "exp1").status == "inconclusive"


def test_pbo_preregistered_exclusion_is_ok(tmp_path):
    frozen = freeze_protocol(protocol(preregistered_exclusions={"t1": "weak"}))
    reg = ResearchRegistry(str(tmp_path / "r.db"))
    reg.register_experiment("exp1", "hyp", frozen.sha256, "d" * 64, "abc123", "c" * 64)
    reg.freeze_protocol("exp1", "test")
    for idx, returns in enumerate(TRIALS):
        reg.record_trial(f"t{idx}", "exp1", "group-a", "completed", PARAMS[idx], {"returns": returns})
    reg.record_candidate_exclusion("exp1", "t1", "weak", "test")
    assert evaluate_pbo_gate(frozen, reg, "exp1").status == "ok"
    reg.close()


def test_dsr_uses_registry_returns(registry):
    artifacts = build_dsr_artifacts(registry, "exp1")
    assert evaluate_dsr_gate(registry, "exp1", "t0", artifacts).status == "ok"


def test_prospective_gate_rejects_non_finite(frozen):
    assert evaluate_prospective(frozen, 30, 14, float("nan"), "2026-12-01T00:00:00Z").eligible is False


def _record_evidence(registry, frozen):
    run_pbo_and_record(registry, "exp1", frozen)
    run_dsr_and_record(registry, "exp1", frozen)
    record_artifact_validation(registry, "exp1", "snapshot", _snapshot_bytes(frozen), frozen)
    record_artifact_validation(registry, "exp1", "walk_forward", _wf_bytes(frozen), frozen)
    record_artifact_validation(registry, "exp1", "stress", _stress_bytes(frozen), frozen)
    return PromotionEvidence(
        snapshot_bytes=_snapshot_bytes(frozen),
        walk_forward_bytes=_wf_bytes(frozen),
        stress_bytes=_stress_bytes(frozen),
        trial_ids=("t0", "t1", "t2"),
    )


def _promotion(registry, frozen, evidence=None, **overrides):
    base = dict(
        frozen=frozen,
        protocol=protocol(),
        registry=registry,
        experiment_id="exp1",
        manifest=synth_manifest(),
        dataset_bytes=SYNTH_BYTES,
        prospective_observations=30,
        prospective_duration_days=14,
        prospective_completeness=0.95,
        prospective_start="2026-12-01T00:00:00Z",
        evidence=evidence or _record_evidence(registry, frozen),
    )
    base.update(overrides)
    return evaluate_promotion(**base)


def test_promotion_with_recomputed_validations(registry, frozen):
    assert _promotion(registry, frozen).eligible is True


def test_promotion_rejects_fabricated_stat_with_correct_metadata(registry, frozen):
    evidence = _record_evidence(registry, frozen)
    stat_artifacts = {
        "return_series_artifact_sha256": "a" * 64,
        "split_manifest_sha256": "b" * 64,
        "benchmark_id": "bench",
        "sample_count": 8,
        "return_skewness": 0.0,
        "return_kurtosis": 3.0,
        "observed_sharpe": 0.1,
        "effective_trial_count": 3.0,
        "trial_correlation_artifact_sha256": "c" * 64,
        "selection_rule": "max",
        "researcher_decision_log_sha256": "d" * 64,
        "protocol_sha256": frozen.sha256,
        "trial_ids": ["t0", "t1", "t2"],
    }
    registry.record_validation("fake-dsr", "exp1", "dsr", stat_artifacts, {"value": 0.99})
    assert _promotion(registry, frozen, evidence=evidence).eligible is False


def test_promotion_rejects_forged_artifact_content(registry, frozen):
    evidence = _record_evidence(registry, frozen)
    forged = PromotionEvidence(
        snapshot_bytes=json.dumps(
            {"schema": "walk-forward-v1", "trial_id": "t0", "protocol_sha256": frozen.sha256, "fold_returns": [[0.5, 0.6], [0.5, 0.6]]}
        ).encode(),
        walk_forward_bytes=evidence.walk_forward_bytes,
        stress_bytes=evidence.stress_bytes,
        trial_ids=evidence.trial_ids,
    )
    record_artifact_validation(registry, "exp1", "walk_forward", forged.snapshot_bytes, frozen)
    forged = PromotionEvidence(
        snapshot_bytes=evidence.snapshot_bytes,
        walk_forward_bytes=forged.snapshot_bytes,
        stress_bytes=evidence.stress_bytes,
        trial_ids=evidence.trial_ids,
    )
    assert _promotion(registry, frozen, evidence=forged).eligible is False


def test_walk_forward_rejects_single_peak(tmp_path, frozen):
    reg = ResearchRegistry(str(tmp_path / "peak.db"))
    reg.register_experiment("exp1", "hyp", frozen.sha256, "d" * 64, "abc123", "c" * 64)
    reg.freeze_protocol("exp1", "test")
    peak_trials = [TRIALS[0], [-0.02, -0.01, -0.02, -0.01, -0.02, -0.01, -0.02, -0.01], [-0.01, -0.02, -0.01, -0.02, -0.01, -0.02, -0.01, -0.02]]
    for idx, returns in enumerate(peak_trials):
        reg.record_trial(f"t{idx}", "exp1", "group-a", "completed", PARAMS[idx], {"returns": returns})
    record_artifact_validation(reg, "exp1", "walk_forward", _wf_bytes(frozen), frozen)
    run = reg.latest_validation("exp1", "walk_forward")
    assert run["artifacts"]["passed"] is False
    reg.close()


def test_walk_forward_rejects_distant_parameters(tmp_path, frozen):
    reg = ResearchRegistry(str(tmp_path / "distant.db"))
    reg.register_experiment("exp1", "hyp", frozen.sha256, "d" * 64, "abc123", "c" * 64)
    reg.freeze_protocol("exp1", "test")
    distant = [{"ema": 20, "atr": 14}, {"ema": 40, "atr": 14}, {"ema": 20, "atr": 30}]
    for idx, returns in enumerate(TRIALS):
        reg.record_trial(f"t{idx}", "exp1", "group-a", "completed", distant[idx], {"returns": returns})
    record_artifact_validation(reg, "exp1", "walk_forward", _wf_bytes(frozen), frozen)
    assert reg.latest_validation("exp1", "walk_forward")["artifacts"]["passed"] is False
    reg.close()


def test_walk_forward_rejects_unregistered_trial(registry, frozen):
    payload = json.loads(_wf_bytes(frozen))
    payload["trial_id"] = "ghost"
    record_artifact_validation(registry, "exp1", "walk_forward", json.dumps(payload).encode(), frozen)
    assert registry.latest_validation("exp1", "walk_forward")["artifacts"]["passed"] is False


def test_promotion_computes_prospective_itself(registry, frozen):
    assert _promotion(registry, frozen, prospective_completeness=0.5).eligible is False
