# NEBULA V1 — specification audit (2026-11-19)

## Locked constraints

| Requirement | Current foundation status | Evidence / remaining work |
|---|---|---|
| OKX Spot only | Partial | Order adapter uses `tdMode=cash`; instrument precision and `SPOT` filtering still need validation. |
| Demo-only, impossible to switch host via env | Implemented in foundation | REST host constant is `https://openapi.okx.com`; no configurable base URL; private headers carry `x-simulated-trading: 1`. Still require a later negative integration test against a deliberately misconfigured environment. |
| No withdrawal | Implemented in foundation | No withdrawal method or endpoint; API key should be created with Read + Trade only, never Withdraw. |
| Trend-following | Partial | EMA crossover primitive exists; RSI and volume confirmation are not implemented. |
| Automatic asset selection | Partial | Candidate filter/ranking function exists; OKX market data normalization, spread calculation, volatility calculation, listing-age parsing and data-quality scoring remain. |
| One active position | Foundation implemented | SQLite singleton position lock is transactional; full runtime/account reconciliation and recovery orchestration remain. |
| Closed candles only | Implemented in strategy primitive | OKX confirmation flag `confirm=1` is filtered; integration response shape still needs validation. |
| SL/TP before entry | Not implemented | Must define trigger-order behavior, precision, partial-fill protection and failure policy before enabling orders. |
| Risk sizing incl. fees/slippage | Partial | Position-sizing function includes estimated round-trip fee/slippage; exchange fee tier and live spread impact still need calibration. |
| Duplicate-order prevention | Partial | SQLite unique intent reservation and deterministic client ID exist; order manager must reconcile by `clOrdId` after uncertain outcomes. |
| Partial fills, API failures, stale data, restart | Partial | Durable order snapshots, cancellation race handling, OCO child state and fill ledger exist; stale-data gating and complete runtime startup orchestration remain. |
| Daily loss limit / circuit breaker / emergency stop | Partial | Kill-switch gate and required risk-limit settings exist; P&L accounting and automated circuit-breaker transitions are not implemented. |
| Auditable activity log | Partial | SQLite audit events and durable execution ledger exist; full lifecycle coverage and log-retention policy remain. |
| Backtest, fee/slippage, stress, forward test | Not implemented | Must be completed before 14-day Demo forward test. |

## Safety review findings

1. Order submission defaults OFF and fails closed if credentials or nonzero limits are missing.
2. The REST host cannot be overridden through environment variables. Do not add a configurable production host.
3. Demo API key secrets remain local environment variables and must never be pasted into chat or committed.
4. Use an OKX Demo API key with only Read and Trade permissions; never enable Withdraw.
5. The current order adapter does not yet manage protective SL/TP orders. Therefore the order gate must remain OFF until the order manager and protective exits are complete and tested.
6. The current tests use a mocked HTTP session. They prove local guards and request construction only; they do not prove OKX Demo connectivity or exchange-side behavior.

## Open configuration decisions

- Trading bar interval and maximum holding period.
- Simulated starting equity and quote currency.
- Approved per-trade risk, max notional, max daily loss, and trades-per-day limits.
- Minimum quote turnover, maximum spread, volatility band, minimum listing age and minimum candle history.
- Stop-loss and take-profit model, including whether target is fixed R-multiple or volatility-based.
- Exchange fee assumption and slippage stress scenarios.

Earlier conversational figures such as a Rp210,000 position cap, Rp2,000 target risk, Rp3,000 hard risk cap, two trades/day and Rp6,000 daily loss were proposals; they are intentionally not silently promoted to approved configuration.

## v0.5.0 follow-up

- One active local position slot is now enforced transactionally in SQLite.
- Entry and exit intents are distinguished: only a buy may acquire the slot; a sell must reference the exact locked entry and instrument.
- Ambiguous order outcomes retain the slot across restart. Definitive zero-fill rejection/cancellation may release it.
- Remaining limitation: the flat-check release method validates caller-supplied balance and open-order evidence but does not itself fetch or cryptographically attest that evidence. A dedicated portfolio reconciler must wire authenticated Demo balance + standard/algo open-order reads before this method is used by runtime automation.
- Protective exit orders are still not implemented. Order submission must remain disabled by default.

## v0.6.0 audit notes

- TP and SL must be one-cancels-the-other (OCO), not a `conditional` order carrying both legs: OKX documents that a conditional order with both TP/SL fields executes only the stop-loss leg.
- OCO exit side is `sell` for a Spot long; `tdMode` is `cash`; order size is base-asset quantity; both order prices use `-1` for market execution after trigger.
- The plan builder accepts only the already-filled size supplied by the caller, but there is not yet a durable fill-delta/OCO reconciliation workflow. Partial fills therefore remain a release blocker.
- A local account snapshot must include available and frozen base balance plus pending regular and algo orders. Any failed read invalidates the snapshot. Lock release remains an explicit separate operation and is not automated by the new reconciler.
- Algo cancel is guarded by the same Demo-only order permission and must never be used to cancel unrelated user orders.

