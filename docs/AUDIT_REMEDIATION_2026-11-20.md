# NEBULA Security and Risk Audit Remediation

Date: 2026-11-20
Base: main at `cca278852abb914f87e0d0ecd0d216cf12c10323`
Working branch: `audit/fix-safety-gates-2026-11-20`

## Implemented in this branch

1. OKX REST adapter now uses an explicit method/path allowlist for supported public reads, private reads, and four reviewed Spot order/cancel endpoints. Unknown endpoints and method/path combinations fail before network I/O.
2. All private write requests are centrally gated in the adapter. Regular order cancellation has a separate permission check so an emergency stop can prevent new exposure while still allowing an operator to cancel a resting regular order. OCO cancellation remains behind the full emergency-stop gate because it can remove active protection.
3. Real OKX Spot buy submissions through `DemoOrderManager` now fail closed unless a portfolio risk-authorizer callback is supplied. The callback must approve the entry, provide a plan matching the exchange-normalized instrument, size, and price, and report a finite positive stop risk not exceeding the configured per-position risk limit.
4. Portfolio-once now synchronizes entry trade fills before inventory valuation. Fill-sync failures are recorded and surfaced as blockers, and mark valuation incomplete.
5. `valuation_complete` is now separated from entry eligibility. Policy blockers such as an already-active position or a missing proposed entry no longer falsely mark otherwise complete account valuation as incomplete.
6. Added regression tests for endpoint allowlisting, central Demo write gating, cancellation during emergency stop, required/matching risk authorization, valuation status semantics, and failed fill synchronization.

## Explicitly not completed / still blocks V1

- There is no production risk-authorizer implementation wired into an automated runtime yet. The new callback requirement intentionally blocks real Demo buy submission until the portfolio snapshot, daily P&L, position sizing, fee/slippage, and stop-risk calculation are connected end-to-end.
- No complete trading orchestrator currently coordinates scanner → closed-candle signal → portfolio/risk approval → order manager → OCO protection → reconciliation.
- Protective-order lifecycle needs further integration testing against current OKX Demo responses, including OCO trigger behavior, partial exits, child-order fills, and restart recovery.
- Emergency-stop policy for protective actions needs a full lifecycle design and tests. This change only allows cancellation of regular orders during the stop; it does not bypass the stop for OCO submission/cancellation.
- GitHub CodeQL, secret scanning/push protection, and branch-protection settings were not changed here. Repository security settings require a separate explicit settings audit and, where applicable, account-level permissions.
- No OKX API credentials were used and no exchange write endpoint was called by these code changes.

## Safety invariants retained

- OKX Demo header remains fixed to `x-simulated-trading: 1`.
- REST host remains fixed to `https://openapi.okx.com`; the Demo header is mandatory.
- `NEBULA_ENABLE_DEMO_ORDERS` remains false by default.
- `NEBULA_KILL_SWITCH` remains true by default.
- No live trading, leverage, margin, futures, grid, withdrawal, account-setting changes, or real-money actions are introduced.

## Verification

GitHub Actions NEBULA CI runs on this branch. Test counts and final conclusion must be taken from the completed workflow run, not inferred from a successful commit response.
