# NEBULA v0.16.2 — Build and Regression Report

## Scope
Offline backtest correctness patch. No strategy parameters were optimized. No exchange order endpoints were called.

## Changes
- Suppress a pending bullish entry signal if its signal candle also tripped the daily-loss circuit breaker.
- Apply the same prior-candle daily-halt guard in the synchronized multi-asset portfolio simulator across UTC day rollover.
- Include final liquidation in the final reported maximum drawdown calculation.
- Add two regression tests that reproduce the stale-signal bug in the single-symbol and portfolio engines.

## Verification in build environment
- `python -m compileall -q src tests`: passed.
- `python -m pytest -q`: 193 passed.
- `PYTHONPATH=src python -m nebula.cli --help`: passed.
- Regression tests were also run with the fix deliberately disabled: both failed as expected; restored fix then full suite passed.

## Not verified
- User laptop installation of v0.16.2.
- Historical performance on the user's BTC/ETH/SOL data; source CSVs were not attached in this turn.
- Any OKX connectivity, private API, Demo order, or 14-day forward test.

## Safety
- No order submitted; this patch changes only offline backtest behavior.
- No live trading mode, leverage, margin, futures, Grid, or withdrawal capability added.
- `NEBULA_ENABLE_DEMO_ORDERS` remains disabled by default.
