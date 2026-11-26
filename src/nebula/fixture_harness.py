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
import json
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
# Pinned digest of the built-in fixture catalog. Adding or changing a
# catalog entry requires a reviewed code change that updates this pin;
# callers cannot authenticate their own catalog.
PINNED_CATALOG_SHA256 = (
    "5aed4488cc174f9481c28b20f0a0cb1a71522e375be54855c3c3afa8ddbbe5d7"
)


def synthetic_fixture_bytes(seed: int = 42, length: int = 64) -> bytes:
    """Deterministic synthetic fixture bytes from the pinned generator."""
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(
            f"nebula-fixture-gen:1.0.0:{seed}:{counter}".encode()
        ).digest()
        counter += 1
    return out[:length]


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


BUILTIN_CATALOG: Mapping[str, CatalogEntry] = MappingProxyType(
    {
        "synth-trend-v1": CatalogEntry(
            source_id="synth-trend-v1",
            kind="synthetic",
            sha256=hashlib.sha256(synthetic_fixture_bytes()).hexdigest(),
            generator="nebula-fixture-gen",
            generator_version="1.0.0",
            seed=42,
        )
    }
)


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
    if catalog_digest(BUILTIN_CATALOG) != PINNED_CATALOG_SHA256:
        reasons.append("built-in catalog does not match pinned trust anchor")
    if frozen.protocol.catalog_sha256 != PINNED_CATALOG_SHA256:
        reasons.append("fixture catalog does not match frozen trust anchor")
    if manifest.sha256 not in frozen.protocol.dataset_hashes.values():
        reasons.append("fixture hash not in frozen dataset hashes")
    if not manifest.sha256:
        reasons.append("manifest hash missing")
    elif hashlib.sha256(dataset).hexdigest() != manifest.sha256:
        reasons.append("dataset bytes do not match manifest hash")
    if manifest.kind not in FIXTURE_KINDS:
        if not deployment_gate_passed:
            reasons.append("non-fixture data rejected before deployment gate passes")
        else:
            reasons.append("fixture kind must be public or synthetic")
    entry = BUILTIN_CATALOG.get(manifest.source_id)
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
    preregistered_exclusions: Mapping = field(default_factory=dict)
    ema_step: int = 1
    atr_step: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "dataset_hashes", MappingProxyType(dict(self.dataset_hashes))
        )
        object.__setattr__(
            self,
            "preregistered_exclusions",
            MappingProxyType(dict(self.preregistered_exclusions)),
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
                "preregistered_exclusions": {str(k): v for k, v in sorted(self.preregistered_exclusions.items())},
                "ema_step": self.ema_step,
                "atr_step": self.atr_step,
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
    if (
        not math.isfinite(protocol.promotion_threshold)
        or not 0.0 < protocol.promotion_threshold < 1.0
    ):
        raise ValueError("promotion threshold must be finite and within (0, 1)")
    if not math.isfinite(protocol.fee_bps) or protocol.fee_bps < 0:
        raise ValueError("fee_bps must be finite and non-negative")
    if not math.isfinite(protocol.slippage_bps) or protocol.slippage_bps < 0:
        raise ValueError("slippage_bps must be finite and non-negative")
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


def _sharpe(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    if var <= 0:
        return 0.0
    return mean / math.sqrt(var)


def _derive_combinations(trials: Sequence[Sequence[float]]):
    """CSCV combinations derived deterministically from registry returns."""
    if not trials:
        return []
    length = len(trials[0])
    if length < 4 or length % 4 != 0:
        return []
    if any(len(trial) != length for trial in trials):
        return []
    if any(not math.isfinite(v) for trial in trials for v in trial):
        return []
    blocks = 4
    size = len(trials[0]) // blocks
    combos = []
    for a in range(blocks):
        for b in range(a + 1, blocks):
            is_idx = list(range(a * size, (a + 1) * size)) + list(
                range(b * size, (b + 1) * size)
            )
            oos_idx = [i for i in range(blocks * size) if i not in set(is_idx)]
            is_perf = [_sharpe([t[i] for i in is_idx]) for t in trials]
            oos_perf = [_sharpe([t[i] for i in oos_idx]) for t in trials]
            combos.append((is_perf, oos_perf))
    return combos


def evaluate_pbo_gate(
    frozen: FrozenProtocol,
    registry,
    experiment_id: str,
) -> StatResult:
    """PBO gate: combinations and exclusions derive from the registry."""
    if not frozen.verify():
        return StatResult.inconclusive("frozen protocol hash mismatch")
    if not registry.protocol_frozen(experiment_id, frozen.sha256):
        return StatResult.inconclusive("protocol not frozen in registry")
    if registry.post_performance_exclusions(experiment_id):
        return StatResult.inconclusive(
            "candidate excluded after performance was seen"
        )
    reasons = registry.exclusion_reasons(experiment_id)
    for index, reason_set in reasons.items():
        if len(reason_set) != 1 or frozen.protocol.preregistered_exclusions.get(index) not in reason_set:
            return StatResult.inconclusive(
                "candidate excluded without preregistration"
            )
    try:
        trials = [returns for _, returns in registry.trial_returns(experiment_id)]
    except Exception:
        return StatResult.inconclusive("registry trial returns unavailable")
    combinations = _derive_combinations(trials)
    if not combinations:
        return StatResult.inconclusive("CSCV artifact missing")
    trial_ids = [tid for tid, _ in registry.trial_returns(experiment_id)]
    excluded_indexes = {
        trial_ids.index(tid)
        for tid in frozen.protocol.preregistered_exclusions
        if tid in trial_ids
    }
    return cscv_pbo(combinations, preregistered_exclusions=excluded_indexes)


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


def build_dsr_artifacts(registry, experiment_id: str) -> DsrArtifacts:
    trials = [returns for _, returns in registry.trial_returns(experiment_id)]
    sample_count = len(trials[0]) if trials else 0
    return DsrArtifacts(
        trial_returns_sha256=_trials_digest(trials),
        correlation_sha256=_correlation_digest(trials),
        sample_count=sample_count,
        registry_trial_count=len(trials),
    )


def evaluate_dsr_gate(
    registry,
    experiment_id: str,
    selected_trial_id: str,
    artifacts: DsrArtifacts,
) -> StatResult:
    """DSR gate: returns are loaded from the registry by trial_id."""
    try:
        stored = registry.trial_returns(experiment_id)
    except Exception:
        return StatResult.inconclusive("registry trial returns unavailable")
    trials = [returns for _, returns in stored]
    selected_index = next(
        (i for i, (tid, _) in enumerate(stored) if tid == selected_trial_id), -1
    )
    if selected_index < 0:
        return StatResult.inconclusive("selected trial not in registry")
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

    snapshot_bytes: bytes
    walk_forward_bytes: bytes
    stress_bytes: bytes
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
    manifest: FixtureManifest,
    dataset_bytes: bytes,
    prospective_observations: int,
    prospective_duration_days: int,
    prospective_completeness: float,
    prospective_start: str,
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
    if not verify_fixture(manifest, dataset_bytes, frozen).accepted:
        reasons.append("fixture provenance not verified")
    prospective = evaluate_prospective(
        frozen,
        prospective_observations,
        prospective_duration_days,
        prospective_completeness,
        prospective_start,
    )
    if not prospective.eligible:
        reasons.extend(prospective.reasons or ("prospective sample insufficient",))
    threshold = frozen.protocol.promotion_threshold
    pbo_run = registry.latest_validation(experiment_id, "pbo")
    pbo_value = (pbo_run or {}).get("result", {}).get("value")
    if (
        not pbo_run
        or pbo_run["status"] != "ok"
        or pbo_run["artifacts"].get("protocol_sha256") != frozen.sha256
        or tuple(pbo_run["artifacts"].get("trial_ids", ())) != tuple(sorted(registry.trial_ids(experiment_id)))
        or not isinstance(pbo_value, (int, float))
        or not math.isfinite(pbo_value)
    ):
        reasons.append("PBO validation missing or inconclusive")
    elif pbo_value > 1.0 - threshold:
        reasons.append("PBO above frozen promotion threshold")
    else:
        recomputed = evaluate_pbo_gate(frozen, registry, experiment_id)
        if recomputed.status != "ok" or recomputed.value is None or abs(recomputed.value - pbo_value) > 1e-9:
            reasons.append("PBO validation does not match recomputation")
    dsr_run = registry.latest_validation(experiment_id, "dsr")
    dsr_value = (dsr_run or {}).get("result", {}).get("value")
    if (
        not dsr_run
        or dsr_run["status"] != "ok"
        or dsr_run["artifacts"].get("protocol_sha256") != frozen.sha256
        or tuple(dsr_run["artifacts"].get("trial_ids", ())) != tuple(sorted(registry.trial_ids(experiment_id)))
        or not isinstance(dsr_value, (int, float))
        or not math.isfinite(dsr_value)
    ):
        reasons.append("DSR validation missing or inconclusive")
    elif dsr_value < threshold:
        reasons.append("DSR below frozen promotion threshold")
    else:
        recomputed = evaluate_dsr_gate(
            registry,
            experiment_id,
            _best_trial_id(registry, experiment_id),
            build_dsr_artifacts(registry, experiment_id),
        )
        if recomputed.status != "ok" or recomputed.value is None or abs(recomputed.value - dsr_value) > 1e-9:
            reasons.append("DSR validation does not match recomputation")
    for method, artifact_bytes, label in (
        ("snapshot", evidence.snapshot_bytes, "snapshot"),
        ("walk_forward", evidence.walk_forward_bytes, "walk-forward plateau"),
        ("stress", evidence.stress_bytes, "fee/slippage stress"),
    ):
        expected_hash = hashlib.sha256(artifact_bytes).hexdigest()
        run = registry.latest_validation(experiment_id, method)
        artifacts = (run or {}).get("artifacts", {})
        if (
            not run
            or run["status"] != "ok"
            or artifacts.get("artifact_sha256") != expected_hash
            or artifacts.get("passed") is not True
            or not _artifact_passed(
                registry, experiment_id, method, artifact_bytes, frozen
            )
        ):
            reasons.append(f"{label} artifact not verified in registry")
    if tuple(sorted(evidence.trial_ids)) != registry.trial_ids(experiment_id):
        reasons.append("cumulative trial set does not match registry")
    return PromotionDecision(eligible=not reasons, reasons=tuple(reasons))

def _stat_artifacts(registry, experiment_id: str, frozen: "FrozenProtocol") -> dict:
    """Computed statistical artifacts bound to the registry returns."""
    stored = registry.trial_returns(experiment_id)
    trials = [list(returns) for _, returns in stored]
    best = max(trials, key=_sharpe)
    mean = sum(best) / len(best)
    var = sum((v - mean) ** 2 for v in best) / len(best)
    std = math.sqrt(var) if var > 0 else 0.0
    skewness = (
        sum(((v - mean) / std) ** 3 for v in best) / len(best) if std else 0.0
    )
    kurtosis = (
        sum(((v - mean) / std) ** 4 for v in best) / len(best) if std else 3.0
    )
    combinations = _derive_combinations([tuple(t) for t in trials])
    return {
        "return_series_artifact_sha256": _trials_digest(trials),
        "split_manifest_sha256": sha256_hex(
            canonical_json({"combinations": [[list(a), list(b)] for a, b in combinations]})
        ),
        "benchmark_id": "registry-returns",
        "sample_count": len(best),
        "return_skewness": skewness,
        "return_kurtosis": kurtosis,
        "observed_sharpe": _sharpe(best),
        "effective_trial_count": float(len(trials)),
        "trial_correlation_artifact_sha256": _correlation_digest(trials),
        "selection_rule": "max-sharpe",
        "researcher_decision_log_sha256": sha256_hex(
            canonical_json({"protocol_sha256": frozen.sha256})
        ),
        "protocol_sha256": frozen.sha256,
        "trial_ids": sorted(registry.trial_ids(experiment_id)),
    }


def run_pbo_and_record(registry, experiment_id: str, frozen) -> StatResult:
    """Record performance_seen, evaluate PBO, and record the result."""
    for trial_id in registry.trial_ids(experiment_id):
        registry.record_performance_seen(experiment_id, trial_id, "fixture-harness")
    result = evaluate_pbo_gate(frozen, registry, experiment_id)
    registry.record_validation(
        f"pbo-{experiment_id}",
        experiment_id,
        "pbo",
        _stat_artifacts(registry, experiment_id, frozen),
        {"value": result.value, "status": result.status},
    )
    return result


def run_dsr_and_record(registry, experiment_id: str, frozen) -> StatResult:
    """Evaluate DSR for the best-Sharpe registry trial and record it."""
    artifacts = build_dsr_artifacts(registry, experiment_id)
    result = evaluate_dsr_gate(
        registry, experiment_id, _best_trial_id(registry, experiment_id), artifacts
    )
    registry.record_validation(
        f"dsr-{experiment_id}",
        experiment_id,
        "dsr",
        _stat_artifacts(registry, experiment_id, frozen),
        {"value": result.value, "status": result.status},
    )
    return result


def _best_trial_id(registry, experiment_id: str) -> str:
    stored = registry.trial_returns(experiment_id)
    return max(stored, key=lambda item: _sharpe(list(item[1])))[0]


def _plateau_stable(registry, experiment_id: str, candidate_id, frozen) -> bool:
    """Plateau across frozen-grid neighbors in the same completed trial group."""
    params = registry.trial_params(experiment_id, frozen.protocol.trial_group)
    stored = dict(registry.trial_returns(experiment_id))
    if candidate_id not in params or candidate_id not in stored:
        return False
    candidate = params[candidate_id]
    if not isinstance(candidate, dict) or set(candidate) != {"ema", "atr"}:
        return False
    if frozen.protocol.ema_step <= 0 or frozen.protocol.atr_step <= 0:
        return False
    neighbors: dict[tuple, list[str]] = {}
    for trial_id, p in params.items():
        if trial_id == candidate_id or not isinstance(p, dict) or set(p) != {"ema", "atr"}:
            continue
        ema_step = (
            abs(p["ema"] - candidate["ema"]) == frozen.protocol.ema_step
            and p["atr"] == candidate["atr"]
        )
        atr_step = (
            abs(p["atr"] - candidate["atr"]) == frozen.protocol.atr_step
            and p["ema"] == candidate["ema"]
        )
        if ema_step or atr_step:
            if trial_id not in stored:
                return False
            neighbors.setdefault((p["ema"], p["atr"]), []).append(trial_id)
    if len(neighbors) < 2:
        return False
    candidate_sharpe = _sharpe(list(stored[candidate_id]))
    if candidate_sharpe <= 0:
        return False
    neighbor_sharpes = [
        min(_sharpe(list(stored[tid])) for tid in trial_ids)
        for trial_ids in neighbors.values()
    ]
    if any(s <= 0 for s in neighbor_sharpes):
        return False
    return sum(neighbor_sharpes) / len(neighbor_sharpes) >= 0.5 * candidate_sharpe


def _artifact_passed(
    registry, experiment_id: str, method: str, artifact_bytes: bytes, frozen
) -> bool:
    """Compute artifact validity from its data, never from summary claims."""
    try:
        payload = json.loads(artifact_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if method == "snapshot":
        trials = [list(r) for _, r in registry.trial_returns(experiment_id)]
        expected = hashlib.sha256(
            json.dumps(trials, separators=(",", ":")).encode()
        ).hexdigest()
        return (
            payload.get("schema") == "snapshot-v1"
            and payload.get("trial_ids") == sorted(registry.trial_ids(experiment_id))
            and payload.get("protocol_sha256") == frozen.sha256
            and payload.get("returns_sha256") == expected
            and payload.get("experiment_id") == experiment_id
            and payload.get("dataset_sha256") in frozen.protocol.dataset_hashes.values()
        )
    if method == "walk_forward":
        folds = payload.get("fold_returns")
        if payload.get("schema") != "walk-forward-v1" or not isinstance(folds, list) or len(folds) < 2:
            return False
        if payload.get("protocol_sha256") != frozen.sha256:
            return False
        stored = dict(registry.trial_returns(experiment_id))
        returns = stored.get(payload.get("trial_id"))
        if returns is None:
            return False
        expected_folds = [list(returns[i : i + 2]) for i in range(0, len(returns), 2)]
        if folds != expected_folds:
            return False
        sharpes = []
        for fold in folds:
            if not isinstance(fold, list) or len(fold) < 2:
                return False
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in fold):
                return False
            sharpes.append(_sharpe([float(v) for v in fold]))
        if min(sharpes) < 0 or sum(sharpes) / len(sharpes) <= 0:
            return False
        return _plateau_stable(registry, experiment_id, payload.get("trial_id"), frozen)
    if method == "stress":
        gross = payload.get("gross_returns")
        if payload.get("schema") != "stress-v1" or not isinstance(gross, list) or len(gross) < 2:
            return False
        if payload.get("protocol_sha256") != frozen.sha256:
            return False
        stored = dict(registry.trial_returns(experiment_id))
        if list(stored.get(payload.get("trial_id"), ())) != [float(v) for v in gross if isinstance(v, (int, float))]:
            return False
        if payload.get("fee_bps") != frozen.protocol.fee_bps:
            return False
        if payload.get("slippage_bps") != frozen.protocol.slippage_bps:
            return False
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in gross):
            return False
        cost = (frozen.protocol.fee_bps + frozen.protocol.slippage_bps) / 10000.0
        net = [float(v) - cost for v in gross]
        return _sharpe(net) > 0
    return False


def record_artifact_validation(
    registry, experiment_id: str, method: str, artifact_bytes: bytes, frozen
) -> None:
    """Record an artifact validation computed from the artifact bytes."""
    registry.record_validation(
        f"{method}-{experiment_id}-{hashlib.sha256(artifact_bytes).hexdigest()[:8]}",
        experiment_id,
        method,
        {
            "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "passed": _artifact_passed(
                registry, experiment_id, method, artifact_bytes, frozen
            ),
        },
        {},
    )
