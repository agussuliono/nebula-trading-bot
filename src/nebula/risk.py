from dataclasses import dataclass
import math


@dataclass(frozen=True)
class PositionPlan:
    quantity: float
    notional: float
    risk_amount: float


def size_position(*, equity: float, entry: float, stop: float, risk_budget: float,
                  max_notional: float, fee_rate: float, slippage_rate: float) -> PositionPlan:
    """Size a long Spot position using net cash loss at the stop.

    ``entry`` is the expected buy fill price (already adjusted for adverse
    entry slippage). Per-unit risk therefore charges no second entry-slippage
    amount: it is entry cash outlay including fee minus net stop-sale proceeds.
    """
    vals = (equity, entry, stop, risk_budget, max_notional, fee_rate, slippage_rate)
    if not all(math.isfinite(x) for x in vals):
        raise ValueError("all sizing inputs must be finite")
    if min(equity, entry, risk_budget, max_notional) <= 0:
        raise ValueError("equity, entry, risk_budget and max_notional must be positive")
    if stop <= 0 or stop >= entry:
        raise ValueError("long spot stop must be positive and below entry")
    if not (0 <= fee_rate < 1 and 0 <= slippage_rate < 1):
        raise ValueError("fee and slippage rates must be in [0,1)")
    entry_cash_per_unit = entry * (1.0 + fee_rate)
    stop_proceeds_per_unit = stop * (1.0 - slippage_rate) * (1.0 - fee_rate)
    cost_per_unit = entry_cash_per_unit - stop_proceeds_per_unit
    if not math.isfinite(cost_per_unit) or cost_per_unit <= 0:
        raise ValueError("invalid per-unit risk")
    qty = min(risk_budget / cost_per_unit,
              max_notional / entry,
              equity / entry_cash_per_unit)
    notional = qty * entry
    return PositionPlan(qty, notional, qty * cost_per_unit)
