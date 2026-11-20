# NEBULA Security and Risk Audit Remediation

Date: 2026-11-20
Base: main at `cca278852abb914f87e0d0ecd0d216cf12c10323`
Working branch: `audit/fix-safety-gates-2026-11-20`

## Implemented in this branch

8. Added an append-only prospective 1H collector for the frozen BTC/ETH/SOL research basket. It validates the exact baseline CSV SHA-256, matching manifest, fixed cutoff, confirmed candle status, strict timestamp continuity, and refuses overlap/gaps/tampering. It uses only the public market-candles endpoint and reports zero private API calls and zero submitted orders. The CLI is `collect-prospective`; the original evidence files must be copied with their manifests before collection.

1. OKX REST adapter now uses an explicit method/path allowlist for supported public reads, private reads, and four reviewed Spot order/cancel endpoints. Unknown endpoints and method/path combinations fail before network I/O.
2. All private write requests are centrally gated in the adapter. Regular order cancellation has a separate permission check so an emergency stop can prevent new exposure while still allowing an operator to cancel a resting regular order. OCO cancellation remains behind the full emergency-stop gate because it can remove active protection.
3. Real OKX Spot buy submissions through `DemoOrderManager` now fail closed unless a portfolio risk-authorizer callback is supplied. The callback must approve the entry, provide a plan matching the exchange-normalized instrument, size, and price, and report a finite positive stop risk not exceeding the configured per-position risk limit.
4. Portfolio-once now synchronizes entry trade fills before inventory valuation. Fill-sync failures are recorded and surfaced as blockers, and mark valuation incomplete.
5. `valuation_complete` is now separated from entry eligibility. Policy blockers such as an already-active position or a missing proposed entry no longer falsely mark otherwise complete account valuation as incomplete.
6. Added `PortfolioRiskAuthorizer`, which fetches fresh private account/balance/order snapshots, checks persistent daily realized P&L, rejects unresolved orders or active positions, requires a matching confirmed closed-candle buy plan with precomputed stop/target, and delegates fee/slippage-aware sizing-risk checks to `PortfolioRiskEngine`. It returns an approval object only; it never submits an order. The caller must inject it into `DemoOrderManager`.
7. Corrected API-key permission validation: OKX `perm` must include both `read_only` and `trade`, and must not include `withdraw`. The prior check incorrectly required read-only permission without requiring Trade, which could falsely classify a read-only-only key as eligible.

## Explicitly not completed / still blocks V1

- There is no production risk-authorizer implementation wired into an automated runtime yet. The new callback requirement intentionally blocks real Demo buy submission until the portfolio snapshot, daily P&L, position sizing, fee/slippage, and stop-risk calculation are connected end-to-end.
- No complete trading orchestrator currently coordinates scanner → closed-candle signal → stop/target plan → risk approval → order manager → OCO protection → reconciliation. The current authorizer is a callable component, not proof of end-to-end integration.
- Protective-order lifecycle needs further integration testing against current OKX Demo responses, including OCO trigger behavior, partial exits, child-order fills, and restart recovery.
- Emergency-stop policy for protective actions needs a full lifecycle design and tests. This change only allows cancellation of regular orders during the stop; it does not bypass the stop for OCO submission/cancellation.
- GitHub CodeQL, secret scanning/push protection, and branch-protection settings were not changed here. Repository security settings require a separate explicit settings audit and, where applicable, account-level permissions.
- No OKX API credentials were used and no exchange write endpoint was called by these code changes.
- Prospective collection has not yet been run on the user's laptop; no new market observations have been collected or evaluated here.
- The collector fetches at most 300 recent candles per request and fails closed if it cannot append a contiguous sequence. CSV and manifest are staged before replacement, but their two replacements are not a single filesystem transaction.

## Safety invariants retained

- OKX Demo header remains fixed to `x-simulated-trading: 1`.
- REST host remains fixed to `https://openapi.okx.com`; the Demo header is mandatory.
- `NEBULA_ENABLE_DEMO_ORDERS` remains false by default.
- `NEBULA_KILL_SWITCH` remains true by default.
- No live trading, leverage, margin, futures, grid, withdrawal, account-setting changes, or real-money actions are introduced.

## Verification

Initial tests cover API-key permission requirements and risk-authorizer allow/deny behavior. The later sections below record the current branch-head verification and supersede this initial note.


## Additional strategy-input hardening
- The EMA signal component now validates the exact nine-field OKX candle shape, strictly newest-first unique timestamps, finite positive OHLC values, internally consistent high/low ranges, and confirmation flags limited to 0/1.
- EMA inputs must be finite positive prices; periods must be integer values satisfying 0 < fast < slow <= 500.
- This is input-integrity hardening only. It does not improve or establish strategy profitability, and the EMA strategy remains research-only and not approved for automated orders.

## Repeated efficiency and safety audit — 2026-11-20
- Restrict CI push-triggered runs to `main`; PR branches continue to run on `pull_request`. This removes duplicate test-suite executions for commits that update an open PR while retaining validation for the target branch and manual dispatch.

A subsequent review of the authenticated HTTP boundary, persistent OCO state transitions, precision-sensitive position-lock release, and CI workflow found and addressed these additional issues:

- Disable automatic HTTP redirects in the OKX adapter. Any 3xx response is rejected before normal response parsing. This prevents private authentication headers or a signed order POST from being forwarded/replayed to a redirect target.
- Keep a protective OCO intent in the retryable `reserved` state when the adapter rejects it locally before network I/O (for example, the Demo gate is still closed). The transition is allowed only from `submitting`; a network-ambiguous `submission_unknown` intent cannot be reset or resubmitted.
- Replace float-based rejected-entry fill checks with exact finite Decimal comparison. A tiny non-zero fill can no longer round down to zero and release the one-position lock.
- Cache pip dependencies in GitHub Actions using `pyproject.toml` as the cache key input. This reduces repeat dependency downloads without changing test or bot runtime behavior.

