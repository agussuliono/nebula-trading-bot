"""Fase 1 offline fixture harness (spec v1.1).

Research-only module. It verifies fixture provenance against a pinned
trusted catalog and the actual dataset bytes, freezes experiment
protocols immutably, and gates promotion on prospective-sample
sufficiency plus bound PBO/DSR artifacts. It never reads trading state,
never touches the network, and has no path to order placement. Demo
data is rejected until the Deployment Security Gate passes; every
verification failure fails closed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping, Optional, Sequence

from nebula.research_registry import (
    StatResult,
    canonical_json,
    cscv_pbo,
    deflated_sharpe_ratio,
    pearson_correlation,
    sha256_hex,
)

ALLOWED_HYPOTHESIS_FAMILIES = frozenset(
    {"regime_filter", "ema_atr_plateau", "volatility_position_scale"}
)
FIXTURE_KINDS = frozenset({"public", "synthetic"})


@dataclass(frozen=True)
class CatalogEntry:
    """Trusted fixture catalog record, maintained separately from manifests."""

    source_id: str
    kind: str
    sha256: str
    generator: str = ""
    generator_version: str = ""
    seed: Optional[int] = None


def catalog_digest(entries: Mapping[str, CatalogEntry]) -> str:
    """Canonical digest of a catalog, used as its trust-anchor pin."""
    payload = {
        source_id: {
            "kind": entry.kind,
            "sha256": entry.sha256,
            "generator": entry.generator,
            "generator_version": entry.generator_version,
            "seed": entry.seed,
        }
        for source_id, entry in sorted(entries.items())
    }
    return sha256_hex(canonical_json(payload))


@dataclass(frozen=True)
class TrustedCatalog:
    """Catalog plus the separately pinned digest that authenticates it."""

    entries: Mapping[str, CatalogEntry]
    trusted_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", MappingProxyType(dict(self.entries)))

    def authenticated(self) -> bool:
        return catalog_digest(self.entries) == self.trusted_sha256


@dataclass(frozen=True)
class FixtureManifest:
    dataset_id: str
    source_id: str
    kind: str
    sha256: str


@dataclass(frozen=True)
class FixtureDecision:
    accepted: bool
    reasons: tuple[str, ...] = ()


def verify_fixture(
    manifest: FixtureManifest,
    dataset: bytes,
    catalog_entries: Mapping[str, CatalogEntry],
    frozen: "FrozenProtocol",
    *,
    deployment_gate_passed: bool = False,
) -> FixtureDecision:
    """Accept a fixture only when provenance verifies end to end.

    The catalog must match its pinned digest, the manifest hash must
    match the actual dataset bytes, and the source, kind, and hash must
    match the authenticated catalog entry. Synthetic fixtures also
    require verifiable generator metadata.
    """
    reasons: list[str] = []
    if not frozen.verify():
        reasons.append("frozen protocol hash mismatch")
    if catalog_digest(catalog_entries) != frozen.protocol.catalog_sha256:
        reasons.append("fixture catalog does not match frozen trust anchor")
    if not manifest.sha256:
        reasons.append("manifest hash missing")
    elif hashlib.sha256(dataset).hexdigest() != manifest.sha256:
        reasons.append("dataset bytes do not match manifest hash")
    if manifest.kind not in FIXTURE_KINDS:
        if not deployment_gate_passed:
            reasons.append("non-fixture data rejected before deployment gate passes")
        else:
            reasons.append("fixture kind must be public or synthetic")
    entry = catalog_entries.get(manifest.source_id)
    if entry is None:
        reasons.append("source not in trusted fixture catalog")
    else:
        if entry.kind != manifest.kind:
            reasons.append("declared kind does not match catalog kind")
        if manifest.sha256 and entry.sha256 != manifest.sha256:
            reasons.append("manifest hash does not match catalog hash")
        if entry.kind == "synthetic" and (
            not entry.generator or not entry.generator_version or entry.seed is None
        ):
            reasons.append("synthetic fixture generator metadata incomplete")
    return FixtureDecision(accepted=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True)
class Protocol:
    trial_group: str
    hypothesis_family: str
    dataset_hashes: Mapping[str, str]
    catalog_sha256: str
    prospective_cutoff: str
    fee_bps: float
    slippage_bps: float
    promotion_threshold: float
    prospective_min_observations: int
    prospective_min_duration_days: int
    prospective_min_completeness: float
    preregistered_exclusions: frozenset = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "dataset_hashes", MappingProxyType(dict(self.dataset_hashes))
        )
        object.__setattr__(
            self, "preregistered_exclusions", frozenset(self.preregistered_exclusions)
        )

    def canonical(self) -> str:
        return canonical_json(
            {
                "trial_group": self.trial_group,
                "hypothesis_family": self.hypothesis_family,
                "dataset_hashes": dict(self.dataset_hashes),
                "catalog_sha256": self.catalog_sha256,
                "prospective_cutoff": self.prospective_cutoff,
                "fee_bps": self.fee_bps,
                "slippage_bps": self.slippage_bps,
                "promotion_threshold": self.promotion_threshold,
                "prospective_min_observations": self.prospective_min_observations,
                "prospective_min_duration_days": self.prospective_min_duration_days,
                "prospective_min_completeness": self.prospective_min_completeness,
                "preregistered_exclusions": sorted(self.preregistered_exclusions),
            }
        )


@dataclass(frozen=True)
class FrozenProtocol:
    protocol: Protocol
    sha256: str

    def assert_unchanged(self, candidate: Protocol) -> None:
        if sha256_hex(candidate.canonical()) != self.sha256:
            raise ValueError(
                "protocol changed after freeze; a new trial group is required"
            )

    def verify(self) -> bool:
        return sha256_hex(self.protocol.canonical()) == self.sha256


def freeze_protocol(protocol: Protocol) -> FrozenProtocol:
    if protocol.hypothesis_family not in ALLOWED_HYPOTHESIS_FAMILIES:
        raise ValueError("hypothesis family is outside the frozen Fase 1 families")
    if protocol.prospective_min_observations < 1:
        raise ValueError("prospective minimum observations must be positive")
    if protocol.prospective_min_duration_days < 1:
        raise ValueError("prospective minimum duration must be positive")
    if not 0.0 < protocol.prospective_min_completeness <= 1.0:
        raise ValueError("prospective minimum completeness must be within (0, 1]")
    if not protocol.dataset_hashes or any(
        not value for value in protocol.dataset_hashes.values()
    ):
        raise ValueError("dataset hashes are required before freeze")
    _parse_timestamp(protocol.prospective_cutoff)
    return FrozenProtocol(protocol=protocol, sha256=sha256_hex(protocol.canonical()))


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ProspectiveDecision:
    eligible: bool
    reasons: tuple[str, ...] = ()


def evaluate_prospective(
    frozen: FrozenProtocol,
    observations: int,
    duration_days: int,
    completeness: float,
    first_observation_at: str,
) -> ProspectiveDecision:
    protocol = frozen.protocol
    reasons: list[str] = []
    if not frozen.verify():
        reasons.append("frozen protocol hash mismatch")
    if not isinstance(observations, int) or isinstance(observations, bool) or observations < 0:
        reasons.append("prospective observations malformed")
    elif observations < protocol.prospective_min_observations:
        reasons.append("prospective observations below frozen minimum")
    if not isinstance(duration_days, int) or isinstance(duration_days, bool) or duration_days < 0:
        reasons.append("prospective duration malformed")
    elif duration_days < protocol.prospective_min_duration_days:
        reasons.append("prospective duration below frozen minimum")
    if (
        not isinstance(completeness, (int, float))
        or isinstance(completeness, bool)
        or not math.isfinite(completeness)
        or not 0.0 <= completeness <= 1.0
    ):
        reasons.append("prospective completeness malformed")
    elif completeness < protocol.prospective_min_completeness:
        reasons.append("prospective completeness below frozen minimum")
    try:
        first = _parse_timestamp(first_observation_at)
        if first < _parse_timestamp(protocol.prospective_cutoff):
            reasons.append("prospective observation before frozen cutoff")
    except (ValueError, TypeError):
        reasons.append("prospective timestamp malformed")
    return ProspectiveDecision(eligible=not reasons, reasons=tuple(reasons))


def evaluate_pbo_gate(
    combinations: Optional[Sequence[tuple[Sequence[Optional[float]], Sequence[Optional[float]]]]],
    frozen: FrozenProtocol,
    registry,
    experiment_id: str,
) -> StatResult:
    """PBO gate: exclusions come from the frozen protocol and registry."""
    if not frozen.verify():
        return StatResult.inconclusive("frozen protocol hash mismatch")
    if not registry.protocol_frozen(experiment_id, frozen.sha256):
        return StatResult.inconclusive("protocol not frozen in registry")
    if registry.post_performance_exclusions(experiment_id):
        return StatResult.inconclusive(
            "candidate excluded after performance was seen"
        )
    if not combinations:
        return StatResult.inconclusive("CSCV artifact missing")
    return cscv_pbo(
        combinations,
        preregistered_exclusions=set(frozen.protocol.preregistered_exclusions),
    )


@dataclass(frozen=True)
class DsrArtifacts:
    """Hashes and counts binding a DSR claim to its trial data."""

    trial_returns_sha256: str
    correlation_sha256: str
    sample_count: int
    registry_trial_count: int


def _trials_digest(trials: Sequence[Sequence[float]]) -> str:
    return sha256_hex(canonical_json({"trials": [list(t) for t in trials]}))


def _correlation_digest(trials: Sequence[Sequence[float]]) -> str:
    matrix = [
        [pearson_correlation(a, b) for b in trials] for a in trials
    ]
    return sha256_hex(canonical_json({"correlation": matrix}))


def build_dsr_artifacts(
    trials: Sequence[Sequence[float]], registry_trial_count: int
) -> DsrArtifacts:
    sample_count = len(trials[0]) if trials else 0
    return DsrArtifacts(
        trial_returns_sha256=_trials_digest(trials),
        correlation_sha256=_correlation_digest(trials),
        sample_count=sample_count,
        registry_trial_count=registry_trial_count,
    )


def evaluate_dsr_gate(
    trials: Sequence[Sequence[float]],
    selected_index: int,
    artifacts: DsrArtifacts,
    registry,
    experiment_id: str,
) -> StatResult:
    """DSR gate: artifacts must bind to the actual trial returns."""
    if not trials:
        return StatResult.inconclusive("DSR artifact missing")
    if any(not math.isfinite(value) for trial in trials for value in trial):
        return StatResult.inconclusive("non-finite trial return")
    if artifacts.trial_returns_sha256 != _trials_digest(trials):
        return StatResult.inconclusive("trial returns artifact mismatch")
    if artifacts.correlation_sha256 != _correlation_digest(trials):
        return StatResult.inconclusive("correlation artifact mismatch")
    if artifacts.registry_trial_count != len(trials):
        return StatResult.inconclusive("registry trial count mismatch")
    if registry.trial_count(experiment_id) != len(trials):
        return StatResult.inconclusive("trial set does not cover registry trials")
    if any(len(trial) != artifacts.sample_count for trial in trials):
        return StatResult.inconclusive("sample count mismatch")
    return deflated_sharpe_ratio(trials, selected_index)


@dataclass(frozen=True)
class PromotionEvidence:
    """Artifact-bound promotion evidence; hashes must be 64-hex digests."""

    snapshot_sha256: str
    walk_forward_sha256: str
    walk_forward_plateau: bool
    stress_sha256: str
    stress_passed: bool
    trial_ids: tuple[str, ...]


def _is_digest(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def evaluate_promotion(
    *,
    frozen: FrozenProtocol,
    protocol: Protocol,
    registry,
    experiment_id: str,
    fixture_decision: FixtureDecision,
    prospective: ProspectiveDecision,
    pbo: StatResult,
    dsr: StatResult,
    evidence: PromotionEvidence,
) -> PromotionDecision:
    """Promotion requires registry-bound evidence and the frozen threshold.

    The frozen promotion_threshold is applied as: DSR >= threshold and
    PBO <= 1 - threshold.
    """
    reasons: list[str] = []
    try:
        frozen.assert_unchanged(protocol)
    except ValueError:
        reasons.append("protocol changed after freeze")
    if not registry.protocol_frozen(experiment_id, frozen.sha256):
        reasons.append("protocol not frozen in registry")
    if not fixture_decision.accepted:
        reasons.append("fixture provenance not verified")
    if not prospective.eligible:
        reasons.extend(prospective.reasons or ("prospective sample insufficient",))
    threshold = frozen.protocol.promotion_threshold
    if pbo.status != "ok" or pbo.value is None or not math.isfinite(pbo.value):
        reasons.append("PBO inconclusive")
    elif pbo.value > 1.0 - threshold:
        reasons.append("PBO above frozen promotion threshold")
    if dsr.status != "ok" or dsr.value is None or not math.isfinite(dsr.value):
        reasons.append("DSR inconclusive")
    elif dsr.value < threshold:
        reasons.append("DSR below frozen promotion threshold")
    if not _is_digest(evidence.snapshot_sha256):
        reasons.append("snapshot artifact missing")
    if not _is_digest(evidence.walk_forward_sha256) or not evidence.walk_forward_plateau:
        reasons.append("walk-forward plateau not proven")
    if not _is_digest(evidence.stress_sha256) or not evidence.stress_passed:
        reasons.append("fee/slippage stress not passed")
    if tuple(sorted(evidence.trial_ids)) != registry.trial_ids(experiment_id):
        reasons.append("cumulative trial set does not match registry")
    return PromotionDecision(eligible=not reasons, reasons=tuple(reasons))
