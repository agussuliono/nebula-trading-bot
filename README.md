# NEBULA Trading Bot V1 — Demo-only foundation

## v0.16.13 patch note

- Startup balance validation now matches the reconciliation standard: `availBal`/`frozenBal` are parsed strictly without truthiness fallback, required fields must be present, and duplicate currencies are rejected before any entry permit can be issued.

## v0.16.11 patch note

- Balance reconciliation is now strictly fail-closed: `availBal` and `frozenBal` are parsed without truthiness fallback, every balance row must be a well-formed object with a unique currency and parseable required amounts, and an invalid snapshot can never prove a position flat or release the position lock.

## v0.16.10 patch note

- Liability validation is now fail-closed in the Demo verifier, portfolio entry gate, and startup recovery: a missing, null, malformed, negative, or non-finite `liab` balance field blocks instead of being treated as zero. Added negative tests for each case in all three paths.

## v0.16.9 patch note

- OKX Spot mode now ships with the spot-borrowing feature enabled at account level (`enableSpotBorrow=true`) and offers no switch to disable it, so the Demo verifier, portfolio entry gate, and startup recovery gate no longer block on that feature flag alone. Actual borrowing state still fails closed: auto-loan must be off, spot auto-repay must be off, and total balance liabilities (`liab`) must be zero; an unknown flag state still blocks. Orders remain cash-mode only and are validated against available balance; no borrow endpoint is ever called.

## v0.16.8 patch note

- Shadow-only `orchestrate-once` uses public settings and does not read API credentials unless `--execute-demo` is explicitly supplied.
- The scanner now counts only confirmed candles with finite positive OHLCV toward its minimum data-history requirement; zero-volume and malformed-volume bars cannot inflate data completeness.
- Added regression coverage for data-quality filtering and credential-free shadow orchestration.


## v0.16.7 patch note

- Public-only CLI paths (check-demo-public and analyze-once) use a dedicated settings loader that never reads API key, secret, passphrase, or order-gate environment variables.
- HTTP errors now retain safe diagnostics (HTTP status, sanitized API code and content type). Non-JSON responses such as HTML 403 pages are classified without copying their body, arbitrary headers, or request URL; only allowlisted Server/Via/CF-Ray metadata is retained, with IP literals redacted.
- Configured API key, secret and passphrase values are redacted from exchange-provided error messages.
- Only unauthenticated, idempotent public GET requests retry transient connection failures, HTTP 429, and HTTP 5xx: at most 3 attempts with 250/500 ms backoff. HTTP 403, other 4xx, redirects, invalid JSON and API-level errors are not retried.
- A public-market circuit breaker opens after 3 exhausted transient failures, pauses requests for 15 seconds, and allows one half-open probe. It does not wrap private reads or order writes.
- Private GETs and all POST operations remain single-attempt. Order submissions are never retried automatically; ambiguous outcomes must be reconciled.
- The Demo REST host is selected from a closed allowlist by `NEBULA_OKX_REGION` (`global`, `us_au`, or `eea`); raw URLs and production-mode switches are not accepted. Every request keeps `x-simulated-trading: 1`, and redirects remain disabled.
- Added regression tests for credential isolation/redaction, 403 non-JSON diagnostics, bounded retries, circuit-breaker recovery, and no retries for private reads/writes.
- This patch does not resolve the previously observed HTTP 403 from the user's network. The rejecting layer is still unknown because the original response headers/body were not retained. No live or Demo account was accessed.

**Version:** 0.16.13  
**Status:** Public API diagnostics and bounded read resilience included. Strategy validation failed on the supplied historical sample; not ready for forward testing or autonomous trading.

## v0.16.5 patch note

- All successful `nebula` CLI command results are now printed normally and best-effort copied as the exact same text to the desktop clipboard.
- Clipboard support is dependency-free and detects `wl-copy` / `xclip` / `xsel` on Linux, `pbcopy` on macOS, and `clip.exe` on Windows.
- Missing clipboard utilities or clipboard errors do not interrupt commands. Disable copying with `NEBULA_AUTO_COPY_RESULT=false`.
- Help text and error messages are not copied; only successful command result output is copied.
- No source code is copied. No exchange, credential, or order behavior changed.