Added regression tests cover 302/307 redirects on private GET/POST, retry of a locally blocked OCO after explicit safety-gate correction, refusal to reset an ambiguous OCO, and exact tiny-fill lock retention. All exchange interactions in these tests use local fakes; no API credential or exchange write request is used.

Verification for branch head `22b398f4e1edce0b569bc55d6327fef3d148badc`: GitHub Actions run #143 completed successfully. `pytest -q` reported **257 passed in 2.27s**; compileall, explicit Demo-off/emergency-stop-on checks, and fail-closed default checks all passed. The pip cache restored successfully (`Cache hit`). Tests used no OKX credentials and submitted no exchange write request.

## Risk preflight orchestration — next milestone

- Added `CandidateRiskPreflight` as a separate boundary between a read-only ATR candidate and `PortfolioRiskAuthorizer`. It validates a confirmed buy candidate, positive finite size/prices, stop < entry < target, exact zero submitted orders, and records approved/rejected outcomes in the local audit ledger.
- The preflight calls only the portfolio authorizer's private-read path. It has no order manager and does not expose submit/cancel methods.
- Separated authenticated private-read permission from the Demo write gate. A user can inspect risk using a Demo read/trade key while `NEBULA_ENABLE_DEMO_ORDERS=false` and the emergency stop remains active. The order-manager authorization path still requires the full Demo write gate.
- Strengthened the order authorizer so the provider's instrument, size, and entry price must exactly match the normalized order; stop/target must be finite and ordered.
- Strengthened portfolio checks so a candidate price must be at or above the latest ask and no more than the configured slippage allowance above it. A candidate based on a stale/moved market is rejected.
- Added regression tests for approved read-only preflight, malformed candidate rejection before private reads, stale-price rejection, boolean order-count rejection, mismatched provider instrument, and the unchanged Demo write gate.

This milestone is a local/fake-backed integration boundary only. No API credentials, private OKX account reads, or exchange writes were used in this development session. Real Demo private-read verification remains a later separately authorized step.

### Verification — candidate risk preflight implementation

- The preflight accepts only a confirmed closed-candle buy candidate with finite positive values, valid stop/entry/target ordering, and an exact integer `orders_submitted=0`.
- The preflight uses authenticated private reads without opening the Demo write gate. The normal `DemoOrderManager` authorization path still requires the explicit write gate and emergency-stop clearance.
- Current public Spot instrument metadata is fetched again and tick/lot/minimum-size rules are revalidated before private account snapshots.
- The latest bid/ask spread, ask depth, and allowed slippage band are rechecked against the candidate price; widened spreads and stale/moved prices fail closed.
- Plan instrument, size, entry price, stop, and target must remain consistent across the candidate and portfolio approval.
- The preflight has no order manager and records an auditable approved/rejected result with `order_submission_attempts=0`.
- GitHub Actions run #166 on `630939ce6d49503901c881d73cf352c9adc87116` completed successfully: **271 tests passed in 1.77s**. Compileall, explicit Demo-off/emergency-stop-on checks, and fail-closed default checks passed. Pip cache restored successfully.
- All tests used local fakes and SQLite. No actual OKX credentials, private account requests, or exchange write requests were used in this development session.

This does not yet connect preflight output to automated order submission. It is a risk decision only; Demo order lifecycle, OCO protection, partial-fill reconciliation, and restart recovery remain separate blocked milestones.

## Read-only CLI orchestration and final preflight hardening

- Added `preflight-once`, requiring explicit paired `--plan-risk-quote` and `--plan-max-notional-quote` limits. It runs the public market scan, selects the highest-ranked buy candidate, and then performs read-only portfolio preflight. If no valid candidate exists, it does not call private account endpoints.
- The orchestrator passes the selected bar interval into candle freshness validation (1H/4H/1D); this avoids treating 4H/1D candles as 1H candles.
- Corrected the `RuntimePolicy` default `min_data_points` from 50 to 51 so the default is consistent with the required `slow_ema + 1` data points.
- Added `NEBULA_DEMO_ACCOUNT_EXCLUSIVE`, default false. Risk authorization refuses approval unless the user explicitly confirms that the Demo account is dedicated to NEBULA and has a clean local P&L baseline. This is necessary because daily realized P&L is currently tracked in the local NEBULA ledger, not reconstructed from every account-wide exchange bill/fill. Do not set this true if the account is also used manually/by another bot or has untracked same-day activity.
- Tightened `DemoOrderManager`: exchange-backed buys now require a concrete `PortfolioRiskAuthorizer` instance, not an arbitrary callback that could fabricate an approval.
- Regression tests cover CLI flag validation, no-candidate private-read avoidance, read-only preflight approval, 4H candle age, account exclusivity default/gate, and concrete-authorizer enforcement.
- GitHub Actions run #190 on `7c89f92482e565341f5d483bd45db9f519ace7b4` completed successfully: **280 tests passed in 2.44s**. Compileall, explicit Demo-off/emergency-stop-on checks, and fail-closed defaults passed; pip cache restored successfully.

The CLI preflight is still an evaluation only. It does not call the order manager or submit any order. The account-exclusivity flag is a user attestation, not an automated exchange guarantee; account-wide P&L reconciliation remains required before production-like risk decisions.
