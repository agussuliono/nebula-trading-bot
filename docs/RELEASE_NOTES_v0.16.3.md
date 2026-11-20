# NEBULA v0.16.3

## Correctness patch

- Corrected risk-per-unit sizing in single-symbol and portfolio backtests.
- Uses exact cash loss at stop after entry fee, adverse stop-exit slippage, and exit fee.
- Removes duplicate entry-slippage charge: entry price already includes adverse buy slippage.
- Adds regression tests for the corrected formula.

## Deliberately unchanged

- EMA crossover strategy and its parameters.
- Spot-only, Demo-only safety gates.
- Order submission defaults remain disabled.
- No OKX connection or orders are part of this patch.

## Validation

Validation results are recorded in BUILD_REPORT_v0.16.3.md after running the test suite.
