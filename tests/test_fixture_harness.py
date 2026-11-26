"""Fase 1 fixture harness acceptance tests (spec v1.1)."""

import hashlib

import pytest

from nebula.fixture_harness import (
    CatalogEntry,
    DsrArtifacts,
    FixtureManifest,
    Protocol,
    TrustedCatalog,
    build_dsr_artifacts,
    catalog_digest,
    evaluate_dsr_gate,
    evaluate_pbo_gate,
    evaluate_promotion,
    evaluate_prospective,
    freeze_protocol,
    verify_fixture,
)

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
CATALOG = TrustedCatalog(entries=ENTRIES, trusted_sha256=catalog_digest(ENTRIES))


def protocol(**overrides):
    base = dict(
        trial_group="group-a",
        hypothesis_family="regime_filter",
        dataset_hashes={"exploration": PUBLIC_HASH, "prospective": SYNTH_HASH},
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


def public_manifest(**overrides):
    base = dict(
        dataset_id="ds1", source_id="public-btc-2024", kind="public", sha256=PUBLIC_HASH
    )
    base.update(overrides)
    return FixtureManifest(**base)


def test_public_fixture_with_trusted_catalog_entry_is_accepted():
    assert verify_fixture(public_manifest(), PUBLIC_BYTES, CATALOG).accepted is True


def test_fixture_without_manifest_hash_is_rejected():
    decision = verify_fixture(public_manifest(sha256=""), PUBLIC_BYTES, CATALOG)
    assert decision.accepted is False
    assert "manifest hash" in decision.reasons[0]


def test_dataset_bytes_mismatch_is_rejected():
    assert verify_fixture(public_manifest(), b"tampered", CATALOG).accepted is False


def test_fake_catalog_is_rejected():
    fake_entries = {
        "public-btc-2024": CatalogEntry(
            source_id="public-btc-2024",
            kind="public",
            sha256=hashlib.sha256(b"demo-bytes").hexdigest(),
        )
    }
    fake = TrustedCatalog(entries=fake_entries, trusted_sha256=CATALOG.trusted_sha256)
    manifest = public_manifest(sha256=hashlib.sha256(b"demo-bytes").hexdigest())
    assert verify_fixture(manifest, b"demo-bytes", fake).accepted is False


def test_unregistered_public_source_is_rejected():
    assert (
        verify_fixture(
            public_manifest(source_id="unknown-source"), PUBLIC_BYTES, CATALOG
        ).accepted
        is False
    )


def test_demo_kind_is_rejected_before_gate_passes():
    assert (
        verify_fixture(
            public_manifest(kind="demo"), PUBLIC_BYTES, CATALOG, deployment_gate_passed=False
        ).accepted
        is False
    )


def test_synthetic_fixture_requires_generator_metadata():
    manifest = FixtureManifest(
        dataset_id="ds2", source_id="synth-trend-v1", kind="synthetic", sha256=SYNTH_HASH
    )
    assert verify_fixture(manifest, SYNTH_BYTES, CATALOG).accepted is True
    broken_entries = {
        "synth-trend-v1": CatalogEntry(
            source_id="synth-trend-v1", kind="synthetic", sha256=SYNTH_HASH
        )
    }
    broken = TrustedCatalog(
        entries=broken_entries, trusted_sha256=catalog_digest(broken_entries)
    )
    assert verify_fixture(manifest, SYNTH_BYTES, broken).accepted is False


def test_frozen_protocol_cannot_change_without_new_trial_group():
    frozen = freeze_protocol(protocol())
    with pytest.raises(ValueError):
        frozen.assert_unchanged(protocol(fee_bps=20.0))
    frozen.assert_unchanged(protocol())


def test_protocol_dataset_hashes_are_immutable_after_construction():
    hashes = {"exploration": PUBLIC_HASH}
    candidate = protocol(dataset_hashes=hashes)
    hashes["exploration"] = "f" * 64
    assert candidate.dataset_hashes["exploration"] == PUBLIC_HASH
    with pytest.raises(TypeError):
        candidate.dataset_hashes["exploration"] = "f" * 64  # type: ignore[index]


def test_protocol_rejects_unknown_hypothesis_family():
    with pytest.raises(ValueError):
        freeze_protocol(protocol(hypothesis_family="llm_veto"))


def test_pbo_post_performance_exclusion_is_inconclusive():
    combos = [([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]), ([3.0, 2.0, 1.0], [1.0, 2.0, 3.0])]
    frozen = freeze_protocol(protocol())
    result = evaluate_pbo_gate(combos, frozen, post_performance_exclusions={1})
    assert result.status == "inconclusive"


def test_pbo_exclusions_come_from_frozen_protocol():
    combos = [([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]), ([3.0, 2.0, 1.0], [1.0, 2.0, 3.0])]
    frozen = freeze_protocol(protocol(preregistered_exclusions=frozenset({1})))
    assert evaluate_pbo_gate(combos, frozen).status == "ok"
    unfrozen = freeze_protocol(protocol())
    # Candidate 1 has complete scores, so without a frozen exclusion PBO is
    # still computable; the binding is proven by the missing-score case:
    missing = [([1.0, None, 3.0], [3.0, 2.0, 1.0]), ([3.0, 2.0, 1.0], [1.0, 2.0, 3.0])]
    assert evaluate_pbo_gate(missing, unfrozen).status == "inconclusive"
    assert evaluate_pbo_gate(missing, frozen).status == "ok"


def test_pbo_missing_artifact_is_inconclusive():
    frozen = freeze_protocol(protocol())
    assert evaluate_pbo_gate(None, frozen).status == "inconclusive"
    assert evaluate_pbo_gate([], frozen).status == "inconclusive"


def test_dsr_gate_requires_bound_artifacts():
    trials = [[0.01, -0.02, 0.03, 0.0], [0.02, 0.01, -0.01, 0.02]]
    artifacts = build_dsr_artifacts(trials, registry_trial_count=2)
    assert evaluate_dsr_gate(trials, 0, artifacts).status == "ok"
    tampered = DsrArtifacts(
        trial_returns_sha256="0" * 64,
        correlation_sha256=artifacts.correlation_sha256,
        sample_count=artifacts.sample_count,
        registry_trial_count=2,
    )
    assert evaluate_dsr_gate(trials, 0, tampered).status == "inconclusive"
    wrong_count = build_dsr_artifacts(trials, registry_trial_count=3)
    assert evaluate_dsr_gate(trials, 0, wrong_count).status == "inconclusive"


def test_dsr_non_finite_input_is_inconclusive():
    trials = [[float("nan"), 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]]
    artifacts = build_dsr_artifacts(trials, registry_trial_count=2)
    assert evaluate_dsr_gate(trials, 0, artifacts).status == "inconclusive"


def test_prospective_gate_requires_all_thresholds_and_cutoff():
    frozen = freeze_protocol(protocol())
    ok = evaluate_prospective(frozen, 30, 14, 0.95, "2026-12-01T00:00:00Z")
    assert ok.eligible is True
    assert evaluate_prospective(frozen, 29, 14, 0.95, "2026-12-01T00:00:00Z").eligible is False
    assert evaluate_prospective(frozen, 30, 13, 0.95, "2026-12-01T00:00:00Z").eligible is False
    assert evaluate_prospective(frozen, 30, 14, 0.94, "2026-12-01T00:00:00Z").eligible is False
    assert evaluate_prospective(frozen, 30, 14, 0.95, "2026-11-30T23:59:59Z").eligible is False


def test_prospective_gate_rejects_non_finite_and_malformed():
    frozen = freeze_protocol(protocol())
    assert evaluate_prospective(frozen, 30, 14, float("nan"), "2026-12-01T00:00:00Z").eligible is False
    assert evaluate_prospective(frozen, 30, 14, float("inf"), "2026-12-01T00:00:00Z").eligible is False
    assert evaluate_prospective(frozen, 30, 14, 0.95, "not-a-time").eligible is False


def _promotion_inputs(**overrides):
    frozen = freeze_protocol(protocol())
    combos = [([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]), ([3.0, 2.0, 1.0], [1.0, 2.0, 3.0])]
    trials = [[0.01, -0.02, 0.03, 0.0], [0.02, 0.01, -0.01, 0.02]]
    base = dict(
        frozen=frozen,
        protocol=protocol(),
        registry_protocol_sha256=frozen.sha256,
        fixture_decision=verify_fixture(public_manifest(), PUBLIC_BYTES, CATALOG),
        prospective=evaluate_prospective(frozen, 30, 14, 0.95, "2026-12-01T00:00:00Z"),
        pbo=evaluate_pbo_gate(combos, frozen),
        dsr=evaluate_dsr_gate(trials, 0, build_dsr_artifacts(trials, 2)),
        snapshot_complete=True,
        all_trials_recorded=True,
        cumulative_trials_reconstructed=True,
        walk_forward_plateau=True,
        stress_passed=True,
    )
    base.update(overrides)
    return base


def test_promotion_eligible_only_when_every_gate_passes():
    assert evaluate_promotion(**_promotion_inputs()).eligible is True
    assert evaluate_promotion(**_promotion_inputs(stress_passed=False)).eligible is False
    assert evaluate_promotion(**_promotion_inputs(walk_forward_plateau=False)).eligible is False
    assert (
        evaluate_promotion(**_promotion_inputs(cumulative_trials_reconstructed=False)).eligible
        is False
    )
    assert (
        evaluate_promotion(**_promotion_inputs(protocol=protocol(fee_bps=99.0))).eligible
        is False
    )
    assert (
        evaluate_promotion(**_promotion_inputs(registry_protocol_sha256="0" * 64)).eligible
        is False
    )
