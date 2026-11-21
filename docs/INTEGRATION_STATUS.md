# OKX Demo Integration — Stage 4 status

Date: 2026-11-19

## Verified from official OKX documentation

- Demo Trading uses REST `https://openapi.okx.com` with `x-simulated-trading: 1` on requests.
- Create a separate API key inside Demo Trading. Read is needed for account reads; Trade is needed for order operations. Do not grant Withdraw.
- Public Spot instrument metadata includes `tickSz`, `lotSz`, `minSz`, base/quote currency and instrument state.
- Spot `lotSz` and `minSz` are denominated in base currency. Order states include `live`, `partially_filled`, `filled`, `canceled` and `mmp_canceled`. Pending-order endpoint only returns live and partially-filled orders; terminal history needs a separate reconciliation path before production use.

References:
- https://app.okx.com/docs-v5/en/
- https://my.okx.com/docs-v5/en/

## Implemented in v0.4.0

- Added `SpotInstrument` parsing with strict SPOT type, identifiers, positive finite tick/lot/min sizes and tradable-state validation.
- Added Decimal-based order normalization: quantity floors to lot size; buy limit price floors to tick size; sell limit price rounds up to tick size; quantity below minSz is rejected.
- Order manager now fetches current Spot instrument metadata and only accepts the requested USDT-quoted Spot instrument before reserving and submitting an order.
- Added persistent cumulative order snapshots with requested size, cumulative filled size, average price, fee and fee currency.
- Fill snapshots reject malformed/non-finite values, fill quantity greater than requested size, missing requested size, and cumulative fill regressions.
- Reconciliation persists execution facts before updating order state; a partial fill remains visible across process restart through SQLite.

## Verification status

- v0.3.0 baseline: 32 tests. v0.4.0: 45 tests passed.
- Python compile check: passed.
- Live public API connectivity: NOT VERIFIED in this environment; a prior DNS lookup to `openapi.okx.com` failed. Retest on the user's local network.
- Private Demo authentication: NOT TESTED; no API credentials were supplied or requested.
- No order was submitted.

## Remaining before any forward test

1. Validate actual instrument and ticker payloads from the user's OKX Demo-compatible region endpoint.
2. Add a durable single-position/exposure lock reconciled against balances and open orders.
3. Add protective Spot exit handling (SL/TP) using documented Demo-compatible conditional/OCO order semantics, including partial-fill sizing and failure recovery.
4. Add terminal order-history lookup and trade-level fills/fee ledger to reconcile executions exactly.
5. Add daily realized/unrealized P&L ledger and circuit breaker.
6. Add fee/slippage-aware backtest, walk-forward, integration/stress/security tests.
7. Only after all gates pass, consider enabling Demo order submission after explicit review; keep the default disabled.
8. Run the required 14-day Demo forward test and evaluate results.

## Safety invariant

This release is not an autonomous trading bot. Order submission remains disabled by default. Never retry a timed-out order until `clOrdId` has been queried and the order state reconciled. Precision normalization does not guarantee an order will be accepted; exchange rules and balances can change between metadata retrieval and order placement.

## v0.5.0 — Single-position lock

- Added a SQLite singleton position lock. Entry reservation and slot acquisition happen in one `BEGIN IMMEDIATE` transaction, so concurrent workers cannot reserve two entry positions.
- Entry orders must be Spot buys. Spot sells must identify the owning entry via `position_client_order_id` and match the locked instrument.
- Ambiguous submissions keep the position lock. A definitively rejected entry can release its lock; a canceled entry releases only when the persisted cumulative fill is zero.
- A filled or partially filled entry keeps the lock. It can only be released through the explicit flat-check method, which requires a fresh open-order check and base-asset balance at or below a configured dust threshold. Release is audit logged.
- This is a local invariant, not yet a complete exchange portfolio reconciler. The caller must obtain the balance and open-order evidence from authenticated Demo reads; no automatic balance-based release is wired yet.

### v0.5.0 verification

