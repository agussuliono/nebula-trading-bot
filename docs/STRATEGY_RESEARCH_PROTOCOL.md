# NEBULA Strategy Research Protocol — Prospective Independent Data

Date: 2026-11-20
Branch: `audit/fix-safety-gates-2026-11-20`

## Purpose

Establish a genuinely unseen, timestamp-disjoint evaluation set after the previously examined BTC/ETH/SOL 1H sample and rejected EMA/breakout candidates. This protocol is research-only. It does not enable Demo order submission or live trading.

## Evidence and constraints

- The previously used sample spans approximately 2026-07-18 through 2026-11-19 and has already been examined. Its former holdout is no longer independent.
- The previous EMA crossover result was negative (about -11.08% net return, 12.03% maximum drawdown, profit factor 0.33). The breakout candidate also failed its exposed holdout and cost stress. Both candidates remain rejected; do not retune them against the old sample.
- OKX V5 public market data exposes candle confirmation status: `0` is unfinished and `1` is completed. The history-candles endpoint is limited to a rolling historical window (the current OKX documentation describes up to three months), so downloading it again today would substantially overlap the old sample. That is not an independent test.
- Therefore, a clean prospective dataset must be accumulated after the previous sample's final timestamp. Historical downloads that overlap the old sample may be used only for data-pipeline testing, not as fresh strategy evidence.

## Frozen research rules

1. Keep the old sample, old holdout, and rejected candidate results frozen. No parameter tuning on them.
2. Start a prospective UTC 1H collection after 2026-11-20T01:00:00Z (the previous sample's final candle opening timestamp was 2026-11-20T01:00:00Z). Do not use any candle whose timestamp is at or before that cutoff for independent evaluation. The first eligible 1H candle opening is 2026-11-20T02:00:00Z.
3. Collect the same eligible universe consistently. Initial diagnostic symbols: BTC-USDT, ETH-USDT, SOL-USDT. This is a fixed diagnostic basket, not proof that the dynamic asset selector is unbiased.
4. Retain original OKX rows/CSV, SHA-256 manifest, UTC timestamps, and all gap/zero-volume reports. Never fill missing candles silently.
5. Only confirmed candles may enter signal calculations. Entry simulation occurs no earlier than the next candle open.
6. Include actual fee assumptions and adverse slippage. Report base costs and at least 2x cost stress. If the strategy only works at optimistic costs, reject it.
7. Compare with a clearly defined passive benchmark over the exact same timestamps. State that it is not risk-matched.
8. Use chronological walk-forward evaluation. Do not shuffle bars. Do not inspect a final holdout during development.
9. Report trade count, net return, max drawdown, profit factor, expectancy, fees, exposure, per-symbol contribution, and fold-by-fold out-of-sample results. A result with too few trades is inconclusive, not successful.
10. A strategy remains rejected unless it demonstrates positive out-of-sample evidence after costs, acceptable drawdown under the configured risk budget, and consistency across more than one market regime. No single metric is sufficient.

## Data collection commands

The incremental collector is implemented as `nebula collect-prospective`. It uses only the public `GET /api/v5/market/candles` endpoint, needs no API credentials, and cannot submit or cancel orders.

Before using it, copy each exact frozen baseline CSV **and its matching manifest** into a separate prospective directory. Do not point the collector at the original evidence files. The collector verifies the original CSV SHA-256 against the three hashes recorded in `src/nebula/prospective.py`, checks the manifest, and refuses any symbol outside BTC/ETH/SOL, any non-1H bar, or any changed cutoff. Keep the original source files untouched.

Example (replace the source paths with the exact original files on the laptop):

```bash
mkdir -p ~/NEBULA/research/prospective
cp /path/to/frozen/BTC-USDT-1H.csv ~/NEBULA/research/prospective/BTC-USDT-1H.csv
cp /path/to/frozen/BTC-USDT-1H.csv.manifest.json ~/NEBULA/research/prospective/BTC-USDT-1H.csv.manifest.json
cp /path/to/frozen/ETH-USDT-1H.csv ~/NEBULA/research/prospective/ETH-USDT-1H.csv
cp /path/to/frozen/ETH-USDT-1H.csv.manifest.json ~/NEBULA/research/prospective/ETH-USDT-1H.csv.manifest.json
cp /path/to/frozen/SOL-USDT-1H.csv ~/NEBULA/research/prospective/SOL-USDT-1H.csv
cp /path/to/frozen/SOL-USDT-1H.csv.manifest.json ~/NEBULA/research/prospective/SOL-USDT-1H.csv.manifest.json
```

Run the collector once per symbol, then repeat at least daily:

```bash
nebula collect-prospective --symbol BTC-USDT --bar 1H --output ~/NEBULA/research/prospective/BTC-USDT-1H.csv
nebula collect-prospective --symbol ETH-USDT --bar 1H --output ~/NEBULA/research/prospective/ETH-USDT-1H.csv
nebula collect-prospective --symbol SOL-USDT --bar 1H --output ~/NEBULA/research/prospective/SOL-USDT-1H.csv
```

The collector appends only confirmed candles strictly after the frozen cutoff and after the last stored candle. It rejects duplicates, overlap, conflicting history, gaps, unconfirmed rows, altered CSV/manifest pairs, and a baseline that does not match the frozen SHA-256. It records the baseline hash, appended count, cutoff, latest timestamp, and updated CSV hash. It never fills missing candles. Because the public candles endpoint returns at most 300 rows per call, collect regularly; if the local dataset falls more than the available window behind, the strict continuity check stops rather than silently skipping bars. The CSV and manifest are staged before replacement, but the two-file replacement is not a single filesystem transaction if the process or OS fails between replacements.

If the command reports `no_new_confirmed_candles`, no file is changed. The collector has not run on this user's laptop yet, so no new prospective observations are claimed at this stage.

Audit each CSV:

```bash
nebula audit-data --bar 1H --data ~/NEBULA/research/prospective/BTC-USDT-1H.csv
nebula audit-data --bar 1H --data ~/NEBULA/research/prospective/ETH-USDT-1H.csv
nebula audit-data --bar 1H --data ~/NEBULA/research/prospective/SOL-USDT-1H.csv
```

Do not run strategy tuning until all three files cover the same prospective timestamps, the post-cutoff sample is large enough, and gaps/zero-volume bars have been reviewed. A 90-day prospective sample takes approximately 90 days to accumulate at 1H resolution.

## Current exit criteria

- Data pipeline changes pass CI.
- A dataset manifest cannot falsify canonical provenance fields.
- The research period is strictly later than the old sample cutoff.
- No strategy is declared successful without sufficient unseen out-of-sample trades and cost-stressed results.
- All order gates remain fail-closed; this phase must not send orders.
