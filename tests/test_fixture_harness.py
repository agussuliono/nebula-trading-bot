"""Fase 1 fixture harness acceptance tests (spec v1.1)."""

import hashlib

import pytest

from nebula.fixture_harness import (
    CatalogEntry,
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
    sign_catalog,
    verify_fixture,
)
from nebula.research_registry import ResearchRegistry

TEST_SEED = bytes(range(32))  # test-only catalog signing seed
ATTACKER_SEED = bytes(range(32, 64))
PUBLIC_BYTES = b"public-fixture-bytes"
SYNTH_BYTES = b"synthetic-fixture-bytes"
PUBLIC_HASH = hashlib.sha256(PUBLIC_BYTES).hexdigest()
SYNTH_HASH = hashlib.sha256(SYNTH_BYTES).hexdigest()
ENTRIES = {
    "public-btc-2024": CatalogEntry(
        source_id="public-btc-2024", kind="public", sha256=PUBLIC_HASH
    ),
    "synth-trend-v1": CatalogEntry(
        source_id="synth-trend-v1",
        kind="synthetic",
        sha256=SYNTH_HASH,
        generator="nebula-fixture-gen",
        generator_version="1.0.0",
        seed=42,
    ),
}
SIGNATURE = sign_catalog(ENTRIES, TEST_SEED)
TRIALS = [[0.01, -0.02, 0.03, 0.0], [0.02, 0.01, -0.01, 0.02]]
COMBOS = [([3.0, 2.0, 1.0], [3.0, 2.0, 1.0]), ([3.0, 2.0, 1.0], [3.0, 2.0, 1.0])]


