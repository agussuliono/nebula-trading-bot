# NEBULA Execution Orchestrator

The orchestrator coordinates one cycle of market scanning, startup/account
reconciliation, portfolio risk authorization, one Spot entry, entry-fill
reconciliation, and protective OCO placement.

## Default mode: shadow-only

Run:

```bash
nebula orchestrate-once
```

This mode uses public market data only. It can report a candidate and its
precomputed ATR stop/target, but it does not read private account data or submit,
cancel, or replace exchange orders.

## Explicit OKX Demo execution

The only execution mode is OKX Spot Demo. It is not enabled by default.

Before a deliberate Demo run, configure credentials locally as environment
variables (never paste API secrets into chat, source files, or logs):

- `OKX_API_KEY`
- `OKX_SECRET_KEY`
- `OKX_PASSPHRASE`
- `NEBULA_DEMO_ACCOUNT_EXCLUSIVE=true`
- `NEBULA_ENABLE_DEMO_ORDERS=true`
- `NEBULA_KILL_SWITCH=false`
- `NEBULA_MAX_DAILY_LOSS_QUOTE`
- `NEBULA_MAX_RISK_QUOTE`
- `NEBULA_MAX_POSITION_QUOTE`

The API key must have read and trade permissions only, with no withdrawal
permission. Startup recovery also requires Spot account mode and borrowing
features disabled. The dedicated Demo-account attestation is an operator
assertion; the code cannot independently prove that no other application or
person uses the account.

After reviewing those settings and confirming the account is the dedicated
Demo account, the explicit command is:

```bash
nebula orchestrate-once --execute-demo
```

The flag does not override any safety gate. Missing credentials, disabled Demo
orders, active emergency stop, missing account attestation, circuit-breaker
lockout, unresolved intents, open/untracked orders, untracked assets, or an
inconsistent database block execution.

## One-cycle lifecycle

1. Scan eligible live USDT Spot instruments using fresh tickers and confirmed
   closed candles.
2. If a buy candidate exists, run read-only startup recovery and account-wide
   reconciliation. Existing protected positions are monitored; triggered OCO
   exits are reconciled and the position lock is released only after complete
   fill accounting and a fresh account snapshot prove the account flat.
3. Require a fresh one-shot StartupEntryPermit.
4. Re-evaluate the exact candidate through PortfolioRiskAuthorizer using fresh
   private account, balance, order, ticker, and instrument-rule snapshots.
5. Submit one limit entry with a deterministic client order ID.
6. Reconcile the entry. If it remains live or partially filled, cancel its
   remainder and confirm the final state before protection.
7. Prepare and submit the persisted, portfolio-approved OCO for net base
   inventory after fees.
8. Keep the position lock if any state is ambiguous or protection is not
   confirmed. Never blindly retry an ambiguous entry or OCO.

Only one cycle is run per CLI invocation. This is not a background service or
a 14-day forward test. Do not rerun after an ambiguous outcome until the local
ledger and Demo account have been reconciled.

## Current limitations

- EMA/ATR settings remain research parameters, not evidence of profitability.
- The orchestrator does not optimize or dynamically learn parameters.
- A successful CI run is not evidence of exchange connectivity, order fills,
  strategy profitability, or a completed 14-day Demo forward test.
- The current change does not enable Demo execution in the user's environment.
