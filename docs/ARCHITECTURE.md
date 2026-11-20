# Architecture and implementation notes

```text
OKX public market data
        |
        v
Universe scanner ---> instrument/data quality filters
        |
        v
Closed-candle strategy ---> signal
        |
        v
Risk manager ---> quantity, fee/slippage, SL/TP plan
        |
        v
SQLite intent/idempotency store (durable reservation + audit events)
        |
        v
Demo-only order manager (no retry; unknown outcomes remain blocked) ---> OKX V5 Demo REST
        |
        v
Reconciliation + audit log + circuit breaker

Separate read-only account-risk path:
OKX Demo account config + balance + public Spot tickers
        |
        v
Portfolio valuation (best bid) + local fill-ledger reconciliation
        |
        v
Unrealized P&L + stop risk + fee/slippage + daily loss gate
        |
        v
Read-only report (no order path)
```

## Decisions

- Python 3.11+; standard library for core logic, `requests` for REST.
- REST first to reduce moving parts; WebSocket is deferred until polling reliability is measured.
- `openapi.okx.com` Demo REST host is hard-coded and every private request is marked simulated. The host is not configurable.
- The strategy is an EMA trend-following primitive only. RSI and volume confirmation are not silently assumed to be finalized.
- No live-mode flag, withdrawal method, margin/futures API, or Grid implementation exists.
- Order submission is fail-closed and remains disabled by default.

## Open design decisions

- Exact bar interval and trading session.
- Risk budget and maximum position size in quote currency (previously discussed figures were proposals, not yet treated as approved production configuration).
- Universe thresholds for turnover, spread, volatility, listing age and candle history.
- Stop/target model and supported OKX conditional-order semantics.
- Whether 14-day forward test begins only after backtest and all safety tests pass.

## Read-only runtime (v0.11.0)

`ReadOnlyRuntime.run_once()` orchestrates only public-data analysis:

1. Fetch live Spot instruments and tickers.
2. Rank the liquid subset to bound candle API usage.
3. Fetch each candidate's candles once, require confirmed/closed and fresh bars, and calculate volatility/data depth.
4. Apply quote-volume, spread, volatility, listing-age, and history filters.
5. Evaluate the EMA signal on the cached closed candles.
6. Persist each signal observation idempotently and report local position-lock/circuit-breaker blockers.

The runtime deliberately has no order-submission dependency. `nebula analyze-once` is a one-shot diagnostic command; it is not a continuous trading daemon and does not manage a position.


## Portfolio and mark-to-market risk (v0.12.0)

`nebula portfolio-once` is a separate read-only diagnostic path. It fetches the account mode and Spot balances through private GET endpoints, then values every nonzero holding using a fresh direct `ASSET-USDT` best bid. USDT is valued at par. Missing or stale prices, non-Spot account mode, auto-loan, unsupported assets, duplicate rows or balance/ledger mismatch block entry.

The active position basis is reconstructed from fills attached to its entry intent and explicitly linked exit intents/OCO child orders. Entry status and cumulative fill snapshot must reconcile exactly; unresolved local exits and exchange regular orders must be absent; the active OCO must be confirmed in exchange pending algo orders. Base- and quote-currency fees preserve their signed OKX values. Unlinked legacy sells are not guessed; if legacy sell fills are present for the active instrument, reconstruction fails closed.

Unrealized P&L is marked net of estimated exit fee and slippage. Stop risk includes the distance from average cost to persisted stop plus exit costs. A proposed entry must provide instrument, quantity, entry, stop and target; estimated entry and exit costs are included in its risk. Without a valid proposed entry, the report never grants entry permission. The `portfolio-once` CLI is snapshot-only and intentionally supplies no proposed entry, so it never grants entry permission. The engine also checks realized plus unrealized daily P&L and projected daily P&L after the proposed stop.

Fee/slippage defaults are provisional 10 bps each and configurable via environment variables. They are not a verified OKX fee tier. The account read-only command does not instantiate an order manager and reports zero order attempts. It is a snapshot tool, not a continuous risk monitor.


## Offline backtest (v0.13.0)

`backtest.py` is isolated from the exchange order path. It consumes validated ascending OHLCV candles, evaluates EMA crossover only on confirmed bars, enters on the next open, simulates one long Spot position, and accounts for fees/slippage, fixed SL/TP, daily-loss and drawdown entry halts. Walk-forward and transaction-cost stress runners reuse the same deterministic simulator. No function in this module can submit an exchange order.
