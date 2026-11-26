# Research Shadow Runbook (Fase 0D)

## Purpose and scope

This runbook operates the Fase 0 research pipeline (0A event foundation, 0B
importer/registry, 0C outcome observer) in **shadow mode**: research events
are produced, imported, and reported without influencing any trading decision,
order, risk limit, or kill switch.

Shadow mode uses **public fixtures and synthetic data only**. Demo data must
not enter the research pipeline until the Deployment Security Gate passes with
actual host/user/runner/secret/transfer/backup evidence and an independent
security audit approves deployment. Until then the gate status remains
NOT PASSED / NOT VERIFIED, and this runbook does not authorize Demo data use,
forward-test activation, or order submission.

## Prerequisites

- Fase 0A, 0B, and 0C merged and their closure audits approved.
- Research exporter flag off by default; enabling it for a shadow run is a
  deliberate operator action recorded in the shadow report.
- Research database (`research.db`) on a host and user separate from trading
  state; the research user cannot read trading state, and the trading user
  cannot read `research.db`.
- No OKX private credentials, runner secrets, or environment secrets are
  present in the research environment.

## Shadow procedure

1. **Prepare.** Record the code version, effective config digest, fixture or
   public dataset identifier, and operator in the shadow report. Confirm the
   exporter flag state and the sealed-config attestation before starting.
2. **Run.** Execute the shadow cycle with the research exporter enabled only
   for this run. Trading behaviour must remain identical to the flag-off
   baseline on the same fixture.
3. **Import.** Import exported events with the single-writer importer.
   Re-importing the same export must be a no-op; conflicting ID/payload pairs
   must be rejected, never overwritten.
4. **Observe.** Run the outcome observer over the imported episodes. Episodes
   with incomplete reconciliation must report `reconciliation_incomplete` and
   must not produce finalized accounting.
5. **Report.** Produce the shadow report (template below) and archive the
   export, import log, and report together.

## Data completeness checks

A shadow run is complete only when all of the following hold:

- Every expected scheduler cycle has either a signed heartbeat and completion
  record, or an explicit `not_started` / `incomplete` gap record. Missing
  cycles are gaps, not silent skips.
- Event sequence numbers are contiguous per source; any gap is listed in the
  report with its sequence range.
- Importer results account for every exported event: imported, duplicate, or
  rejected with reason. Counts must reconcile exactly.
- Registry hash chain verifies from genesis to the latest checkpoint, and
  the external checkpoint matches the registry head.
- Every trade episode either reaches finalized accounting with reconciled
  fills/fees or is explicitly reported incomplete or legacy-unavailable.
- No raw order ID, credential, signature, or private balance appears in any
  exported payload, registry row, or report.

## Reproducibility

- Re-running the importer on the same export produces no new rows and the
  same registry head hash.
- Re-running the observer on the same state produces the same event IDs and
  payloads (idempotent reconstruction).
- Registry statistics (trial counts, PBO, DSR) recomputed from the registry
  match the values recorded at import time.
- The shadow report records the exact inputs needed to reproduce the run:
  code version, config digest, fixture/dataset identifier, and checkpoint
  hash before and after the run.

## Monitoring

- Heartbeat gaps beyond one expected cycle interval raise a gap alert in the
  report; two consecutive missing cycles stop the shadow run.
- Exporter failures are counted and reported; they must never change trading
  behaviour. Any evidence that export failure altered a decision stops the
  run immediately. A single exporter failure marks the run incomplete: the
  affected sequence range is recorded as a gap, and the run must not be
  reported as complete. Two consecutive exporter failures, or any exporter
  failure whose cause cannot be identified, stop the run and are recorded
  as an integrity incident. The run may resume only after the exporter is
  verified healthy, the gap range is recorded, and the recovery decision is
  noted in the shadow report; results from an incomplete run must not be
  promoted as complete evidence.
- Importer rejections above zero are listed individually; a rejection caused
  by payload conflict is treated as an integrity incident, not noise.
- Registry checkpoint mismatch stops the run and blocks further imports
  until investigated.

## Recovery

- **Exporter crash:** restart from the last persisted sequence; events after
  the gap are imported normally and the gap remains visible in the report.
- **Importer crash:** re-run the import; idempotency guarantees no duplicate
  rows. Verify the hash chain before resuming.
- **Registry corruption or checkpoint mismatch:** stop, preserve the
  database and checkpoint evidence unchanged, and escalate to Gan. Do not
  repair by rewriting history; recovery uses the last verified checkpoint
  and re-import of archived exports.
- **Config or attestation mismatch:** stop the run; do not promote any
  result produced under a mismatched effective config. Preserve the
  mismatched config and attestation evidence unchanged, escalate the
  mismatch to Gan as an integrity incident, and record the recovery
  decision before any further run. No shadow run may continue or restart
  until the effective config is re-verified against the attestation.

## Shadow report template

```text
Shadow run ID:
Date/time (UTC):
Operator:
Code version:
Effective config digest:
Fixture / dataset identifier:
Exporter flag: on (shadow only)
Checkpoint before:
Checkpoint after:
Expected cycles / completed / gaps:
Events exported / imported / duplicate / rejected:
Episodes finalized / incomplete / legacy-unavailable:
Registry statistics recomputed: match | mismatch
Completeness checks: pass | fail (list failures)
Incidents and recovery actions:
Conclusion: shadow only — no trading impact, no Demo data
```

## Stop conditions and approvals

Stop the shadow run and escalate to Gan on any of: checkpoint mismatch,
payload-conflict rejection, evidence of trading-behaviour change, discovery
of private data in research outputs, two consecutive missing heartbeat
cycles, two consecutive exporter failures or an exporter failure with an
unidentified cause, or a config/attestation mismatch.

Starting a shadow run with the exporter enabled, opening per-trade outcomes
beyond aggregates, and any use of Demo data each require Gan's explicit
approval. This runbook grants none of those approvals by itself.