## v0.16.4 patch note

- Replaced the scanner's simple high-low range estimate with mean true range as a percentage of close, so gaps relative to the prior valid close are included.
- Excluded confirmed zero-volume candles from the volatility estimate; these bars no longer dilute measured volatility.
- Made scanner ranking explicitly use all five selector dimensions: quote turnover, spread, volatility proximity to the configured band midpoint, listing age, and candle-history count. Liquidity and spread remain the first two priorities.
- Added regression tests for ranking tie-breakers, true-range gap handling, zero-volume exclusion, unconfirmed-candle exclusion, and invalid windows.
- No strategy signal, order, credential, or exchange-write behavior changed. This is not evidence of improved trading returns.

## v0.16.3 patch note

- Corrected per-unit stop-risk sizing to use exact net entry cash outlay minus net stop liquidation proceeds. Entry slippage is no longer double-counted after the entry price has already been adjusted. Applied to single-symbol and portfolio backtests.
- Aligned the reusable risk-sizing helper with the same exact cash-loss formula and included entry fees in the equity affordability cap.\n- Added regression coverage for exact fee/slippage-aware risk sizing.
- No strategy parameters or signals changed.

## v0.16.2 patch note

- Fixed a daily-loss circuit-breaker edge case where a signal formed on a halted candle could be carried across UTC midnight and executed after the daily reset.
- Applied the same protection to the multi-symbol portfolio simulator.
- Final liquidation is included in the reported maximum drawdown calculation.
- This is a simulator correctness/safety patch; it does not claim improved strategy profitability.

## v0.16.1 patch note

- Fixed `nebula --help` crashing because argparse interpreted the literal `%` in the sensitivity help text as a formatting directive. The help now spells out “percent”.
- Added a regression test for the top-level CLI help output.
- No strategy, exchange, credential, or order-execution behavior changed in this patch.

## v0.15.1 patch note

- Corrected a time-dependent test fixture: the order-manager circuit-breaker test now freezes the UTC trading day instead of assuming 2026-11-19 is the current day.
- Production circuit-breaker behavior is unchanged: daily realized-loss lockout resets on the next UTC day; the manual emergency stop persists.

## Safety model

## Local state and single-instance boundary

- Set `NEBULA_DB_PATH` to one stable absolute path when running NEBULA from different working directories. Every process for the same Demo account must use the same state database; a different database cannot see the same position lock or order ledger.
- SQLite database, WAL, SHM and journal files are restricted to owner-only permissions. Symlinked state paths are refused.
- Paginated private reads use a shared Linux file lock under the same OS user. This coordinates processes on one Linux host only; it does not coordinate another laptop, Codespace, VM, container with a separate filesystem, or another OS user. Do not run concurrent NEBULA instances against the same OKX Demo User ID on separate hosts.
- A passing offline test suite is not evidence that the account can be accessed or that a strategy is suitable. The previously evaluated EMA strategy was rejected; Demo order permission must remain disabled until independent prospective strategy evidence is sufficient.


- REST hosts are selected only from the closed Demo allowlist for the account-registration region; no arbitrary URL or live-trading host can be configured.
- Every request carries `x-simulated-trading: 1`.
- Order submission requires `NEBULA_ENABLE_DEMO_ORDERS=true`, complete Demo credentials, finite positive risk limits, `max_risk <= max_daily_loss`, and inactive kill switch.
- No withdrawal endpoint or withdrawal permission is implemented or needed.
- Never paste API secrets into chat. Store Demo credentials only in local environment variables.
- A timeout after sending an order is ambiguous; never blindly retry. Reconcile by `clOrdId` first.
- No automatic retry is implemented for order submission.

## Modules