- 50 automated tests passed, including single-slot contention, exit-to-position linkage, ambiguous-order lock retention, and strict flat-release conditions.
- Python compile check passed.
- No private API authentication or exchange order was performed.

## v0.6.0 — protective OCO and read-only account reconciliation foundation

- Added Spot OCO exit request support (`POST /api/v5/trade/order-algo`) with `tdMode=cash`, `side=sell`, actual base-asset quantity, `tpOrdPx=-1`, and `slOrdPx=-1` (market execution after trigger). Submission still passes the same immutable Demo header and explicit order gate.
- Added read-only endpoints for pending OCO/conditional algo orders and algo order history, plus guarded algo cancellation.
- Added a pure OCO plan builder that rejects invalid quantities, trigger ordering, symbols, and client IDs.
- Added `DemoAccountReconciler` to collect authenticated Spot balance, open regular orders, and pending OCO orders as one fail-closed snapshot. A flat state requires base available + frozen balance at/below dust and no regular/algo orders for the instrument.
- Important OKX behavior: `conditional` with both TP and SL parameters executes only the SL leg; use `oco` when both legs are required. The adapter uses OCO accordingly.
- This release does not yet orchestrate OCO placement after each partial-fill increment, persist algo-order intent IDs, reconcile triggered OCO child orders, or automatically release the position lock. Do not treat the OCO helper as a complete protection lifecycle.
- No private OKX request, API credential, or order was used during development. `NEBULA_ENABLE_DEMO_ORDERS` remains false by default.

## v0.7.0 — durable OCO protection lifecycle foundation

- Added a persistent `protection_intents` table keyed by entry client order ID, with a unique deterministic `algoClOrdId`, immutable filled quantity/TP/SL, status, exchange algo ID, and audit trail.
- Protection preparation requires a reconciled terminal buy entry (`filled`, `canceled`, or `mmp_canceled`). A still-live/partially-filled entry is not protected by an OCO yet; the remaining entry quantity must first be canceled and its terminal final fill reconciled. This avoids placing an undersized OCO while additional entry fills can still arrive.
- Final filled quantity is checked against current public Spot instrument metadata (`state`, `quoteCcy`, `lotSz`, `minSz`) and is never rounded upward or changed.
- OCO submission intent is persisted before the network call. A timeout or incomplete response becomes `submission_unknown` and cannot be automatically resubmitted.
- Added recovery lookup by `algoClOrdId` using `GET /api/v5/trade/order-algo`. Live/pause/partially_effective map to active; effective maps to triggered; canceled and failure states are retained. A triggered OCO does not release the position lock.
- Demo order gate remains enforced by the exchange adapter. A blocked submission is recorded as failed; no real or Demo order was sent during development.

### v0.7.0 limitations / next required work

- No automatic cancel-entry-reconcile orchestration is wired into the trading loop. The caller must ensure the entry is terminal before preparing protection.
- OCO child order IDs are not yet reconciled against regular order history/trades and fees. A triggered OCO is not proof that the sell has completely filled.
- No automatic OCO size amendment/replacement; immutable one-OCO-per-entry prevents unsafe duplicate replacements.
- The position lock is deliberately retained after OCO trigger/cancel/failure until authenticated balances, open regular orders, algo orders, and child fills jointly prove the position is flat.
- No authenticated API call, Demo order, or live order was performed. No 14-day forward test has begun.

### v0.7.0 verification

- 73 automated tests passed.
- Python compile check passed.
- Includes tests for partial-entry waiting, final partial-fill protection, duplicate prevention, parameter immutability, lot-size/minimum-size validation, restart persistence, active/triggered OCO recovery, ambiguous timeout blocking, and disabled order gate.


## v0.8.0 — entry remainder cancellation and final-fill confirmation