def protocol(**overrides):
    base = dict(
        trial_group="group-a",
        hypothesis_family="regime_filter",
        dataset_hashes={"exploration": PUBLIC_HASH, "prospective": SYNTH_HASH},
        catalog_sha256=catalog_digest(ENTRIES),
        prospective_cutoff="2026-12-01T00:00:00Z",
        fee_bps=10.0,
        slippage_bps=5.0,
        promotion_threshold=0.6,
        prospective_min_observations=30,
        prospective_min_duration_days=14,
        prospective_min_completeness=0.95,
        preregistered_exclusions=frozenset(),
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
        reg.record_trial(f"t{idx}", "exp1", "group-a", "completed", {}, {"returns": returns})
    yield reg
    reg.close()


def public_manifest(**overrides):
    base = dict(
        dataset_id="ds1", source_id="public-btc-2024", kind="public", sha256=PUBLIC_HASH
    )
    base.update(overrides)
    return FixtureManifest(**base)


def test_signed_catalog_fixture_is_accepted(frozen):
    assert verify_fixture(public_manifest(), PUBLIC_BYTES, ENTRIES, SIGNATURE, frozen).accepted is True


def test_fake_catalog_with_new_protocol_is_rejected():
    demo_hash = hashlib.sha256(b"demo-bytes").hexdigest()
    fake = {
        "public-btc-2024": CatalogEntry(
            source_id="public-btc-2024", kind="public", sha256=demo_hash
        )
    }
    fake_frozen = freeze_protocol(
        protocol(
            catalog_sha256=catalog_digest(fake),
            dataset_hashes={"exploration": demo_hash},
        )
    )
    attacker_sig = sign_catalog(fake, ATTACKER_SEED)
    manifest = public_manifest(sha256=demo_hash)
    assert verify_fixture(manifest, b"demo-bytes", fake, attacker_sig, fake_frozen).accepted is False


def test_fixture_hash_must_be_in_frozen_dataset_hashes(frozen):
    other = freeze_protocol(protocol(dataset_hashes={"exploration": SYNTH_HASH}))
    assert verify_fixture(public_manifest(), PUBLIC_BYTES, ENTRIES, SIGNATURE, other).accepted is False


def test_freeze_rejects_invalid_threshold():
    with pytest.raises(ValueError):
        freeze_protocol(protocol(promotion_threshold=float("nan")))
    with pytest.raises(ValueError):
        freeze_protocol(protocol(promotion_threshold=1.5))


def test_pbo_post_performance_exclusion_event_is_inconclusive(registry, frozen):
    registry.record_candidate_exclusion("exp1", 1, "weak", performance_seen=True, actor="test")
    assert evaluate_pbo_gate(COMBOS, frozen, registry, "exp1").status == "inconclusive"


def test_dsr_uses_registry_returns(registry):
    artifacts = build_dsr_artifacts(registry, "exp1")
    assert evaluate_dsr_gate(registry, "exp1", "t0", artifacts).status == "ok"
    registry.record_trial("t2", "exp1", "group-a", "failed", {}, {"returns": [0.0, 0.0, 0.0, 0.0]})
    assert evaluate_dsr_gate(registry, "exp1", "t0", artifacts).status == "inconclusive"


def test_prospective_gate_rejects_non_finite(frozen):
    assert evaluate_prospective(frozen, 30, 14, float("nan"), "2026-12-01T00:00:00Z").eligible is False


def _record_promotion_evidence(registry, frozen):
    pbo = evaluate_pbo_gate(COMBOS, frozen, registry, "exp1")
    dsr = evaluate_dsr_gate(registry, "exp1", "t0", build_dsr_artifacts(registry, "exp1"))
    stat_artifacts = {
        "return_series_artifact_sha256": "a" * 64,
        "split_manifest_sha256": "b" * 64,
        "benchmark_id": "bench",
        "sample_count": 4,
        "return_skewness": 0.0,
        "return_kurtosis": 3.0,
        "observed_sharpe": 0.1,
        "effective_trial_count": 2.0,
        "trial_correlation_artifact_sha256": "c" * 64,
        "selection_rule": "max",
        "researcher_decision_log_sha256": "d" * 64,
    }
    registry.record_validation("pbo1", "exp1", "pbo", stat_artifacts, {"value": pbo.value})
    registry.record_validation("dsr1", "exp1", "dsr", stat_artifacts, {"value": dsr.value})
    registry.record_validation("snap1", "exp1", "snapshot", {"artifact_sha256": "e" * 64, "passed": True}, {})
    registry.record_validation("wf1", "exp1", "walk_forward", {"artifact_sha256": "f" * 64, "passed": True}, {})
    registry.record_validation("stress1", "exp1", "stress", {"artifact_sha256": "1" * 64, "passed": True}, {})
    return PromotionEvidence(
        snapshot_sha256="e" * 64,
        walk_forward_sha256="f" * 64,
        stress_sha256="1" * 64,
        trial_ids=("t0", "t1"),
    )


def _promotion(registry, frozen, evidence=None, **overrides):
    base = dict(
        frozen=frozen,
        protocol=protocol(),
        registry=registry,
        experiment_id="exp1",
        fixture_decision=verify_fixture(public_manifest(), PUBLIC_BYTES, ENTRIES, SIGNATURE, frozen),
        prospective=evaluate_prospective(frozen, 30, 14, 0.95, "2026-12-01T00:00:00Z"),
        evidence=evidence or _record_promotion_evidence(registry, frozen),
    )
    base.update(overrides)
    return evaluate_promotion(**base)


def test_promotion_reads_registry_validations(registry, frozen):
    assert _promotion(registry, frozen).eligible is True


def test_promotion_rejects_dummy_evidence_hash(registry, frozen):
    evidence = _record_promotion_evidence(registry, frozen)
    dummy = PromotionEvidence(
        snapshot_sha256="a" * 64,
        walk_forward_sha256=evidence.walk_forward_sha256,
        stress_sha256=evidence.stress_sha256,
        trial_ids=evidence.trial_ids,
    )
    assert _promotion(registry, frozen, evidence=dummy).eligible is False


def test_promotion_without_registry_validations_is_rejected(registry, frozen):
    evidence = PromotionEvidence(
        snapshot_sha256="e" * 64,
        walk_forward_sha256="f" * 64,
        stress_sha256="1" * 64,
        trial_ids=("t0", "t1"),
    )
    assert _promotion(registry, frozen, evidence=evidence).eligible is False