- `config.py`: fail-closed safety settings.
- `exchange.py`: OKX V5 Demo REST adapter; public Spot market data, private balance/order reads, guarded Spot limit order/cancel and OCO operations, and Spot instrument lookup.
- `instruments.py`: Decimal-based `tickSz`, `lotSz`, `minSz` validation; buy prices round down, sell prices round up, and size rounds down.
- `scanner.py`: USDT Spot universe normalization, spread, quote turnover, listing-age and true-range volatility primitives; deterministic ranking uses all configured selection dimensions.
- `strategy.py`: EMA crossover primitive using confirmed/closed candles only.
- `risk.py`: long Spot sizing with fees/slippage and equity/notional caps.
- `state.py`: durable SQLite intent store, signed fee snapshots, OCO child-order records, idempotent trade-level fill ledger, explicit sell-to-entry linkage, position lock and audit events.
- `accounting.py`: conservative Spot quote-currency cash-flow and realized P&L calculation; only marks accounting complete when base inventory is flat and fees are in base/quote currency.
- `portfolio.py`: read-only USDT portfolio valuation at best bid, fill-ledger average-cost basis, unrealized P&L, fee/slippage-aware stop risk, daily realized-plus-unrealized loss checks, and fail-closed entry gating. Fee, slippage and freshness assumptions are environment-configurable.
- `risk_controls.py`: persistent UTC daily realized-loss circuit breaker and manual emergency stop; blocks entries after the daily threshold and blocks all regular orders during manual stop.
- `diagnostics.py`: trade-level metrics, per-symbol and exit-reason breakdowns, closed-candle regime labels, equal-weight buy-and-hold benchmark, exploratory EMA sensitivity with an untouched final holdout, and CSV trade export.
- `backtest.py`: deterministic offline OHLCV backtest, synchronized multi-asset portfolio simulation with one shared cash balance and one global position slot, chronological walk-forward, and fee/slippage stress evaluation; next-open entries, closed-candle EMA signals, fixed pre-entry SL/TP, single Spot-long position, fee/slippage, daily-loss and drawdown entry halts.
- `runtime.py`: one-cycle read-only orchestration of public Spot universe scanning, candle freshness checks, liquidity/spread/volatility/listing-age filters, closed-candle EMA signals, idempotent signal logging, and risk/position status. It has no order-submission path.
- `orders.py`: Demo order lifecycle coordinator, duplicate-attempt blocking, precision normalization, conservative reconciliation, and one-position/linked-exit enforcement.
- `protection.py` / `protection_manager.py`: validated OCO plan, guarded entry-remainder cancellation, terminal-fill reconciliation, durable idempotency, OCO child-order discovery, paginated fill ingestion and explicit flat-state finalization.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e '.[test]'
cp .env.example .env  # edit locally; never commit .env
pytest -q
nebula check-demo-public
nebula analyze-once --bar 1H  # public-data-only; no orders sent
nebula portfolio-once       # private read-only Demo account snapshot; no orders sent
nebula backtest --data candles.csv --walk-forward --folds 3
nebula backtest --data candles.csv --stress
```

The public connectivity and analysis commands send no orders and do not require credentials. `portfolio-once` requires an OKX Demo API key with **Read** permission; the command itself uses only GET requests. Trade permission is not needed for this command, and Withdraw permission is rejected by the portfolio safety check. Run it only from a trusted local machine. It reads account configuration and balances plus public tickers, and does not send orders.

## Portfolio diagnostics (v0.16.0+)

```bash
nebula diagnose-portfolio \
  --asset BTC-USDT=../data/BTC-USDT-1H.csv \
  --asset ETH-USDT=../data/ETH-USDT-1H.csv \
  --asset SOL-USDT=../data/SOL-USDT-1H.csv \
  --trades-output reports/trades.csv \
  --sensitivity