- Added guarded `POST /api/v5/trade/cancel-order` support for a known Spot entry `ordId`; it shares the immutable Demo header and explicit Demo order permission gate.
- Added `cancel_entry_remainder_and_reconcile`: only live/partially-filled buy entries with a persisted exchange order ID can be canceled. The manager always performs a separate order lookup after the cancel request.
- A cancel acknowledgement alone is not terminal proof. If the follow-up lookup still reports `live` or `partially_filled`, the manager returns `cancel_pending`; OCO preparation continues to wait.
- If cancel request errors or times out, the manager still attempts a read-only order lookup to handle fill/cancel races. If lookup fails or the order is missing, the position lock remains held.
- Final cumulative fill, average price, fee and fee currency are persisted before local order status is updated. Partial-fill canceled entries retain the position lock and are eligible for OCO preparation only at their final size. Confirmed zero-fill terminal cancellation can release the unused entry slot.
- 81 automated tests passed; Python compile check passed. Tests cover cancel/lookup race, cancel timeout with filled race, pending cancel acknowledgement, zero-fill release, and safety-gate behavior.
- No private API call, credential, or Demo/live order was used.

### v0.8.0 remaining limitations

- Cancellation orchestration is exposed as a manager method but is not yet integrated into a running strategy loop.
- OCO-triggered child order IDs, regular order terminal history, trade fills and fee ledger are not yet reconciled end-to-end.
- Do not enable Demo order submission or begin the 14-day forward test.


## v0.9.0 — OCO child orders, trade ledger and flat-state finalization

- Added read-only `GET /api/v5/trade/order` by `ordId` and `GET /api/v5/trade/fills-history` for Spot fills.
- Fill-history requests accept bounded pages (max 100) and the adapter supports the OKX `after` billId cursor. The manager caps pagination at 10 pages and fails closed if it cannot establish a complete page sequence.
- OCO `ordIdList` values are stored durably. Each child sell order is looked up independently and its cumulative fill snapshot cannot regress.
- Added idempotent fill storage keyed by instrument + order ID + trade ID. Duplicate trade IDs do not double count; conflicting economic values for an existing key raise an integrity error.
- Preserved signed OKX fee/rebate values. Negative means fee charged; positive means rebate. This corrects the earlier cumulative order snapshot behavior that discarded the sign.
- Added quote cash-flow accounting that includes quote-currency fees/rebates and base-currency fee effects on inventory. Realized quote P&L is emitted only when base inventory is flat and no unsupported fee currency is present.
- Added an explicit finalizer that requires both a complete flat fill ledger and a fresh authenticated account snapshot showing base available+frozen balance within dust tolerance and no open regular/algo orders. SQLite also blocks release while any OCO child remains unknown/live/partially filled.
- The finalizer does not automatically run in a trading loop. It is read-only apart from local SQLite lock release; no order is submitted or canceled.
- 98 automated tests passed locally. Tests include signed fee/rebate handling, duplicate-fill idempotency, conflicting trade ID rejection, pagination, triggered OCO child lookup, accounting, and flat/non-flat lock finalization.
- No private OKX request, Demo order, or live order was sent. Demo order gate remains disabled.

### v0.9.0 remaining limitations

- Runtime orchestration is not complete; entry cancel, OCO placement, child reconciliation and finalizer are separate explicit calls.
- Fill history is bounded to 10 pages (up to 1,000 fills per order per reconciliation); overflow intentionally fails closed and needs a longer-running cursor checkpoint design.
- Fee conversion for third currencies, unrealized P&L, daily loss/circuit breaker, complete backtest/stress testing, region-specific API host validation, and 14-day Demo forward test remain outstanding.
- OCO lifecycle states are monotonic after trigger/cancel/failure; stale or missing lookup results cannot downgrade a terminal state or unlock the position.

## v0.10.0 — persistent circuit breaker and emergency stop

