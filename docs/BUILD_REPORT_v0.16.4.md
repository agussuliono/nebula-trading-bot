# Build report — NEBULA v0.16.4

## Scope

Scanner correctness and deterministic candidate ranking only. No strategy or order-execution changes.

## Changes

1. Volatility uses mean true range as percent of close.
2. Zero-volume and unconfirmed candles are excluded from volatility calculation.
3. Candidate ranking explicitly considers turnover, spread, volatility-band proximity, listing age, and candle-history count.
4. Added regression tests for the new behavior.

## Verification

- `python -m pytest -q`: 200 passed.
- `python -m compileall -q src tests`: passed.
- `python -m nebula.cli --help`: passed.
- ZIP integrity: passed.
- Historical CSV check: volatility primitives evaluated offline on the supplied BTC/ETH/SOL 1H datasets. Latest 24-bar mean true-range estimates: BTC 1.0670%, ETH 0.6212%, SOL 0.6505%. These are descriptive volatility measurements, not return forecasts.

## Limitations

- The ranking hierarchy is a transparent engineering choice, not a statistically proven return predictor.
- Best bid/ask spread is top-of-book only; market depth and executable slippage are not inferred from it.
- Volatility thresholds need empirical calibration using independent data; no strategy-performance claim is made.
- No new independent market sample was used to validate profitability.