```

The diagnostics command is offline and read-only. Sensitivity output is exploratory only; it does not select a winning parameter set or evaluate the reserved final holdout.

## Backtest CSV format (v0.14.0)

Provide an ascending-time CSV with a header and columns `ts,open,high,low,close`; `volume` and `confirm` are optional. `ts` is the candle opening timestamp in Unix milliseconds. `confirm` accepts `1`/`true` for completed candles and `0`/`false` for unfinished candles. At least 60 confirmed, unique, ascending candles are required. Do not reverse OKX's newest-first API response without sorting it into ascending time.

Example:

```csv
ts,open,high,low,close,volume,confirm
1700000000000,100,102,99,101,1200,1
1700003600000,101,103,100,102,1350,1
```

The backtest uses only confirmed bars. A crossover at bar close can enter only at the next bar open. A same-bar SL/TP collision resolves to SL first; market exits incur adverse slippage and fees on both sides. Open inventory is marked at candle low net of estimated exit costs for conservative drawdown. It models one long Spot position, no leverage, no short selling, and no exchange orders. Walk-forward folds are chronological and each test fold starts flat while retaining prior candles only for EMA warm-up. Exchange tick/lot rounding, minimum order size, partial fills and market impact are not simulated.

Example parameters (defaults are placeholders, not validated OKX account fees):

```bash
nebula backtest --data candles.csv --walk-forward --folds 3 \
  --initial-equity 1000 --risk-pct 0.01 --max-position-pct 0.25 \
  --stop-pct 0.02 --target-pct 0.04 --fee-bps 10 --slippage-bps 10