## v0.7.0 audit additions

- OCO intent persistence is independent from exchange acknowledgement, closing the crash window where a process could otherwise forget the client algo ID.
- The manager waits for a terminal entry state so the protected quantity is final; partial fill with an unfilled live remainder is not considered safe to protect yet.
- The quantity must match current live Spot instrument rules exactly; no upward rounding is permitted.
- OCO lookup uses the official algo details endpoint with the persisted client algo ID. Unknown/missing outcomes remain blocked; no automatic retry is allowed.
- Triggered OCO state does not unlock the sole position slot. A triggered order may still be pending, partially filled, or failed; balance and child-order reconciliation remains mandatory.
- Residual risk: while the entry remainder is being canceled and reconciled, any already-filled partial exposure has no exchange-side protective OCO yet. This release therefore remains a testable lifecycle foundation, not production-safe autonomous protection. The order gate stays disabled by default.


## v0.8.0 audit additions

- The entry remainder can now be canceled through the fixed Demo REST adapter, with the same order gate as all other writes.
- The cancellation acknowledgement is explicitly not treated as terminal state. A second GET order lookup must provide `filled`/`canceled`/`mmp_canceled` evidence before protection can proceed.
- Timeout or rejection races are handled by attempting the independent lookup; lookup failure keeps the position slot locked.
- The final cumulative fill snapshot is persisted before local state advances. Partial-fill canceled entries retain their lock; a terminal zero-fill cancellation may release the unused slot.
- Residual gaps: method is not wired into the runtime strategy loop; OCO child order/fill/fee reconciliation and P&L/circuit breaker remain incomplete. No private API or order was used in tests.


## v0.9.0 audit additions

- OKX `GET /api/v5/trade/order` accepts `ordId` with `instId`; the triggered algo details response exposes `ordIdList` for spawned regular orders. NEBULA persists every discovered child ID and independently queries each child.
- OKX Spot fill history (`GET /api/v5/trade/fills-history`) exposes trade-level `tradeId`, `ordId`, `fillSz`, `fillPx`, `fee`, `feeCcy`, `fillTime` and `billId` pagination. The implementation uses `billId` as the `after` cursor and fails closed at its configured pagination cap.
- OKX documents fee sign semantics: fee deduction is negative and rebate is positive. The v0.8.0 snapshot's absolute-value normalization was therefore incorrect for rebates; v0.9.0 preserves the sign.
- Fill IDs are idempotent and conflicting economic data for the same instrument/order/trade key raises an integrity error.
- Realized P&L is only calculated after the fill ledger's net base inventory is exactly zero and all fee currencies can be accounted for in base/quote. Unknown fee currency blocks accounting completion.
- A separate finalizer checks the fill ledger and then fetches a fresh account snapshot. It refuses release when residual base balance, open regular/algo orders, or unresolved child order states remain. This is still not connected to an autonomous loop.
- 93 local mock tests pass. No OKX private authentication or order submission was used.
- Terminal OCO states are now monotonic: a stale/missing algo lookup cannot downgrade `triggered`, `canceled`, or `failed` to `active`/`submission_unknown`. Missing details for an acknowledged/terminal OCO raises and retains the position lock.

## v0.11.0 runtime audit

- Runtime scope is intentionally restricted to public market data and local persistence. The runtime module has no order manager dependency or exchange write method calls; the CLI does not read private account data.
- Candidate candle requests are bounded by `max_symbols_to_analyze`; candles are cached within the cycle to avoid duplicate market-data calls.
- Only OKX-confirmed closed candles (`confirm == "1"`) are passed to volatility and EMA calculations. A candle timestamp in the future or older than the configured interval bound is rejected.
- Repeated signal observations are idempotent by instrument, candle timestamp, and action. A new candle may produce a new observation; no signal observation creates an order intent.
- Residual risk: a one-shot public scanner cannot guarantee exchange-side position state or protect an open position. It is not suitable for autonomous order execution. Private account reconciliation and a separately audited execution state machine are required before the order gate can be considered for Demo forward testing.


## v0.12.0 audit — portfolio risk

