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
2. Start a prospective UTC 1H collection after 2026-11-19T23:00:00Z (the previous sample's final candle opening timestamp was 2026-11-19T23:00:00Z). Do not use any candle whose timestamp is at or before that cutoff for independent evaluation.
3. Collect the same eligible universe consistently. Initial diagnostic symbols: BTC-USDT, ETH-USDT, SOL-USDT. This is a fixed diagnostic basket, not proof that the dynamic asset selector is unbiased.
4. Retain original OKX rows/CSV, SHA-256 manifest, UTC timestamps, and all gap/zero-volume reports. Never fill missing candles silently.
5. Only confirmed candles may enter signal calculations. Entry simulation occurs no earlier than the next candle open.
6. Include actual fee assumptions and adverse slippage. Report base costs and at least 2x cost stress. If the strategy only works at optimistic costs, reject it.
7. Compare with a clearly defined passive benchmark over the exact same timestamps. State that it is not risk-matched.
8. Use chronological walk-forward evaluation. Do not shuffle bars. Do not inspect a final holdout during development.
9. Report trade count, net return, max drawdown, profit factor, expectancy, fees, exposure, per-symbol contribution, and fold-by-fold out-of-sample results. A result with too few trades is inconclusive, not successful.
10. A strategy remains rejected unless it demonstrates positive out-of-sample evidence after costs, acceptable drawdown under the configured risk budget, and consistency across more than one market regime. No single metric is sufficient.

## Data collection commands

Run on the user's laptop using the installed project CLI. These commands use public market-data endpoints only; they do not need API credentials and do not submit orders.

```bash
mkdir -p ~/NEBULA/research/prospective
cd ~/NEBULA/nebula-trading-bot-v0.16.6
source ../nebula-trading-bot-v0.16.3/.venv/bin/activate

nebula download-history --symbol BTC-USDT --bar 1H --candles 2160 \
  --output ~/NEBULA/research/prospective/BTC-USDT-1H.csv
nebula download-history --symbol ETH-USDT --bar 1H --candles 2160 \
  --output ~/NEBULA/research/prospective/ETH-USDT-1H.csv
nebula download-history --symbol SOL-USDT --bar 1H --candles 2160 \
  --output ~/NEBULA/research/prospective/SOL-USDT-1H.csv
```

Important: a download made immediately after the old sample will overlap the old sample because OKX's rolling history window returns recent data. Do not call it independent. The prospective collection should be appended only as new candles close, and the cutoff must be enforced in the research dataset. A complete 90-day prospective sample takes approximately 90 days to accumulate at 1H resolution.

Audit each CSV:

```bash
nebula audit-data --bar 1H --data ~/NEBULA/research/prospective/BTC-USDT-1H.csv
nebula audit-data --bar 1H --data ~/NEBULA/research/prospective/ETH-USDT-1H.csv
nebula audit-data --bar 1H --data ~/NEBULA/research/prospective/SOL-USDT-1H.csv
```

Do not run strategy tuning until all three files cover the same prospective timestamps, the post-cutoff sample is large enough, and gaps/zero-volume bars have been reviewed. The exact collection mechanism for incremental forward updates remains to be implemented; do not repeatedly overwrite a dataset that is being preserved as evidence.

## Current exit criteria

- Data pipeline changes pass CI.
- A dataset manifest cannot falsify canonical provenance fields.
- The research period is strictly later than the old sample cutoff.
- No strategy is declared successful without sufficient unseen out-of-sample trades and cost-stressed results.
- All order gates remain fail-closed; this phase must not send orders.
