# NEBULA v0.16.3 — Build and Regression Report

## Scope
Correctness-only patch to fee/slippage-aware stop-risk sizing in the offline single-symbol and portfolio backtest engines.

## Code change
- Entry fill price already includes adverse buy slippage.
- Old sizing approximation charged an additional entry-slippage term on that already-adjusted price.
- New per-unit risk is exact cash outlay at entry minus net liquidation proceeds at the stop:
  `entry_price * (1 + fee) - stop_price * (1 - slippage) * (1 - fee)`.
- Applied in both `Backtester` and `PortfolioBacktester`.
- Aligned the reusable `risk.size_position` helper with the same net-cash-loss formula and reserved entry fees within the equity affordability cap. This helper currently has no call sites in the runtime, so it is not claimed as a live execution change.
- Strategy signals, parameters, and selection behavior are unchanged.

## Validation
- `python -m pytest -q`: 197 passed.
- `python -m compileall -q src tests`: exit code 0.
- `PYTHONPATH=src python -m nebula.cli --help`: exit code 0.
- Offline portfolio baseline on the supplied BTC/ETH/SOL 1H data is unchanged because the default 25% notional cap binds before risk sizing:
  - Return: -11.0816%
  - Maximum drawdown: 12.0254%
  - Trades: 47
  - Profit factor: 0.3326
  - Positive walk-forward folds: 0 of 3
- No exchange connection or order submission. `orders_submitted=0`.

## Interpretation
This patch improves risk calculation correctness; it does not improve the trading strategy's historical return. The current EMA strategy remains unfit for Demo forward testing. The failed breakout candidate remains rejected. Do not tune against the already-observed holdout segment; obtain fresh independent data before another final validation.

## Safety
Spot-only, Demo-only project constraints remain unchanged. Demo order submission remains disabled by default. No credentials were used.