- Read-only portfolio path is isolated from order-writing methods. `portfolio-once` calls account config, account balance and public tickers only; it does not instantiate order/protection managers.
- Account mode must be Spot, auto-loan and Spot borrowing must be disabled, the API key must include Read permission, and Withdraw permission is rejected. Balance and ticker timestamps are bounded; duplicate rows, missing prices, crossed markets and unsupported currencies fail closed.
- Asset valuation uses best bid, not last price, to estimate conservative liquidation value. Only direct USDT-quoted holdings are supported.
- Local position basis is rebuilt from fills linked to the active entry and its exits. Base/quote fees preserve signed fee semantics; third-currency fees or missing timestamps invalidate basis. Exchange balance mismatch blocks entry. Entry fill snapshots must reconcile exactly to fill-ledger size; unresolved local exits, exchange regular orders, and missing/mismatched active OCO pending state block entry.
- Net unrealized P&L deducts estimated exit fee/slippage. Stop-risk includes stop loss distance plus exit costs. Proposed entry risk includes both entry and exit fee/slippage and requires entry, stop, target and size. Missing plan or zero daily-loss, per-position-risk or position-notional limits block entry.
- Daily loss check includes realized P&L plus active unrealized P&L; proposed stop risk is also compared against remaining daily loss budget.
- Known residual risk: fee/slippage defaults (10 bps each, configurable via environment variables) are assumptions, account balance `uTime`/ticker timestamp freshness can cause conservative false blocks, direct-USDT conversion only, and no private Demo integration test has been executed.
- No API credentials were supplied, no authenticated OKX call was made, no order was sent, and the 14-day forward test has not started.


## v0.13.0 audit — backtest, walk-forward and stress

- Look-ahead control: EMA crossover observed on bar `t` enters no earlier than bar `t+1` open; the trade record includes both timestamps. Only confirmed candles are accepted.
- Risk: one long Spot position maximum, no leverage/shorting; quantity is the minimum of stop-risk sizing and notional cap. SL/TP are fixed before the simulated entry can be managed.
- Costs: entry and exit fee plus adverse slippage are applied. Drawdown marks open inventory at candle low net of estimated liquidation costs.
- Conservative ambiguity: if SL and TP are both inside one candle, SL is assumed first; gap-through-stop uses the worse opening price. This is a model assumption, not proof of actual fill.
- Circuit breakers: daily loss threshold is measured against UTC day-start equity and includes conservative mark-to-market; max drawdown halts new entries. Open positions remain subject to their SL/TP/exit logic.
- Walk-forward: chronological, disjoint test windows; each fold starts flat and only earlier candles contribute to EMA warm-up. The engine does not optimize parameters or use future fold data.
- CSV validation rejects invalid OHLC, non-finite values, duplicate/out-of-order timestamps and insufficient history. Incomplete candles are excluded.
- Residual risks: OHLC bars cannot determine intrabar path, liquidity/market impact, partial fills, exchange latency, spread dynamics or third-currency fee conversion. Exchange tick/lot rounding and minimum size are not simulated. Results need out-of-sample stress tests and fee calibration.
- No API credentials, private OKX requests, Demo orders or live orders were used. Order gate remains OFF.


### v0.13.0 cost stress extension

- Added four repeatable fee/slippage scenarios: configured base, 1.5x moderate costs, 2x slippage, and 2x severe costs (bounded to supported limits).
- Cost stress holds strategy parameters and OHLCV data fixed; it is not an optimizer and does not select a supposedly best parameter set.
- CLI: `nebula backtest --data candles.csv --stress`. Walk-forward and stress modes are mutually exclusive per invocation.


## v0.15.0 audit — market data provenance and quality

- Historical fetch uses only the public market-data endpoint. The command constructs the client with empty settings rather than loading API credentials; no private authentication is used.
- Page size is capped at the official history-candle maximum of 100; pacing is 110 ms between pages, below the documented 20 requests per 2 seconds. The cursor must move strictly backwards or collection aborts.
- Only confirmed rows (`confirm=1`) are kept. Both 8- and 9-element documented candle shapes are accepted; duplicate timestamp rows with different OHLC values abort rather than silently choosing one.
- CSV and provenance manifest include a SHA-256 digest. Existing files cannot be overwritten unless `--overwrite` is explicit.
- Dataset audit rejects duplicate/out-of-order timestamps, invalid OHLC and interval mismatches; missing intervals are reported and never interpolated.
- Tests: 183 pass. Real OKX connectivity is unverified because the build environment could not resolve the host DNS. No market data performance conclusions can be drawn.
- Remaining: add listing/universe history to mitigate survivorship bias; calibrate spread/fees/slippage using Demo observations; assess multi-asset correlation and execution realism.


## v0.16.8 audit — exchange order snapshot identity and lifecycle