- Added persistent SQLite daily realized-P&L events keyed by position ID, preventing duplicate P&L counting across retries/restarts.
- Daily loss threshold is evaluated in UTC; the lockout resets on the next UTC day. Manual emergency stop persists until explicitly cleared.
- Daily-loss lockout blocks new buy entries while allowing risk-reducing sell orders. Manual emergency stop blocks both regular buy/sell submissions. Existing exchange-side OCO protection is not canceled by the local stop.
- Order manager checks the breaker before reserving a regular order when the client exposes settings. Flat finalization records realized P&L after the fill ledger and independent account balance/open-order checks pass.
- Manual stop clearing requires an explicit confirmation argument; no automatic clear path or CLI command is provided.
- Limitation: daily P&L currently covers only fully closed positions with supported fee currencies. Unrealized P&L, third-currency fee conversion, strategy runtime integration, and end-to-end exchange Demo verification remain incomplete.
- All risk-control tests are local; order gate remains disabled by default and no OKX order was sent.

## v0.11.0 — read-only runtime orchestration

- Added a bounded one-cycle runtime that fetches public Spot instruments/tickers, prioritizes liquid candidates, fetches confirmed candles once per candidate, rejects stale candles, calculates volatility and data depth, applies scanner filters, and evaluates EMA crossover signals only on closed candles.
- Runtime signal events are idempotent by instrument + candle timestamp + action. Runtime cycle outcomes and public-data failures are recorded in the SQLite audit log.
- The runtime reads local position-lock and circuit-breaker status to annotate blocked buy signals. It does not create or call an order manager, read private balances, or submit/cancel orders.
- Added `nebula analyze-once --bar 1H` with `1H`, `4H`, and `1D` choices. The command is explicitly read-only and reports zero order-submission attempts.
- Local verification: tests cover read-only execution, signal idempotency, stale-candle rejection, market-data failure handling, and policy validation. All tests use fakes; no OKX request is made by tests.

### v0.11.0 limitations

- This is a one-shot analysis runtime, not a continuous unattended trading loop. It does not submit entries, manage protective exits, perform private account startup reconciliation, or calculate unrealized P&L/mark-to-market equity.
- Scanner thresholds are conservative code defaults and still require empirical calibration through historical data/backtesting. No asset selection should be interpreted as a recommendation to buy.
- Public OKX connectivity, account-region REST host compatibility, private Demo authentication, and exchange-side behavior remain unverified. `NEBULA_ENABLE_DEMO_ORDERS` stays false.


## v0.12.0 — portfolio and mark-to-market risk engine

- Official OKX docs confirm private `GET /api/v5/account/config` and `GET /api/v5/account/balance`, plus public `GET /api/v5/market/tickers` with bid/ask and timestamp fields. Portfolio reads require API Read permission. The command itself uses only GET requests; Trade permission is not needed, and Withdraw permission is explicitly rejected by the portfolio safety check.
- Added `get_account_config()` and a read-only `portfolio-once` CLI path. It reads account mode/auto-loan state, balances and public tickers; no write endpoint is reachable from this command.
- Account must be Spot mode (`acctLv=1`) and auto-loan disabled. Every nonzero asset must have a fresh direct USDT mark and enough top-of-book bid size for its balance; valuation uses best bid to estimate conservative liquidation value. Missing/stale/crossed/thin-book data and unsupported assets block entry.
- Active position quantity and average cost are reconstructed from linked fill records. The entry order must be terminal; cumulative entry fill size must match the fill ledger; unresolved local exits and local order intents must be absent; exchange open regular orders must be absent; and the exact active OCO must still appear in pending algo orders. The exchange base balance must match the local ledger within a strict tolerance. Third-currency fees and missing fill timestamps are rejected.
- The risk report includes net unrealized P&L after estimated exit fee/slippage, risk to the persisted stop, projected daily P&L including unrealized loss, and optional proposed-entry risk including entry/exit fee and slippage. No proposed entry means no entry permission.
- A schema migration adds `position_client_order_id` to exit intents. Legacy sells are deliberately not guessed or auto-attributed; if an unlinked legacy sell fill exists for the active instrument, portfolio reconstruction fails closed and requires manual reconciliation. Historical closed positions are not reassigned.
- Fee and slippage defaults (10 bps each, configurable with `NEBULA_FEE_BPS` and `NEBULA_SLIPPAGE_BPS`) are provisional assumptions, not an exchange fee quote. Snapshot freshness is controlled by `NEBULA_MARKET_DATA_MAX_AGE_MS`. These values must be empirically calibrated. Zero/unset daily-loss, per-position-risk or position-notional limits block entry; per-position risk cannot exceed the daily loss limit.
- Verification: 152 local tests pass; compile/CLI/ZIP checks are to be performed during final packaging. No OKX private request or order was sent.

