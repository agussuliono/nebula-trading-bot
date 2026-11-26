"""Fase 1 offline fixture harness (spec v1.1).

Research-only module. It verifies fixture provenance against a trusted
catalog, freezes experiment protocols, and gates promotion decisions on
prospective-sample sufficiency plus PBO/DSR validity. It never reads
trading state, never touches the network, and has no path to order
placement. Demo data is rejected until the Deployment Security Gate
passes; verification failure never falls back to acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from nebula.research_registry import StatResult, canonical_json, cscv_pbo, deflated_sharpe_ratio, sha256_hex

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
    catalog: Mapping[str, CatalogEntry],
    *,
    deployment_gate_passed: bool = False,
) -> FixtureDecision:
    """Accept a fixture only when its provenance verifies against the catalog.

    The manifest hash proves integrity, not origin: the source must exist
    in the trusted catalog, the declared kind must match the catalog kind,
    and the hash must match the catalog hash. Synthetic fixtures also
    require verifiable generator metadata in the catalog entry.
    """
    reasons: list[str] = []
    if not manifest.sha256:
        reasons.append("manifest hash missing")
    if manifest.kind not in FIXTURE_KINDS:
        if not deployment_gate_passed:
            reasons.append("non-fixture data rejected before deployment gate passes")
        else:
            reasons.append("fixture kind must be public or synthetic")
    entry = catalog.get(manifest.source_id)
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
    prospective_cutoff: str
    fee_bps: float
    slippage_bps: float
    promotion_threshold: float
    prospective_min_observations: int
    prospective_min_duration_days: int
    prospective_min_completeness: float
    preregistered_exclusions: frozenset = frozenset()

    def canonical(self) -> str:
        return canonical_json(
            {
                "trial_group": self.trial_group,
                "hypothesis_family": self.hypothesis_family,
                "dataset_hashes": dict(self.dataset_hashes),
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
    return FrozenProtocol(protocol=protocol, sha256=sha256_hex(protocol.canonical()))


@dataclass(frozen=True)
class ProspectiveDecision:
    eligible: bool
    reasons: tuple[str, ...] = ()


def evaluate_prospective(
    frozen: FrozenProtocol,
    observations: int,
    duration_days: int,
    completeness: float,
) -> ProspectiveDecision:
    protocol = frozen.protocol
    reasons: list[str] = []
    if observations < protocol.prospective_min_observations:
        reasons.append("prospective observations below frozen minimum")
    if duration_days < protocol.prospective_min_duration_days:
        reasons.append("prospective duration below frozen minimum")
    if completeness < protocol.prospective_min_completeness:
        reasons.append("prospective completeness below frozen minimum")
    return ProspectiveDecision(eligible=not reasons, reasons=tuple(reasons))


def evaluate_pbo_gate(
    combinations: Optional[Sequence[tuple[Sequence[Optional[float]], Sequence[Optional[float]]]]],
    *,
    preregistered_exclusions: frozenset | set | tuple = (),
    post_performance_exclusions: frozenset | set | tuple = (),
) -> StatResult:
    """PBO gate: post-performance exclusions and missing artifacts fail closed."""
    if post_performance_exclusions:
        return StatResult.inconclusive(
            "candidate excluded after performance was seen"
        )
    if not combinations:
        return StatResult.inconclusive("CSCV artifact missing")
    return cscv_pbo(combinations, preregistered_exclusions=set(preregistered_exclusions))


def evaluate_dsr_gate(
    trials: Sequence[Sequence[float]],
    selected_index: int,
    *,
    artifacts_present: bool,
) -> StatResult:
    """DSR gate: missing statistical artifacts fail closed."""
    if not artifacts_present:
        return StatResult.inconclusive("DSR artifact missing")
    return deflated_sharpe_ratio(trials, selected_index)


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def evaluate_promotion(
    *,
    frozen: FrozenProtocol,
    fixture_decision: FixtureDecision,
    prospective: ProspectiveDecision,
    pbo: StatResult,
    dsr: StatResult,
    snapshot_complete: bool,
    all_trials_recorded: bool,
) -> PromotionDecision:
    """Promotion to Fase 2 design requires every gate to pass."""
    reasons: list[str] = []
    if not fixture_decision.accepted:
        reasons.append("fixture provenance not verified")
    if not prospective.eligible:
        reasons.extend(prospective.reasons or ("prospective sample insufficient",))
    if pbo.status != "ok":
        reasons.append("PBO inconclusive")
    if dsr.status != "ok":
        reasons.append("DSR inconclusive")
    if not snapshot_complete:
        reasons.append("context snapshot incomplete")
    if not all_trials_recorded:
        reasons.append("not all trials recorded")
    return PromotionDecision(eligible=not reasons, reasons=tuple(reasons))