# Re-run the same strategy under progressively worse fee/slippage assumptions
nebula backtest --data candles.csv --stress
```

## Multi-asset portfolio backtest (v0.14.0)

Use `nebula portfolio-backtest --asset SYMBOL=FILE.csv` at least twice. Every asset file must contain the same ascending timestamps and bar interval; the engine rejects mismatched grids rather than silently aligning incomplete histories. It ranks bullish EMA crosses from the previous completed candle by positive EMA separation (`fast_ema / slow_ema - 1`), with alphabetical tie-breaking. It uses one shared cash balance and a single portfolio-wide position slot. A position closed intrabar cannot be replaced retroactively at that same candle open; re-entry can occur only on a later bar. Portfolio walk-forward starts each out-of-sample fold flat while retaining earlier bars only for EMA warm-up. Portfolio stress repeats the same deterministic selection under higher fee/slippage assumptions. No order is sent.

```bash
nebula portfolio-backtest --asset BTC-USDT=btc.csv --asset ETH-USDT=eth.csv --asset SOL-USDT=sol.csv
nebula portfolio-backtest --asset BTC-USDT=btc.csv --asset ETH-USDT=eth.csv --walk-forward --folds 3
nebula portfolio-backtest --asset BTC-USDT=btc.csv --asset ETH-USDT=eth.csv --stress
```

The ranking score is a deterministic implementation choice, not a proven predictive edge. All symbols must have the same timeframe, timestamps and timezone convention. This version still does not model cross-asset correlation, exchange lot/tick rounding, minimum notional, partial fills, market impact, or order-book depth.

## Test status for v0.16.13

- 603 automated tests passed in the local build environment (2026-11-26, pytest 8.4.2, Python 3.12).
- Tests cover fee/slippage impact, next-open execution, same-bar stop priority, risk sizing, daily-loss/drawdown halts, chronological single-asset and portfolio walk-forward splits, multi-asset deterministic selection, global one-position enforcement, synchronized timestamps, portfolio cost-stress scenarios, CSV validation and public history-candle request construction.
- Tests use local synthetic data and mocked HTTP; they do not establish OKX connectivity or exchange execution behavior.
- No API credentials were used and no Demo or live orders were sent.

## Remaining before forward testing

The current release adds offline fee/slippage-aware single-asset and multi-asset portfolio backtesting, portfolio-wide one-position enforcement, and chronological walk-forward evaluation. Remaining work includes broader stress testing, independent prospective strategy evidence, confirmation of the account-registration region, authenticated private Demo read-only integration, full execution reconciliation, security review, and the required 14-day Demo forward run. The backtest does not authorize or enable order execution.

See `docs/INTEGRATION_STATUS.md` and `docs/SPEC_AUDIT.md`.

### Protective exits and account reconciliation (v0.6.0–v0.7.0)

NEBULA includes a Spot OCO exit adapter and read-only account snapshot helper. The durable protection manager waits for the entry order to become terminal, validates final cumulative filled size against current Spot lot/minimum rules, records a unique OCO intent before submission, and can recover its state through OKX algo-order details using the persisted `algoClOrdId`. A triggered OCO does not release the position lock.

This remains a lifecycle foundation, not an unattended protection system. Entry cancellation, OCO submission, child reconciliation and final flat-state verification are explicit manager operations and are not wired into a continuous strategy loop. Keep Demo order submission disabled until the full runtime, account-domain connectivity and safety gates have been verified.


### Entry cancellation and final-fill reconciliation (v0.8.0)

The protection manager can now cancel a known live/partially-filled buy remainder, then independently query the order by its durable client ID. A successful cancel acknowledgement is not treated as proof of cancellation: protection remains blocked until the exchange reports a terminal `filled` or `canceled` state and the final cumulative fill is persisted. A cancel timeout/error is followed by a read-only lookup to detect a fill/cancel race; if lookup fails, the position lock remains held. A confirmed terminal zero-fill cancellation may release the empty entry slot; a partial fill retains the position lock and can proceed to OCO preparation only at the final filled quantity.

This is an explicit manager operation, not yet wired into a continuous trading loop. Cancellation and OCO submission both remain behind the disabled-by-default Demo order gate. No OKX private request or order was sent during this build.


### OCO child reconciliation and fill accounting (v0.9.0)

The adapter now reads regular order details by `ordId` and OKX Spot fill history by `ordId`, with bounded pagination through `billId`. When an OCO reports `effective`, its `ordIdList` is persisted and each spawned child sell is independently inspected. Fill rows are deduplicated by instrument/order/trade ID; the signed fee/rebate amount and fee currency are preserved. Quote-currency cash flow and realized P&L are reported only when the fill ledger proves base inventory is exactly flat and all fees are denominated in base or quote currency.

Lock release is a separate explicit operation requiring both complete flat fill-ledger accounting and a fresh account snapshot with base available+frozen balance under the dust limit and no open regular/algo orders. Child orders in unknown/live/partially-filled states block release. The manager never interprets an OCO trigger as proof of a completed sell.

Limitations at v0.9.0: this did not provide unrealized P&L, fee conversion for third currencies, daily circuit breaker, or continuous runtime orchestration. It does not perform authenticated exchange calls during tests.

## v0.10.0 — persistent daily-loss circuit breaker

- Added a SQLite-backed daily realized-P&L ledger, keyed by position ID so retries after restart do not double count a closed trade.
- Daily loss is measured in UTC and trips at `daily_realized_pnl_quote <= -NEBULA_MAX_DAILY_LOSS_QUOTE`. A new UTC day starts a new daily counter; manual emergency stop persists until explicitly cleared.
- New entries are blocked after the daily loss threshold. A daily-loss halt does not block risk-reducing Spot sells. Manual emergency stop blocks regular buy/sell order submissions. The exchange-level `NEBULA_KILL_SWITCH` remains the stronger write kill switch and also blocks protective OCO writes.
- `DemoOrderManager` checks the persistent breaker before reserving/submitting orders when the client exposes Settings. `DemoProtectionManager` records realized P&L only after fill-ledger flatness and a fresh account snapshot confirm flat; event writes are idempotent.
- `CircuitBreaker.clear_emergency_stop(explicit_confirmation=True)` requires an explicit confirmation argument and is not exposed as an automatic reset command.
- Daily P&L currently includes only fully closed positions whose fees are supported by quote/base currency accounting. Unrealized P&L and third-currency fee conversion are not included; this is a known limitation, so it is not a complete mark-to-market risk engine.

### v0.10.0 verification

- 108 automated tests passed locally (`pytest -q`).
- Tests cover threshold trip, idempotency, conflicting P&L replay, UTC rollover, persistent manual stop, explicit clear confirmation, buy blocking, sell behavior after daily halt, and finalizer P&L integration.
- All tests use local SQLite and fake clients. No authenticated OKX API call or exchange order is made.


## v0.11.0 — read-only runtime orchestration

- Added `ReadOnlyRuntime` to coordinate a bounded one-shot public market-data cycle: fetch live USDT Spot instruments and tickers, prioritize by liquidity/spread, fetch candles for a bounded candidate set, reject stale/unconfirmed candle data, enrich volatility and data quality, apply the configured scanner filters, and calculate EMA crossover signals only from closed candles.
- Signal observations are persisted idempotently by instrument, candle timestamp, and action. Runtime cycles and market-data failures are written to the existing audit log.
- Existing position-lock and daily circuit-breaker state are surfaced as entry-signal blockers; the runtime does not place orders.
- Added CLI command `nebula analyze-once --bar 1H` (also supports `4H` and `1D`). It uses public market data only, does not read private balances, does not instantiate an order manager, and reports `order_submission_attempts: 0`.
- Runtime thresholds and bounds are validated; candle fetches are bounded to the most liquid symbols and cached within a cycle to avoid duplicate API calls.

### v0.11.0 verification and limitations

- Local tests cover read-only behavior, idempotent signals, stale-candle rejection, public-data failure handling, and runtime policy validation.
- This is a one-shot analysis runtime, not an unattended trading loop. It does not submit entries, manage exits, calculate mark-to-market equity/unrealized P&L, or run continuously. It does not yet perform private account startup reconciliation.
- Tests use local fakes. Public OKX connectivity has not been demonstrated in this environment; no private API request or exchange order was sent. Keep `NEBULA_ENABLE_DEMO_ORDERS=false`.


## v0.12.0 — read-only portfolio and mark-to-market risk

- Added read-only `GET /api/v5/account/config` and `GET /api/v5/account/balance` adapter methods. The CLI command `nebula portfolio-once` combines these private reads with public Spot tickers; it does not instantiate an order manager or call any write endpoint.
- Added conservative USDT valuation using each asset's direct `ASSET-USDT` best bid, with timestamp, crossed-market, duplicate-row, missing-price and top-of-book depth checks. Unsupported or unpriced assets block entry instead of being assigned an assumed conversion rate.
- Added average-cost inventory reconstruction from the durable fill ledger. Base- and quote-currency fees are included with OKX's signed fee convention; third-currency fees and missing fill timestamps fail closed.
- Added exchange-balance versus local-ledger reconciliation for the active locked position, including terminal entry snapshot versus trade-fill totals, unresolved local exits/order intents, exchange open regular orders, and confirmation that the exact protective OCO remains pending. New exit fills are linked to their originating entry intent. Legacy sell fills are not guessed or auto-attributed; they fail closed and require manual reconciliation if found on the active instrument.
- Added unrealized gross/net P&L, estimated exit fee/slippage, stop-loss risk, total portfolio risk, and daily realized-plus-unrealized loss checks. A proposed entry must include instrument, size, entry, stop and target; estimated entry/exit fee and slippage are included before the entry gate can pass.
- Default fee and slippage assumptions are 10 bps each (`NEBULA_FEE_BPS`, `NEBULA_SLIPPAGE_BPS`) and are placeholders for conservative simulation, not a verified OKX fee tier. Snapshot age is configurable via `NEBULA_MARKET_DATA_MAX_AGE_MS`. Calibrate all three before any forward test. Risk limits of zero are treated as unconfigured and block entry.
- Local verification: 152 tests pass. Tests use local fakes/SQLite; no authenticated OKX call, API key, Demo order or live order was used. `NEBULA_ENABLE_DEMO_ORDERS` remains false.

### v0.12.0 limitations

- Private Demo read-only connectivity has not been tested. Use a Demo API key with Read permission only for `portfolio-once`; never grant Withdraw.
- Portfolio valuation currently supports direct USDT-quoted Spot holdings only. Other quote currencies and third-currency fees block the report rather than being guessed.
- The CLI provides a snapshot audit; it is not a continuous runtime and does not itself construct a strategy entry plan. Because it supplies no proposed entry, `portfolio-once` always withholds entry permission. The current public runtime remains read-only.
- The 14-day forward test is not authorized or ready. Backtest, walk-forward, stress, region/host compatibility, full startup reconciliation and security review remain outstanding.

## v0.15.0 — historical data collection and quality gate

NEBULA can download public historical USDT Spot candles without API credentials or private endpoints:

```bash
nebula download-history --symbol BTC-USDT --bar 1H --candles 5000 --output data/BTC-USDT-1H.csv
# add --overwrite only when you intentionally want to replace the CSV and manifest
nebula audit-data --data data/BTC-USDT-1H.csv --bar 1H
nebula backtest --data data/BTC-USDT-1H.csv --walk-forward --folds 3
```

The collector uses OKX V5 `GET /api/v5/market/history-candles`, paginates backward with `after`, requests 100 rows per page, deduplicates timestamps, sorts oldest-to-newest, excludes unconfirmed candles, and writes a SHA-256 manifest beside the CSV. It only supports normalized USDT Spot symbols and 1m/3m/5m/15m/30m/1H/2H/4H/1D/1Dutc bars. The collector is public-data-only; it does not authenticate or submit orders. Data collection is user-run and requires internet access on the machine running NEBULA.

`audit-data` validates OHLC ranges, positive prices, chronology, duplicate timestamps, interval consistency and estimates missing bars. It reports gaps but never fills or interpolates them. Investigate gaps against listing date and exchange maintenance before backtesting. OKX returns candle rows newest-first; the collector normalizes them into ascending order. The history endpoint's documented maximum is 100 rows per request. The API `confirm` field must be `1` for a completed candle; unfinished rows are discarded. The collector accepts both documented 8-field and 9-field candle arrays, because the official history-candle response example describes 8 fields while the candlestick endpoint describes 9. Duplicate timestamps with conflicting OHLC data are rejected. Existing output files are protected unless `--overwrite` is explicitly supplied.

**Research discipline:** a successful download is not evidence of strategy profitability. Run identical settings on multiple symbols, retain chronological out-of-sample folds, compare transaction-cost stress, and record all parameter choices before looking at test results. Do not optimize repeatedly on the final holdout. Static current-symbol lists can introduce survivorship bias; record delisted assets or disclose that limitation. This release does not automatically select the best parameter set.

### v0.15.0 verification scope

Automated tests use fake OKX responses and synthetic candles. They verify pagination, timestamp deduplication/order, incomplete-candle exclusion, manifest hash, data-gap reporting, malformed responses and zero order submissions. No authenticated API request or order is sent by these tests.


## Test status for v0.15.0

- 184 automated tests passed from the project source tree.
- Added tests for history pagination, 8/9-field OKX candle response compatibility, incomplete candle filtering, duplicate conflict detection, output overwrite protection, manifest SHA-256 and missing-bar reporting.
- `compileall`, CLI help, offline dataset audit and clean-extraction tests are part of the release checks.
- Public OKX connectivity could not be tested from the build environment because DNS resolution for `openapi.okx.com` failed. No private API credentials or endpoints were used, and no orders were submitted.


## Strategy diagnostics (v0.16.0)

Run an offline trade-level report and export the ledger:

```bash
nebula diagnose-portfolio \
  --asset BTC-USDT=/path/to/BTC-USDT-1H.csv \
  --asset ETH-USDT=/path/to/ETH-USDT-1H.csv \
  --asset SOL-USDT=/path/to/SOL-USDT-1H.csv \
  --trades-output reports/trades.csv \
  --sensitivity
```

See `docs/STRATEGY_DIAGNOSTICS.md` for metric definitions, regime labeling, benchmark assumptions, and the untouched-holdout safeguard. This command is offline/read-only and submits no orders.