### Remaining limitations

- No authenticated Demo read-only integration test has been performed; the account's applicable regional API host is not yet confirmed.
- Only direct USDT-quoted assets are valued; other currencies fail closed.
- The one-shot portfolio snapshot is not integrated into a continuous strategy/execution loop. Since the CLI does not provide a planned entry, it never grants entry permission. Backtesting, walk-forward, stress tests, full reconciliation and the required 14-day Demo forward test remain not started.


## v0.13.0 — offline backtest, walk-forward and cost stress engine

- Added strict OHLCV candle model and CSV importer. Timestamps must be unique and ascending; unfinished rows (`confirm=0`) are excluded; at least 60 confirmed bars are required.
- Added deterministic EMA crossover backtest. Signals use completed candles and entries occur only at the next candle open; each trade records signal and execution timestamps to make look-ahead checks auditable.
- Added one-position Spot-long simulation with fixed SL/TP established from actual simulated entry price, risk-budget sizing, notional cap, fee and adverse slippage on both sides.
- Same-bar SL/TP collision is resolved stop-first. Gap-through-stop execution is conservatively modeled at the worse open; target exits are charged adverse slippage.
- Equity is marked conservatively using candle low and estimated liquidation costs. Daily loss halt (UTC) and maximum drawdown halt block new entries while any existing position continues to be managed.
- Added chronological walk-forward folds. Each out-of-sample fold starts flat and uses only earlier bars for indicator warm-up; no parameter optimization is performed.
- Added public `GET /api/v5/market/history-candles` read method with bounded page size and timestamp cursor validation. The adapter method is read-only and does not access private endpoints.
- CLI: `nebula backtest --data candles.csv`, `--walk-forward --folds N`, or `--stress`.
- Local verification: 167 tests passed. No API credentials, private account calls, Demo orders or live orders were used.

### v0.13.0 limitations

- Backtest uses OHLCV bars, not tick/order-book replay. Intrabar order of price extremes is unknown; stop-first is deliberately conservative but does not fully model exchange execution.
- Exchange tick-size/lot-size rounding, minimum order size, partial fills, market impact and liquidity constraints are not simulated; these need a separate execution-model extension.
- Default fee/slippage assumptions are placeholders. Results are not predictive and require calibration against the user's actual Demo fee tier and observed spreads/slippage.
- No parameter optimization is included; avoid tuning against the same period used for reporting. No multi-asset portfolio correlation, partial fills, queue position, funding or taxes are modeled.
- Historical API paging is exposed as a bounded read method, but bulk download orchestration and data-quality provenance checks are not yet implemented.
- This release does not enable order submission or qualify the bot for the 14-day forward test.


### v0.13.0 cost stress extension

- Added four repeatable fee/slippage scenarios: configured base, 1.5x moderate costs, 2x slippage, and 2x severe costs (bounded to supported limits).
- Cost stress holds strategy parameters and OHLCV data fixed; it is not an optimizer and does not select a supposedly best parameter set.
- CLI: `nebula backtest --data candles.csv --stress`. Walk-forward and stress modes are mutually exclusive per invocation.


## v0.14.0 — multi-asset portfolio backtest

