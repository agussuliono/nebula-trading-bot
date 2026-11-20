# NEBULA candidate entry-plan contract

Date: 2026-11-20  
Branch: `audit/fix-safety-gates-2026-11-20`

## Scope

`src/nebula/entry_plan.py` builds a deterministic candidate plan for a long USDT Spot entry. It is a pure calculation component: it performs no network calls, private account reads, order writes, or state changes.

The plan requires:
- A validated live USDT Spot instrument specification.
- A buy signal whose timestamp exactly matches the latest confirmed candle.
- Enough confirmed, strictly ordered OKX candles to calculate ATR.
- A fresh, completed candle and a tick-aligned proposed entry price.
- Explicit positive quote-currency risk and notional ceilings.

## Candidate stop and target

The provisional research rule uses the arithmetic mean of the most recent 14 true ranges (ATR by default). The stop is placed two ATR below the proposed entry; the target is two times the stop distance above entry. Both values are rounded conservatively to the instrument tick: stop upward, target downward.

These defaults are engineering placeholders, not optimized parameters and not evidence of a profitable edge. They must be evaluated on out-of-sample historical data and later compared with Demo execution. Changing them requires a documented strategy experiment; do not optimize them on the same period used for evaluation.

## Position sizing

The candidate quantity is limited by both:
- Configured maximum loss at the stop, including estimated entry fee, exit fee, and adverse exit slippage.
- Configured maximum entry notional.

Quantity is floored to the current `lotSz`; a result below `minSz` is rejected. The plan is rejected if rounding causes either ceiling to be exceeded. The separate portfolio risk authorizer must still validate fresh account/balance/order snapshots and all configured risk controls.

## Safety boundary

- This module returns `orders_submitted: 0`.
- It does not invoke `DemoOrderManager`, `ProtectionManager`, or any exchange write endpoint.
- It does not grant order authorization. A future orchestrator must pass the exact plan through portfolio authorization, then preserve the existing immutable Demo gate and emergency stop.
- Entry price must be supplied by a future caller from a fresh executable quote/order policy; the last-traded price alone is not guaranteed to be an executable fill.
- The OCO lifecycle, partial fills, child-order reconciliation, restart recovery, and 14-day Demo forward test remain separate blockers.

## Verification

Automated tests cover closed-candle identity, freshness, ATR calculation, fee/slippage-aware sizing, tick/lot/minimum-size rules, non-buy signals, and invalid policy values. The GitHub Actions result for the new branch head is the source of truth for CI status; no local execution is claimed.
