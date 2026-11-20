# NEBULA v0.16.4

## Scanner correctness and ranking patch

- Volatility is now mean true range divided by close, expressed as percent. This includes gaps relative to the prior valid closed candle.
- Confirmed zero-volume candles are excluded from volatility calculation so no-trade bars do not artificially lower the estimate.
- Candidate ranking now has an explicit deterministic order: higher 24h quote-currency turnover, tighter spread, volatility nearer the midpoint of the accepted volatility band, older listing, more candle history, then instrument ID.
- The first two ranking priorities are liquidity and spread. Volatility remains bounded by the configured eligibility range. Listing age and data-point count remain eligibility checks as well as tie-breakers.

## Deliberately unchanged

- EMA crossover entry/exit strategy and all backtest parameters.
- OKX endpoint behavior and request frequency.
- Demo-only safety gates and disabled-by-default order submission.
- No API credentials, private account endpoints, or orders used in this patch.

## Validation

- 200 automated tests passed in the build environment.
- Compile and CLI checks are recorded in BUILD_REPORT_v0.16.4.md.
- Scanner changes are not proof of predictive edge or profitability.