- Durable order snapshots now bind returned identity fields to the original local intent whenever OKX supplies them: instrument, side, product type, trade mode, and client order ID.
- An exchange order ID cannot change after it has been persisted or conflict with the order ID already acknowledged on the intent. Requested size is immutable because V1 does not amend regular orders.
- Cumulative fill size remains monotonic. Order lifecycle transitions from live/partially_filled are constrained to documented Spot states; once filled/canceled/mmp_canceled is observed, a stale response cannot regress the persisted terminal state.
- Added regressions for mismatched client/instrument/product/mode/side identity, changed order ID, changed requested size, terminal-state regression, and valid partial-fill-to-canceled progression.
- CI on commit 557fb7584457effa339380623ceb98a8e3418cf8: 454 tests passed in 3.69 seconds; compile and both explicit/default safety checks passed.
- Identity fields are checked when present to retain compatibility with older offline fixtures. Production OKX order-detail responses are expected to include these fields; strict real-Demo response-shape verification remains part of the pending read-only integration test.
- No credentials, authenticated OKX request, exchange write, or Demo/live order was used.


## v0.16.9 audit — monotonic intent state machine and crash-before-submit recovery

- Replaced unrestricted intent updates with an explicit forward-only state transition table. Unknown states, skipped transitions, terminal-state rewrites, and changes to an already persisted exchange order ID are rejected.
- Valid submission path is reserved → submitting → acknowledged/submission_unknown → exchange-observed live/partially_filled/terminal. Reconciliation may move ambiguous states only toward observed exchange states; terminal intents cannot be reopened.
- Added recovery for a durable reserved intent. The order manager marks it rejected locally and releases a zero-fill buy reservation without any exchange read/write. This is safe because the submit protocol persists submitting before calling the exchange write method; a process crash while still reserved proves the network submission was not attempted.
- A crash after submitting remains ambiguous and follows the read-only exchange reconciliation path; it is never automatically resubmitted.
- Added tests for invalid/skipped transitions, unknown status, terminal rewrite, exchange order ID mutation, and recovery of a pre-submit reserved entry without exchange calls.
- GitHub Actions run #345 / ID 44898364646 on commit c84b02a64baa4fdb82cf2396a08fe4db113adb3a: 457 tests passed in 4.06 seconds; compile, explicit Demo safety settings, and fail-closed defaults passed.
- No credentials, authenticated OKX account call, exchange write, Demo/live order, or real money was used.


## v0.16.10 audit — reconcile by immutable OKX ordId

- Once an exchange order ID is persisted, the order manager now queries order details by ordId instead of clOrdId. Pending-order fallback must match both the persisted ordId and local clOrdId.
- This avoids ambiguous lookups if a client ID is ever reused: OKX documents that GET /api/v5/trade/order returns only the latest match when clOrdId is associated with multiple orders. Local intent IDs remain idempotent in the durable database, but exchange ordId is the stronger lookup key once acknowledged.
- A response with a different ordId is rejected by the order snapshot validator; a pending row with the same clOrdId but different ordId is not accepted as the original order.
- Added tests for exact ordId lookup, mismatched order response, and pending fallback requiring the exact ordId.
- Official reference: https://app.okx.com/docs-v5/en/ (GET /api/v5/trade/order; clOrdId lookup behavior).
- GitHub Actions run ID 44898598648 on source commit 06d63736a1aa7e53b09178b2d6e2a64c16f28649: 460 tests passed in 5.54 seconds; compile, explicit safety settings, and fail-closed defaults passed.
- No API credentials, authenticated account calls, or exchange writes were used.


## v0.16.11 audit — public API isolation, diagnostics and resilience

- Public-only CLI commands check-demo-public and analyze-once now use a settings loader that reads only non-secret market-data assumptions. It never accesses API credentials or order-gate environment variables.
- HTTP errors expose bounded diagnostic fields only: status code, sanitized content type, numeric API error code, a short sanitized message, and allowlisted Server/Via/CF-Ray metadata with IP literals redacted. Non-JSON responses are identified without including response bodies, arbitrary headers, or request URLs.
- Exchange-provided error messages redact configured API key, secret and passphrase values.
- Public unauthenticated GET requests retry only transient network failures, HTTP 429 and HTTP 5xx, for at most three attempts with bounded exponential delays (250 ms, 500 ms). No retry is performed for 403/other 4xx, redirects, invalid JSON, or API business errors.
- Public-market circuit breaker trips after three exhausted transient calls, stays open for 15 seconds, then allows a single half-open probe. Definitive HTTP/API responses reset the transport breaker.
- Authenticated private GETs and every POST remain single-attempt. In particular, order submit/cancel/amend flows are not replayed automatically.
- The fixed REST host, Demo header, endpoint allowlist and disabled redirects remain unchanged.
- Regression tests cover credential-free public settings, 403 HTML diagnostics, secret redaction, retry bounds, transient circuit opening/recovery, and the no-retry private/write contract.
- The observed HTTP 403 from the user's network remains unresolved. Since response headers/body were not retained and no new request was authorized, its source cannot be attributed. This patch does not bypass access restrictions.
- CI fixtures are synthetic; they verify client policy, not real exchange connectivity or trading outcomes.

Status: implemented in review branch; pending CI verification. No authenticated request or exchange write used.
