"""Phase 0B research registry: importer, append-only evidence store, statistics.

Research-only module. It never reads trading state, never touches the
network, and has no path to order placement. It implements the Fase 0B
specification (v2 section 8-9, statistics frozen by v2.2.1-v2.2.3):

- single-writer importer over ``research.db`` (``BEGIN IMMEDIATE``),
  idempotent event import, ID/payload conflicts recorded as rejections;
- append-only evidence tables enforced by SQLite triggers;
- hash chain verifiable from genesis plus external checkpoints;
- experiment registry counting every trial (failed/aborted/invalid too);
- locked-holdout access log with irreversible contamination;
- CSCV/PBO and Deflated Sharpe Ratio with the frozen estimators.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Optional, Sequence

GENESIS_HASH = "0" * 64
EULER_GAMMA = 0.5772156649015329
EULER_E = 2.718281828459045
EIGENVALUE_TOLERANCE = -1e-10

_NORMAL = NormalDist()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Statistics (frozen estimators, spec v2.2.1-v2.2.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatResult:
    status: str  # "ok" or "inconclusive"
    value: Optional[float]
    reason: str = ""

    @classmethod
    def ok(cls, value: float) -> "StatResult":
        return cls("ok", float(value), "")

    @classmethod
    def inconclusive(cls, reason: str) -> "StatResult":
        return cls("inconclusive", None, reason)


def sharpe_ratio(returns: Sequence[float]) -> StatResult:
    """Sharpe ratio with sample standard deviation (ddof=1)."""
    n = len(returns)
    if n < 2:
        return StatResult.inconclusive("sharpe needs at least 2 returns")
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        return StatResult.inconclusive("zero-variance return series")
    return StatResult.ok(mean / math.sqrt(var))


def skewness_kurtosis(returns: Sequence[float]) -> Optional[tuple[float, float]]:
    """Population-moment skewness g3 and Pearson kurtosis g4 (normal = 3)."""
    n = len(returns)
    if n == 0:
        return None
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    if m2 <= 0:
        return None
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    return m3 / m2**1.5, m4 / m2**2


def pbo_from_ranks(ranks: Sequence[float], n_candidates: int) -> StatResult:
    """PBO from out-of-sample ranks of the in-sample winner.

    Rank 1 is the worst out-of-sample performance, rank N the best.
    omega = rank / (N + 1); lambda = ln(omega / (1 - omega));
    PBO is the fraction of combinations with lambda < 0.
    """
    if n_candidates < 2:
        return StatResult.inconclusive("PBO needs at least 2 candidates")
    if not ranks:
        return StatResult.inconclusive("PBO needs at least 1 combination")
    lambdas = []
    for rank in ranks:
        if rank < 1 or rank > n_candidates:
            return StatResult.inconclusive("rank outside 1..N")
        omega = rank / (n_candidates + 1)
        lambdas.append(math.log(omega / (1.0 - omega)))
    return StatResult.ok(sum(1 for lam in lambdas if lam < 0) / len(lambdas))


def cscv_pbo(
    combinations: Sequence[tuple[Sequence[Optional[float]], Sequence[Optional[float]]]],
    preregistered_exclusions: Iterable[int] = (),
) -> StatResult:
    """CSCV PBO from (in-sample, out-of-sample) score lists per combination.

    A candidate is valid in a combination when both scores are present.
    Fewer than 2 valid candidates, or a missing score for a candidate that
    was not excluded before performance was seen, makes PBO inconclusive.
    """
    excluded = set(preregistered_exclusions)
    ranks: list[float] = []
    n_candidates: Optional[int] = None
    for is_scores, oos_scores in combinations:
        if len(is_scores) != len(oos_scores):
            return StatResult.inconclusive("IS/OOS candidate count mismatch")
        if n_candidates is None:
            n_candidates = len(is_scores)
        elif len(is_scores) != n_candidates:
            return StatResult.inconclusive("candidate count changes across combinations")
        valid = []
        for idx, (is_score, oos_score) in enumerate(zip(is_scores, oos_scores)):
            if idx in excluded:
                continue
            if is_score is None or oos_score is None:
                return StatResult.inconclusive(
                    "missing return after selection without preregistered exclusion"
                )
            valid.append(idx)
        if len(valid) < 2:
            return StatResult.inconclusive("fewer than 2 valid candidates in a combination")
        winner = max(valid, key=lambda i: is_scores[i])  # type: ignore[arg-type]
        winner_oos = oos_scores[winner]
        worse = sum(1 for i in valid if oos_scores[i] < winner_oos)  # type: ignore[operator]
        tied = sum(1 for i in valid if i != winner and oos_scores[i] == winner_oos)
        ranks.append(1 + worse + tied / 2.0)
    if n_candidates is None:
        return StatResult.inconclusive("no combinations")
    effective_n = n_candidates - len(excluded & set(range(n_candidates)))
    return pbo_from_ranks(ranks, effective_n)


def pearson_correlation(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Pearson correlation; None when undefined (length < 2 or zero variance)."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _jacobi_eigenvalues(matrix: Sequence[Sequence[float]]) -> list[float]:
    """Eigenvalues of a symmetric matrix via Jacobi rotations."""
    n = len(matrix)
    a = [list(row) for row in matrix]
    for _ in range(100):
        off = math.sqrt(sum(a[i][j] ** 2 for i in range(n) for j in range(n) if i != j))
        if off < 1e-12:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(a[p][q]) < 1e-15:
                    continue
                theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
                t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(n):
                    akp = a[k][p]
                    akq = a[k][q]
                    a[k][p] = c * akp - s * akq
                    a[k][q] = s * akp + c * akq
                for k in range(n):
                    apk = a[p][k]
                    aqk = a[q][k]
                    a[p][k] = c * apk - s * aqk
                    a[q][k] = s * apk + c * aqk
    return sorted((a[i][i] for i in range(n)), reverse=True)


def effective_trial_count(correlation: Sequence[Sequence[Optional[float]]]) -> StatResult:
    """Participation-ratio effective trial count from a correlation matrix."""
    n = len(correlation)
    if n == 0:
        return StatResult.inconclusive("empty correlation matrix")
    matrix: list[list[float]] = []
    for row in correlation:
        if len(row) != n:
            return StatResult.inconclusive("correlation matrix is not square")
        converted = []
        for value in row:
            if value is None:
                return StatResult.inconclusive("incomplete trial correlation")
            converted.append(float(value))
        matrix.append(converted)
    eigenvalues = _jacobi_eigenvalues(matrix)
    adjusted = []
    for value in eigenvalues:
        if value < EIGENVALUE_TOLERANCE:
            return StatResult.inconclusive("correlation matrix is not positive semidefinite")
        adjusted.append(0.0 if value < 0 else value)
    total = sum(adjusted)
    squares = sum(v * v for v in adjusted)
    if total <= 0 or squares <= 0:
        return StatResult.inconclusive("degenerate correlation eigenvalues")
    return StatResult.ok(total * total / squares)


def deflated_sharpe_ratio(
    trial_returns: Sequence[Sequence[float]],
    selected_index: int,
) -> StatResult:
    """Deflated Sharpe Ratio for the selected trial (frozen estimator).

    Any zero-variance trial in the DSR trial set, or an incomplete trial
    correlation, makes the result inconclusive. A single trial uses
    SR0 = 0 and still requires T >= 3 and a positive denominator.
    """
    if not trial_returns or not (0 <= selected_index < len(trial_returns)):
        return StatResult.inconclusive("selected trial out of range")
    sharpes: list[float] = []
    for returns in trial_returns:
        result = sharpe_ratio(returns)
        if result.status != "ok":
            return StatResult.inconclusive(f"trial Sharpe undefined: {result.reason}")
        sharpes.append(result.value)  # type: ignore[arg-type]
    selected = trial_returns[selected_index]
    moments = skewness_kurtosis(selected)
    if moments is None:
        return StatResult.inconclusive("selected trial moments undefined")
    g3, g4 = moments
    sr_hat = sharpes[selected_index]
    t_obs = len(selected)
    if t_obs < 3:
        return StatResult.inconclusive("DSR needs at least 3 returns")
    denominator_sq = 1.0 - g3 * sr_hat + ((g4 - 1.0) / 4.0) * sr_hat**2
    if denominator_sq <= 0:
        return StatResult.inconclusive("non-positive DSR denominator")
    if len(trial_returns) == 1:
        sr0 = 0.0
    else:
        corr = [
            [pearson_correlation(trial_returns[i], trial_returns[j]) for j in range(len(trial_returns))]
            for i in range(len(trial_returns))
        ]
        n_eff_result = effective_trial_count(corr)
        if n_eff_result.status != "ok":
            return n_eff_result
        n_eff = n_eff_result.value  # type: ignore[assignment]
        mean_sr = sum(sharpes) / len(sharpes)
        var_sr = sum((s - mean_sr) ** 2 for s in sharpes) / (len(sharpes) - 1)
        if var_sr <= 0 or n_eff <= 1.0:
            sr0 = 0.0
        else:
            sr0 = math.sqrt(var_sr) * (
                (1.0 - EULER_GAMMA) * _NORMAL.inv_cdf(1.0 - 1.0 / n_eff)
                + EULER_GAMMA * _NORMAL.inv_cdf(1.0 - 1.0 / (n_eff * EULER_E))
            )
    z = (sr_hat - sr0) * math.sqrt(t_obs - 1) / math.sqrt(denominator_sq)
    return StatResult.ok(_NORMAL.cdf(z))


# ---------------------------------------------------------------------------
# Registry store
# ---------------------------------------------------------------------------

EVIDENCE_TABLES = (
    "research_events",
    "import_rejections",
    "experiments",
    "experiment_trials",
    "experiment_status_events",
    "validation_runs",
    "holdout_access_events",
    "research_checkpoints",
    "import_files",
    "cycle_completeness",
)

STAT_ARTIFACT_FIELDS = (
    "return_series_artifact_sha256",
    "split_manifest_sha256",
    "benchmark_id",
    "sample_count",
    "return_skewness",
    "return_kurtosis",
    "observed_sharpe",
    "effective_trial_count",
    "trial_correlation_artifact_sha256",
    "selection_rule",
    "researcher_decision_log_sha256",
)

TRIAL_STATUSES = ("completed", "failed", "aborted", "invalid")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_events (
    event_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    reason TEXT NOT NULL,
    payload_sha256 TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    hypothesis TEXT NOT NULL,
    protocol_sha256 TEXT NOT NULL,
    dataset_manifest_sha256 TEXT NOT NULL,
    code_commit TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiment_trials (
    trial_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    trial_group TEXT NOT NULL,
    status TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiment_status_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    old_status TEXT,
    new_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_runs (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    method TEXT NOT NULL,
    status TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdout_access_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    access TEXT NOT NULL,
    protocol_sha256 TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    chain_head TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    signature_reference TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_files (
    file_id TEXT PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycle_completeness (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_attempt TEXT NOT NULL,
    expected_count INTEGER NOT NULL,
    received_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class RegistryError(RuntimeError):
    pass


class ResearchRegistry:
    """Append-only research evidence store backed by SQLite."""

    SCHEMA_VERSION = "0b-1"

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA recursive_triggers = ON")
        self._create_schema()

    def close(self) -> None:
        self._conn.close()

    _PRIMARY_KEYS = {
        "research_events": "event_id",
        "import_rejections": "id",
        "experiments": "experiment_id",
        "experiment_trials": "trial_id",
        "experiment_status_events": "id",
        "validation_runs": "run_id",
        "holdout_access_events": "id",
        "research_checkpoints": "checkpoint_id",
        "import_files": "file_id",
        "cycle_completeness": "id",
    }

    def _create_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._migrate()
        for table in EVIDENCE_TABLES:
            self._conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_update "
                f"BEFORE UPDATE ON {table} BEGIN "
                f"SELECT RAISE(ABORT, 'evidence table is append-only'); END"
            )
            self._conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete "
                f"BEFORE DELETE ON {table} BEGIN "
                f"SELECT RAISE(ABORT, 'evidence table is append-only'); END"
            )
            pk = self._PRIMARY_KEYS[table]
            self._conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace "
                f"BEFORE INSERT ON {table} "
                f"WHEN NEW.{pk} IS NOT NULL AND EXISTS "
                f"(SELECT 1 FROM {table} WHERE {pk} = NEW.{pk}) "
                f"BEGIN SELECT RAISE(ABORT, 'evidence table is append-only'); END"
            )

    def _migrate(self) -> None:
        trial_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(experiment_trials)")
        }
        if "origin" not in trial_cols:
            self._conn.execute(
                "ALTER TABLE experiment_trials ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'"
            )
        checkpoint_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(research_checkpoints)")
        }
        if "evidence_digest" not in checkpoint_cols:
            self._conn.execute(
                "ALTER TABLE research_checkpoints ADD COLUMN evidence_digest TEXT NOT NULL DEFAULT ''"
            )
        if "signature" not in checkpoint_cols:
            self._conn.execute(
                "ALTER TABLE research_checkpoints ADD COLUMN signature TEXT NOT NULL DEFAULT ''"
            )
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (self.SCHEMA_VERSION,),
        )

    # -- importer ---------------------------------------------------------

    def _chain_head(self) -> tuple[str, int]:
        row = self._conn.execute(
            "SELECT event_hash FROM research_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        count = self._conn.execute("SELECT COUNT(*) FROM research_events").fetchone()[0]
        return (row[0] if row else GENESIS_HASH), int(count)

    def import_events(self, events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
        """Import events idempotently; conflicts become rejections."""
        imported = duplicates = rejected = 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            prev_hash, _ = self._chain_head()
            for event in events:
                event_id = event.get("event_id")
                if not isinstance(event_id, str) or not event_id:
                    self._record_rejection(None, "missing event_id", None)
                    rejected += 1
                    continue
                payload = canonical_json(event)
                payload_hash = sha256_hex(payload)
                existing = self._conn.execute(
                    "SELECT payload_sha256 FROM research_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing is not None:
                    if existing[0] == payload_hash:
                        duplicates += 1
                    else:
                        self._record_rejection(event_id, "event_id conflict", payload_hash)
                        rejected += 1
                    continue
                event_hash = sha256_hex(prev_hash + payload_hash)
                self._conn.execute(
                    "INSERT INTO research_events "
                    "(event_id, payload_json, payload_sha256, prev_hash, event_hash, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (event_id, payload, payload_hash, prev_hash, event_hash, _utc_now()),
                )
                prev_hash = event_hash
                imported += 1
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return {"imported": imported, "duplicates": duplicates, "rejected": rejected}

    def _record_rejection(
        self, event_id: Optional[str], reason: str, payload_hash: Optional[str]
    ) -> None:
        self._conn.execute(
            "INSERT INTO import_rejections (event_id, reason, payload_sha256, created_at) "
            "VALUES (?, ?, ?, ?)",
            (event_id, reason, payload_hash, _utc_now()),
        )

    def verify_chain(self) -> bool:
        prev_hash = GENESIS_HASH
        rows = self._conn.execute(
            "SELECT payload_json, payload_sha256, prev_hash, event_hash "
            "FROM research_events ORDER BY rowid"
        )
        for payload, payload_hash, stored_prev, event_hash in rows:
            if stored_prev != prev_hash:
                return False
            if sha256_hex(payload) != payload_hash:
                return False
            if sha256_hex(prev_hash + payload_hash) != event_hash:
                return False
            prev_hash = event_hash
        return True

    # -- checkpoints ------------------------------------------------------

    def _evidence_digest(self) -> str:
        """Digest over every evidence table so checkpoints cover the registry."""
        digest = hashlib.sha256()
        for table in EVIDENCE_TABLES:
            if table == "research_checkpoints":
                continue
            digest.update(table.encode("utf-8"))
            for row in self._conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                digest.update(canonical_json({"row": [str(v) for v in row]}).encode("utf-8"))
        return digest.hexdigest()

    def create_checkpoint(
        self, signature_reference: str, signature: str = ""
    ) -> dict[str, Any]:
        head, count = self._chain_head()
        evidence_digest = self._evidence_digest()
        checkpoint_id = sha256_hex(f"{head}:{count}:{evidence_digest}:{_utc_now()}")
        self._conn.execute(
            "INSERT INTO research_checkpoints "
            "(checkpoint_id, chain_head, event_count, schema_version, signature_reference, "
            "evidence_digest, signature, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                checkpoint_id,
                head,
                count,
                self.SCHEMA_VERSION,
                signature_reference,
                evidence_digest,
                signature,
                _utc_now(),
            ),
        )
        return {
            "checkpoint_id": checkpoint_id,
            "chain_head": head,
            "event_count": count,
            "schema_version": self.SCHEMA_VERSION,
            "signature_reference": signature_reference,
            "evidence_digest": evidence_digest,
            "signature": signature,
        }

    def verify_checkpoint(
        self,
        checkpoint: Mapping[str, Any],
        signature_verifier: Optional[Any] = None,
    ) -> bool:
        stored = self._conn.execute(
            "SELECT chain_head, event_count, schema_version, signature_reference, "
            "evidence_digest, signature FROM research_checkpoints WHERE checkpoint_id = ?",
            (checkpoint.get("checkpoint_id"),),
        ).fetchone()
        if stored is None:
            return False
        expected = {
            "chain_head": stored[0],
            "event_count": stored[1],
            "schema_version": stored[2],
            "signature_reference": stored[3],
            "evidence_digest": stored[4],
            "signature": stored[5],
        }
        if any(checkpoint.get(key) != value for key, value in expected.items()):
            return False
        head, count = self._chain_head()
        if head != expected["chain_head"] or count != expected["event_count"]:
            return False
        if self._evidence_digest() != expected["evidence_digest"]:
            return False
        if expected["signature"]:
            if signature_verifier is None:
                return False
            if not signature_verifier(checkpoint, expected["signature"]):
                return False
        return self.verify_chain()

    # -- experiments and trials ------------------------------------------

    def register_experiment(
        self,
        experiment_id: str,
        hypothesis: str,
        protocol_sha256: str,
        dataset_manifest_sha256: str,
        code_commit: str,
        config_hash: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO experiments "
            "(experiment_id, hypothesis, protocol_sha256, dataset_manifest_sha256, "
            "code_commit, config_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                experiment_id,
                hypothesis,
                protocol_sha256,
                dataset_manifest_sha256,
                code_commit,
                config_hash,
                _utc_now(),
            ),
        )

    TRIAL_ORIGINS = ("manual", "ai", "automated")

    def record_trial(
        self,
        trial_id: str,
        experiment_id: str,
        trial_group: str,
        status: str,
        parameters: Mapping[str, Any],
        metrics: Mapping[str, Any],
        origin: str = "manual",
    ) -> None:
        if status not in TRIAL_STATUSES:
            raise RegistryError(f"unknown trial status: {status}")
        if origin not in self.TRIAL_ORIGINS:
            raise RegistryError(f"unknown trial origin: {origin}")
        self._conn.execute(
            "INSERT INTO experiment_trials "
            "(trial_id, experiment_id, trial_group, status, parameters_json, metrics_json, "
            "origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trial_id,
                experiment_id,
                trial_group,
                status,
                canonical_json(parameters),
                canonical_json(metrics),
                origin,
                _utc_now(),
            ),
        )

    def trial_count(self, experiment_id: str) -> int:
        """Every recorded trial counts, including failed/aborted/invalid."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM experiment_trials WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        return int(row[0])

    def record_experiment_status(
        self, experiment_id: str, new_status: str, reason: str, actor: str
    ) -> None:
        row = self._conn.execute(
            "SELECT new_status FROM experiment_status_events "
            "WHERE experiment_id = ? ORDER BY id DESC LIMIT 1",
            (experiment_id,),
        ).fetchone()
        self._conn.execute(
            "INSERT INTO experiment_status_events "
            "(experiment_id, old_status, new_status, reason, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (experiment_id, row[0] if row else None, new_status, reason, actor, _utc_now()),
        )

    # -- validation --------------------------------------------------------

    def record_validation(
        self,
        run_id: str,
        experiment_id: str,
        method: str,
        artifacts: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> str:
        """Store a validation run; PBO/DSR claims without artifacts are inconclusive."""
        status = "ok"
        if method in ("pbo", "dsr"):
            missing = [f for f in STAT_ARTIFACT_FIELDS if artifacts.get(f) in (None, "")]
            hash_fields = [f for f in STAT_ARTIFACT_FIELDS if f.endswith("_sha256")]
            bad_hash = [
                f
                for f in hash_fields
                if not (
                    isinstance(artifacts.get(f), str)
                    and len(artifacts[f]) == 64
                    and all(c in "0123456789abcdef" for c in artifacts[f])
                )
            ]
            sample_count = artifacts.get("sample_count")
            bad_count = not (isinstance(sample_count, int) and sample_count > 0)
            if missing or bad_hash or bad_count:
                status = "inconclusive"
        self._conn.execute(
            "INSERT INTO validation_runs "
            "(run_id, experiment_id, method, status, artifacts_json, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                experiment_id,
                method,
                status,
                canonical_json(artifacts),
                canonical_json(result),
                _utc_now(),
            ),
        )
        return status

    # -- holdout -----------------------------------------------------------

    def request_holdout_access(
        self,
        dataset_id: str,
        experiment_id: str,
        protocol_sha256: str,
        protocol_frozen: bool,
    ) -> bool:
        """Access gate: holdout reads require a frozen protocol."""
        granted = bool(protocol_frozen)
        self.record_holdout_access(dataset_id, experiment_id, granted, protocol_sha256)
        return granted

    def holdout_read_allowed(
        self, dataset_id: str, experiment_id: str, protocol_sha256: str
    ) -> bool:
        row = self._conn.execute(
            "SELECT result FROM holdout_access_events WHERE dataset_id = ? "
            "AND experiment_id = ? AND protocol_sha256 = ? ORDER BY id DESC LIMIT 1",
            (dataset_id, experiment_id, protocol_sha256),
        ).fetchone()
        return row is not None and row[0] in ("granted", "contaminated")

    def record_holdout_access(
        self,
        dataset_id: str,
        experiment_id: str,
        granted: bool,
        protocol_sha256: str,
        used_for_selection: bool = False,
    ) -> str:
        result = "contaminated" if used_for_selection else ("granted" if granted else "denied")
        self._conn.execute(
            "INSERT INTO holdout_access_events "
            "(dataset_id, experiment_id, access, protocol_sha256, result, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                dataset_id,
                experiment_id,
                "granted" if granted else "denied",
                protocol_sha256,
                result,
                _utc_now(),
            ),
        )
        return self.holdout_status(dataset_id)

    def holdout_status(self, dataset_id: str) -> str:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM holdout_access_events "
            "WHERE dataset_id = ? AND result = 'contaminated'",
            (dataset_id,),
        ).fetchone()
        return "contaminated" if row[0] else "untouched"