- Added synchronized multi-symbol OHLCV simulation with one shared cash balance and one global position slot. Inputs must have identical ascending candle timestamps and bar interval; mismatches fail closed.
- At each eligible candle open, the engine evaluates bullish EMA crossovers from the prior completed candle. If multiple symbols qualify, it selects the highest positive EMA separation (`fast_ema / slow_ema - 1`); ties are deterministic by symbol name. This ranking score is a transparent heuristic, not a demonstrated predictive advantage.
- Entry sizing, SL/TP, fees, slippage, daily-loss halt and max-drawdown halt are calculated against the shared portfolio equity. A position closed intrabar cannot be replaced retroactively at the same candle open; a new position can be opened only on a subsequent bar. A position closed at the open by a prior bearish signal may release the slot for a different qualifying asset at that open.
- Added portfolio chronological walk-forward and four-scenario portfolio fee/slippage stress reports. Each OOS fold starts flat, retaining earlier candles only for EMA warm-up.
- CLI: `nebula portfolio-backtest --asset BTC-USDT=btc.csv --asset ETH-USDT=eth.csv`; add `--walk-forward --folds 3` or `--stress` for the respective analysis.
- Verification: 176 local tests pass, including deterministic selection, synchronized-grid rejection, one-position enforcement, no retroactive same-open re-entry, portfolio walk-forward and cost stress.
- No exchange requests, private credentials, Demo orders or live orders were used. `orders_submitted` is always zero in this offline engine.

### v0.14.0 limitations

- The engine requires identical timestamp grids and bar intervals for all assets; it does not silently fill missing bars.
- EMA separation is a simple ranking heuristic and has not been calibrated or proven profitable. No correlation-aware ranking, portfolio optimization, or adaptive parameter tuning is performed.
- Tick/lot rounding, minimum notional, partial fills, market impact, order-book depth, liquidity capacity and cross-asset correlation are not simulated.
- No real market historical dataset has been loaded or validated in this environment. Synthetic/local tests verify mechanics only. This does not qualify NEBULA for Demo forward testing.


## v0.15.0 — historical data collection and quality audit

- Added public-only `download-history` CLI for USDT Spot candles using `GET /api/v5/market/history-candles`. The collector pages backward with the `after` timestamp cursor, requests 100 rows per page, rate-limits below the documented 20 requests/2 seconds, deduplicates identical timestamps, rejects conflicting OHLC duplicates, sorts ascending, and excludes unconfirmed candles.
- Official OKX documentation was rechecked: the history endpoint allows at most 100 rows per request, rate limit 20 requests per 2 seconds, and uses `confirm=1` for completed candles. The documentation's history response example describes an 8-field row while the regular candles example describes 9 fields; the parser accepts both documented shapes and selects the final field as `confirm`.
- Added CSV plus JSON manifest with source, symbol, bar, fetch time, timestamp range, page count, incomplete-row count and CSV SHA-256. Existing output/manifest files are protected unless `--overwrite` is explicitly passed.
- Added `audit-data` to validate OHLCV, strict chronology, interval consistency and report estimated gaps without filling/interpolating data.
- Verification: 184 local tests pass, including mocked page navigation, duplicate/conflict handling, unconfirmed rows, manifest hash and gap reporting. Compileall and CLI help pass.
- Public endpoint connectivity was attempted from the build environment but DNS resolution for `openapi.okx.com` failed. Therefore no actual historical dataset was downloaded, and no market-performance result is claimed. This is an environment connectivity limitation, not evidence that OKX is unavailable from the user's network.
- No credentials, private endpoints, order endpoints or Demo/live orders were used.

### v0.15.0 remaining limitations

- User must run `download-history` on a network that can resolve/reach the OKX API host and review the returned manifest.
- The audit reports gaps but does not know listing histories or exchange maintenance windows; no gap filling is performed.
- Backtest CSV supports one symbol at a time; multi-asset backtest requires aligned CSVs on the exact same timestamp grid.
- Strategy parameter selection, survivorship-free historical universe construction, correlation diagnostics, spread/order-book replay and realistic partial-fill modeling remain future work. No parameter optimization is performed, to avoid accidental overfitting.
- This stage does not enable Demo order submission or qualify the bot for the 14-day forward test.


## v0.16.7 — verified Spot fill-history pagination

