"""Fase 1 fixture harness acceptance tests (spec v1.1)."""

import pytest

from nebula.fixture_harness import (
    CatalogEntry,
    FixtureManifest,
    Protocol,
    evaluate_dsr_gate,
    evaluate_pbo_gate,
    evaluate_promotion,
    evaluate_prospective,
    freeze_protocol,
    verify_fixture,
)

PUBLIC_HASH = "a" * 64
SYNTH_HASH = "b" * 64
CATALOG = {
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


def test_public_fixture_with_trusted_catalog_entry_is_accepted():
    manifest = FixtureManifest(
        dataset_id="ds1", source_id="public-btc-2024", kind="public", sha256=PUBLIC_HASH
    )
    assert verify_fixture(manifest, CATALOG).accepted is True


def test_fixture_without_manifest_hash_is_rejected():
    manifest = FixtureManifest(
        dataset_id="ds1", source_id="public-btc-2024", kind="public", sha256=""
    )
    decision = verify_fixture(manifest, CATALOG)
    assert decision.accepted is False
    assert "manifest hash" in decision.reasons[0]


def test_unregistered_public_source_is_rejected():
    manifest = FixtureManifest(
        dataset_id="ds1", source_id="unknown-source", kind="public", sha256=PUBLIC_HASH
    )
    assert verify_fixture(manifest, CATALOG).accepted is False


def test_demo_dataset_relabeled_public_with_recomputed_hash_is_rejected():
    # Attacker relabels a Demo dataset as public and recomputes its hash;
    # the hash still does not match the trusted catalog entry.
    manifest = FixtureManifest(
        dataset_id="ds1", source_id="public-btc-2024", kind="public", sha256="c" * 64
    )
    assert verify_fixture(manifest, CATALOG).accepted is False


def test_demo_kind_is_rejected_before_gate_passes():
    manifest = FixtureManifest(
        dataset_id="ds1", source_id="public-btc-2024", kind="demo", sha256=PUBLIC_HASH
    )
    assert verify_fixture(manifest, CATALOG, deployment_gate_passed=False).accepted is False


def test_synthetic_fixture_requires_generator_metadata():
    manifest = FixtureManifest(
        dataset_id="ds2", source_id="synth-trend-v1", kind="synthetic", sha256=SYNTH_HASH
    )
    assert verify_fixture(manifest, CATALOG).accepted is True
    broken_catalog = {
        "synth-trend-v1": CatalogEntry(
            source_id="synth-trend-v1", kind="synthetic", sha256=SYNTH_HASH
        )
    }
    assert verify_fixture(manifest, broken_catalog).accepted is False


def test_frozen_protocol_cannot_change_without_new_trial_group():
    frozen = freeze_protocol(protocol())
    changed = protocol(fee_bps=20.0)
    with pytest.raises(ValueError):
        frozen.assert_unchanged(changed)
    frozen.assert_unchanged(protocol())


def test_protocol_rejects_unknown_hypothesis_family():
    with pytest.raises(ValueError):
        freeze_protocol(protocol(hypothesis_family="llm_veto"))


def test_pbo_post_performance_exclusion_is_inconclusive():
    combos = [([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]), ([3.0, 2.0, 1.0], [1.0, 2.0, 3.0])]
    result = evaluate_pbo_gate(combos, post_performance_exclusions={1})
    assert result.status == "inconclusive"


def test_pbo_preregistered_exclusion_is_allowed():
    combos = [([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]), ([3.0, 2.0, 1.0], [1.0, 2.0, 3.0])]
    result = evaluate_pbo_gate(combos, preregistered_exclusions={1})
    assert result.status == "ok"


def test_pbo_missing_artifact_is_inconclusive():
    assert evaluate_pbo_gate(None).status == "inconclusive"
    assert evaluate_pbo_gate([]).status == "inconclusive"


def test_dsr_missing_artifact_is_inconclusive():
    trials = [[0.01, -0.02, 0.03, 0.0], [0.02, 0.01, -0.01, 0.02]]
    assert evaluate_dsr_gate(trials, 0, artifacts_present=False).status == "inconclusive"
    assert evaluate_dsr_gate(trials, 0, artifacts_present=True).status == "ok"


def test_prospective_gate_requires_all_thresholds():
    frozen = freeze_protocol(protocol())
    ok = evaluate_prospective(frozen, observations=30, duration_days=14, completeness=0.95)
    assert ok.eligible is True
    assert evaluate_prospective(frozen, 29, 14, 0.95).eligible is False
    assert evaluate_prospective(frozen, 30, 13, 0.95).eligible is False
    assert evaluate_prospective(frozen, 30, 14, 0.94).eligible is False


def test_promotion_blocked_when_any_gate_fails():
    frozen = freeze_protocol(protocol())
    combos = [([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]), ([3.0, 2.0, 1.0], [1.0, 2.0, 3.0])]
    trials = [[0.01, -0.02, 0.03, 0.0], [0.02, 0.01, -0.01, 0.02]]
    manifest = FixtureManifest(
        dataset_id="ds1", source_id="public-btc-2024", kind="public", sha256=PUBLIC_HASH
    )
    decision = evaluate_promotion(
        frozen=frozen,
        fixture_decision=verify_fixture(manifest, CATALOG),
        prospective=evaluate_prospective(frozen, 30, 14, 0.95),
        pbo=evaluate_pbo_gate(combos),
        dsr=evaluate_dsr_gate(trials, 0, artifacts_present=True),
        snapshot_complete=True,
        all_trials_recorded=True,
    )
    assert decision.eligible is True
    blocked = evaluate_promotion(
        frozen=frozen,
        fixture_decision=verify_fixture(manifest, CATALOG),
        prospective=evaluate_prospective(frozen, 10, 14, 0.95),
        pbo=evaluate_pbo_gate(combos),
        dsr=evaluate_dsr_gate(trials, 0, artifacts_present=True),
        snapshot_complete=True,
        all_trials_recorded=True,
    )
    assert blocked.eligible is False
    assert blocked.reasons
