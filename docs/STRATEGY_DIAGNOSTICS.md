# NEBULA Strategy Diagnostics — Historical v0.16.0 Baseline

> Historical report carried forward for reference. It is not a current strategy validation or a claim of profitability.

This release adds an offline, read-only diagnostic command. It does not connect to private OKX endpoints and does not submit, cancel, or amend orders.

## Command

```bash
nebula diagnose-portfolio \
  --asset BTC-USDT=/path/to/BTC-USDT-1H.csv \
  --asset ETH-USDT=/path/to/ETH-USDT-1H.csv \
  --asset SOL-USDT=/path/to/SOL-USDT-1H.csv \
  --trades-output reports/trades.csv \
  --sensitivity
```

All input datasets must have identical ascending candle timestamps and at least 60 confirmed candles. The command reports aggregate trade metrics, per-symbol metrics, exit-reason metrics, entry-regime metrics, an equal-weight buy-and-hold reference, and optionally exploratory EMA sensitivity results. A detailed trade ledger can be exported to CSV.

## Metric definitions

- Win: net trade PnL greater than zero after modeled entry/exit fees and slippage.
- Loss: net trade PnL less than zero. Break-even is reported separately.
- Profit factor: gross positive net PnL divided by the absolute gross negative net PnL. It is `null` and flagged unbounded when there are wins and no losses; it is `0` when there are no wins.
- Expectancy: arithmetic mean of net PnL per closed trade.
- Holding duration: number of evaluated bars between entry and exit.
- Fees: sum of modeled entry and exit fees stored in the backtest trade ledger.

## Regime labeling

The signal regime is labeled at `signal_ts`, the previous completed candle before the simulated entry. `uptrend` requires fast EMA above slow EMA and slow EMA above its value five closed bars earlier. Other valid observations are labeled `sideways_or_non_uptrend`. Insufficient indicator history is separately labeled. This is descriptive grouping, not proof of causation and not an additional strategy filter.

## Benchmark

The benchmark allocates starting equity equally across the supplied symbols, buys at the first test candle open, and liquidates at the last test candle close. The same configured fee and slippage assumptions are applied on both sides. It is a simple passive reference, not risk-matched to the one-position active strategy; benchmark drawdown is therefore not compared in this release.

## Parameter sensitivity safeguards

`--sensitivity` evaluates nine fixed fast/slow EMA pairs on the first 70% of the post-warm-up period. It does not select a winner and does not evaluate the reserved final 30%. Do not use these exploratory results to claim out-of-sample validation. Any candidate must be frozen before a separately designed validation and untouched holdout procedure.

## Important limitations

- Candle OHLC cannot reveal the exact intrabar path. The backtester uses conservative stop-first priority when stop and target are both touched in one candle.
- Historical fees and slippage are assumptions, not measured execution costs.
- Regime labels and passive benchmark are descriptive references.
- A diagnostic report does not establish profitability or authorize Demo order execution.
- All outputs are offline simulations; `orders_submitted` remains zero.
