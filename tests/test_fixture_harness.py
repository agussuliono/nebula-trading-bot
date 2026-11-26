"""Fase 1 fixture harness acceptance tests (spec v1.1)."""

import hashlib

import pytest

from nebula.fixture_harness import (
    CatalogEntry,
    DsrArtifacts,
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
    verify_fixture,
)
from nebula.research_registry import ResearchRegistry

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
    reg.record_trial("t0", "exp1", "group-a", "completed", {}, {})
    reg.record_trial("t1", "exp1", "group-a", "completed", {}, {})
    yield reg
    reg.close()


def public_manifest(**overrides):
    base = dict(
        dataset_id="ds1", source_id="public-btc-2024", kind="public", sha256=PUBLIC_HASH
    )
    base.update(overrides)
    return FixtureManifest(**base)


def test_public_fixture_is_accepted(frozen):
    assert verify_fixture(public_manifest(), PUBLIC_BYTES, ENTRIES, frozen).accepted is True


def test_fake_catalog_with_recomputed_digest_is_rejected(frozen):
    demo_hash = hashlib.sha256(b"demo-bytes").hexdigest()
    fake = {
        "public-btc-2024": CatalogEntry(
            source_id="public-btc-2024", kind="public", sha256=demo_hash
        )
    }
    manifest = public_manifest(sha256=demo_hash)
    assert verify_fixture(manifest, b"demo-bytes", fake, frozen).accepted is False


def test_dataset_bytes_mismatch_is_rejected(frozen):
    assert verify_fixture(public_manifest(), b"tampered", ENTRIES, frozen).accepted is False


def test_demo_kind_is_rejected_before_gate_passes(frozen):
    assert (
        verify_fixture(public_manifest(kind="demo"), PUBLIC_BYTES, ENTRIES, frozen).accepted
        is False
    )


def test_synthetic_fixture_requires_generator_metadata():
    broken = {
        "synth-trend-v1": CatalogEntry(
            source_id="synth-trend-v1", kind="synthetic", sha256=SYNTH_HASH
        )
    }
    frozen_broken = freeze_protocol(protocol(catalog_sha256=catalog_digest(broken)))
    manifest = FixtureManifest(
        dataset_id="ds2", source_id="synth-trend-v1", kind="synthetic", sha256=SYNTH_HASH
    )
    assert verify_fixture(manifest, SYNTH_BYTES, broken, frozen_broken).accepted is False


def test_protocol_dataset_hashes_are_immutable():
    hashes = {"exploration": PUBLIC_HASH}
    candidate = protocol(dataset_hashes=hashes)
    hashes["exploration"] = "f" * 64
    assert candidate.dataset_hashes["exploration"] == PUBLIC_HASH
    with pytest.raises(TypeError):
        candidate.dataset_hashes["exploration"] = "f" * 64  # type: ignore[index]


def test_pbo_exclusions_come_from_frozen_protocol(registry, frozen):
    missing = [([1.0, None, 3.0], [3.0, 2.0, 1.0]), ([3.0, 2.0, 1.0], [1.0, 2.0, 3.0])]
    assert evaluate_pbo_gate(missing, frozen, registry, "exp1").status == "inconclusive"
    frozen_excl = freeze_protocol(protocol(preregistered_exclusions=frozenset({1})))
    registry.register_experiment("exp2", "hyp", frozen_excl.sha256, "d" * 64, "abc123", "c" * 64)
    registry.freeze_protocol("exp2", "test")
    assert evaluate_pbo_gate(missing, frozen_excl, registry, "exp2").status == "ok"


def test_pbo_post_performance_exclusion_from_registry_is_inconclusive(registry, frozen):
    registry.record_trial(
        "t2", "exp1", "group-a", "invalid", {},
        {"candidate_index": 1, "excluded_after_performance": True},
    )
    assert evaluate_pbo_gate(COMBOS, frozen, registry, "exp1").status == "inconclusive"


