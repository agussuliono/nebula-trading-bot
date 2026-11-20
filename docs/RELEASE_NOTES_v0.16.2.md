# NEBULA v0.16.2

## Purpose
Correctness and risk-control patch for the offline backtest engines. No strategy parameter was tuned and no profitability claim is made.

## Fixes
- A bullish signal created on a candle that triggered the daily-loss circuit breaker is no longer queued for execution after the next UTC-day reset.
- The fix is applied to both the single-symbol and synchronized portfolio backtesters.
- Final liquidation costs are included when calculating reported maximum drawdown.

## Safety
- Demo-only project constraints unchanged.
- No live trading mode, leverage, margin, futures, Grid, or withdrawal capability added.
- No order endpoint is called by tests or offline backtests.
- Order gate remains OFF by default.

## Validation
Validation results are recorded in the accompanying build report. Historical performance must be rerun against the user's original BTC/ETH/SOL datasets before deciding whether the strategy is suitable for any Demo forward test.
