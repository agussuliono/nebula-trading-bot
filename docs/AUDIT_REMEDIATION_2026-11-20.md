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

New tests cover API key permission requirements and risk authorizer allow/deny behavior. The latest branch head must receive a completed GitHub Actions run before this remediation batch is marked verified; no test count is inferred from commit creation.


## Additional strategy-input hardening
- The EMA signal component now validates the exact nine-field OKX candle shape, strictly newest-first unique timestamps, finite positive OHLC values, internally consistent high/low ranges, and confirmation flags limited to 0/1.
- EMA inputs must be finite positive prices; periods must be integer values satisfying 0 < fast < slow <= 500.
- This is input-integrity hardening only. It does not improve or establish strategy profitability, and the EMA strategy remains research-only and not approved for automated orders.