def test_dsr_gate_reconciles_registry_trial_count(registry):
    artifacts = build_dsr_artifacts(TRIALS, registry_trial_count=2)
    assert evaluate_dsr_gate(TRIALS, 0, artifacts, registry, "exp1").status == "ok"
    registry.record_trial("t2", "exp1", "group-a", "failed", {}, {})
    assert evaluate_dsr_gate(TRIALS, 0, artifacts, registry, "exp1").status == "inconclusive"


def test_dsr_non_finite_input_is_inconclusive(registry):
    trials = [[float("nan"), 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]]
    artifacts = build_dsr_artifacts(trials, registry_trial_count=2)
    assert evaluate_dsr_gate(trials, 0, artifacts, registry, "exp1").status == "inconclusive"


def test_prospective_gate_rejects_non_finite_and_early(frozen):
    assert evaluate_prospective(frozen, 30, 14, float("nan"), "2026-12-01T00:00:00Z").eligible is False
    assert evaluate_prospective(frozen, 30, 14, 0.95, "2026-11-30T23:59:59Z").eligible is False
    assert evaluate_prospective(frozen, 30, 14, 0.95, "2026-12-01T00:00:00Z").eligible is True


def _evidence(**overrides):
    base = dict(
        snapshot_sha256="a" * 64,
        walk_forward_sha256="b" * 64,
        walk_forward_plateau=True,
        stress_sha256="c" * 64,
        stress_passed=True,
        trial_ids=("t0", "t1"),
    )
    base.update(overrides)
    return PromotionEvidence(**base)


def _promotion(registry, frozen, **overrides):
    base = dict(
        frozen=frozen,
        protocol=protocol(),
        registry=registry,
        experiment_id="exp1",
        fixture_decision=verify_fixture(public_manifest(), PUBLIC_BYTES, ENTRIES, frozen),
        prospective=evaluate_prospective(frozen, 30, 14, 0.95, "2026-12-01T00:00:00Z"),
        pbo=evaluate_pbo_gate(COMBOS, frozen, registry, "exp1"),
        dsr=evaluate_dsr_gate(
            TRIALS, 0, build_dsr_artifacts(TRIALS, 2), registry, "exp1"
        ),
        evidence=_evidence(),
    )
    base.update(overrides)
    return evaluate_promotion(**base)


def test_promotion_eligible_only_when_every_gate_passes(registry, frozen):
    assert _promotion(registry, frozen).eligible is True
    assert _promotion(registry, frozen, evidence=_evidence(stress_passed=False)).eligible is False
    assert _promotion(registry, frozen, evidence=_evidence(trial_ids=("t0",))).eligible is False
    assert _promotion(registry, frozen, protocol=protocol(fee_bps=99.0)).eligible is False


def test_promotion_requires_registry_freeze(tmp_path, frozen):
    reg = ResearchRegistry(str(tmp_path / "r.db"))
    reg.register_experiment("exp1", "hyp", frozen.sha256, "d" * 64, "abc123", "c" * 64)
    reg.record_trial("t0", "exp1", "group-a", "completed", {}, {})
    reg.record_trial("t1", "exp1", "group-a", "completed", {}, {})
    assert _promotion(reg, frozen).eligible is False
    reg.close()


def test_promotion_threshold_is_applied(registry):
    strict = freeze_protocol(protocol(promotion_threshold=0.999))
    registry.register_experiment("exp2", "hyp", strict.sha256, "d" * 64, "abc123", "c" * 64)
    registry.freeze_protocol("exp2", "test")
    registry.record_trial("u0", "exp2", "group-a", "completed", {}, {})
    registry.record_trial("u1", "exp2", "group-a", "completed", {}, {})
    decision = _promotion(
        registry,
        strict,
        protocol=protocol(promotion_threshold=0.999),
        experiment_id="exp2",
        evidence=_evidence(trial_ids=("u0", "u1")),
        pbo=evaluate_pbo_gate(COMBOS, strict, registry, "exp2"),
        dsr=evaluate_dsr_gate(TRIALS, 0, build_dsr_artifacts(TRIALS, 2), registry, "exp2"),
    )
    assert decision.eligible is False