- Hardened trade-level fill ingestion for GET /api/v5/trade/fills-history. Each page must be a list within the requested limit; every record must identify the exact requested order and instrument, be SPOT, use USDT quote currency, have a buy/sell side consistent with subtype 1/2, and contain numeric bill/trade IDs, timestamps, positive size/price, and finite signed fee data.
- Duplicate bill IDs, repeated/non-advancing cursors, pages that do not move strictly toward older bill IDs, malformed records, wrong order/instrument, unsupported product/quote currency, and pagination overflow fail closed.
- Pagination advances with the minimum bill ID of each full page. OKX documents that after returns records earlier than the supplied bill ID; choosing the minimum avoids relying on the order of rows inside a page.
- A page is fully validated before any row from that page is written to the idempotent fill ledger. Earlier valid pages may already be persisted if a later page fails, but the caller receives an error and cannot treat the sequence as complete; a subsequent retry safely deduplicates those earlier fills.
- Added fault-injection coverage for multi-page cursor progression, duplicate/non-older IDs, wrong order/instrument/type, invalid side/subtype, malformed timestamps/fees, invalid bounds, and overflow.
- GitHub Actions run #341: 446 tests passed in 2.25 seconds; compile, explicit safety settings, and fail-closed defaults all passed. Run: https://github.com/agussuliono/nebula-trading-bot/actions/runs/44897716212
- Official API reference: https://app.okx.com/docs-v5/en/ (GET /api/v5/trade/fills-history; last 3 months, maximum 100 records per page, after/before bill ID cursors, signed fee/rebate semantics).
- No API credentials, private OKX account connection, Demo/live order, or exchange write was used.

### v0.16.7 remaining limitations

- The three-month endpoint retention is not a substitute for durable local backups. If an active position's fill history falls outside exchange retention or cannot be retrieved, reconciliation must remain blocked.
- The CI fixtures are synthetic. They verify local validation and pagination logic, not actual response ordering/behavior on an OKX Demo account.
- The 14-day forward test has not begun. No strategy profitability claim is made.


## v0.16.11 — public API diagnostics and bounded resilience (2026-11-21)

### Implemented in the review branch

- Added Settings.from_public_env(); check-demo-public and analyze-once no longer call Settings.from_env() and do not read API key, secret, passphrase, or order-gate environment variables.
- HTTP failures retain sanitized status, content type and numeric API error code when available. Non-JSON 403 responses are reported as HTTP failures without exposing body, headers, URL or credentials.
- API error messages redact any configured key, secret and passphrase before they can reach diagnostics.
- Public unauthenticated GET only: at most 3 attempts for transient connection errors, HTTP 429 and HTTP 5xx, with bounded 250/500 ms backoff.
- Public market-data circuit breaker opens after 3 exhausted transient calls, cools down for 15 seconds, then permits one half-open probe.
- HTTP 403 and other 4xx, redirects, invalid JSON and exchange-level API errors are not retried. Private GET and all POST operations are single-attempt. Order submission remains non-retryable.
- The fixed Demo REST host and x-simulated-trading: 1 header remain mandatory; redirects remain disabled.

### External connectivity status — unresolved

- Prior public endpoint tests from outside the sandbox received HTTP 403 for both Spot instruments and tickers. The responses were not retained, so the rejecting layer (OKX, CDN/WAF, or an upstream intermediary) cannot be identified.
- DNS resolution succeeded outside the sandbox; earlier sandbox DNS failures were consistent with sandbox network restrictions.
- This code change improves diagnostics and protects against transient failures; it does not bypass or claim to resolve HTTP 403.
- No additional OKX request, account authentication, API credential read, or exchange write was performed as part of this patch.

### Verification boundary

- Automated CI uses mocked HTTP responses and verifies the local retry, circuit-breaker, diagnostics, host and credential-isolation contracts.
- A passing CI run cannot establish that OKX will permit requests from the user's network. Real public connectivity and authenticated Demo read-only verification remain separate gates.
- No Demo order, live order, cancel, transfer, withdrawal, or money movement is enabled by this change.
