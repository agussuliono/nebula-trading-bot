"""Deterministic, closed-candle-only Spot entry planning (no order submission).

ATR stop/target levels are provisional research parameters, not evidence of
strategy profitability. This module only builds a candidate plan; portfolio
authorization and the exchange order gate remain separate mandatory controls.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP

from .instruments import SpotInstrument
from .strategy import closed_candles


class EntryPlanError(ValueError):
    """A candidate entry plan cannot be constructed safely."""


def _decimal(value, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EntryPlanError(f"{field} must be a valid decimal") from exc
    if not result.is_finite():
        raise EntryPlanError(f"{field} must be finite")
    return result


@dataclass(frozen=True)
class EntryPlanPolicy:
    risk_budget_quote: Decimal
    max_notional_quote: Decimal
    atr_period: int = 14
    stop_atr_multiple: Decimal = Decimal("2")
    reward_risk: Decimal = Decimal("2")
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("10")
    max_candle_age_intervals: int = 2

    def __post_init__(self):
        for name in ("risk_budget_quote", "max_notional_quote", "stop_atr_multiple",
                     "reward_risk", "fee_bps", "slippage_bps"):
            value = _decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        if self.risk_budget_quote <= 0 or self.max_notional_quote <= 0:
            raise ValueError("risk budget and max notional must be positive")
        if self.stop_atr_multiple <= 0 or self.reward_risk <= 0:
            raise ValueError("ATR stop multiple and reward/risk must be positive")
        if self.fee_bps < 0 or self.slippage_bps < 0 or self.fee_bps + self.slippage_bps >= 10000:
            raise ValueError("fee and slippage assumptions must be nonnegative and below 10000 bps combined")
        if isinstance(self.atr_period, bool) or not isinstance(self.atr_period, int) or not 2 <= self.atr_period <= 200:
            raise ValueError("atr_period must be an integer in 2..200")
        if (isinstance(self.max_candle_age_intervals, bool)
                or not isinstance(self.max_candle_age_intervals, int)
                or self.max_candle_age_intervals < 1):
            raise ValueError("max_candle_age_intervals must be a positive integer")


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def build_entry_plan(*, candles: list, signal, entry_price, instrument: SpotInstrument,
                     policy: EntryPlanPolicy, now_ms: int, bar_ms: int) -> dict:
    """Build an ATR-based long Spot plan from the exact latest confirmed buy candle.

    The entry price must already be aligned to the exchange tick. Stop is rounded
    up and target down to avoid understating risk or overstating reward. Quantity
    is floored to lotSz and checked against minSz and the configured risk/notional
    ceilings. No network access, private account read, or order write occurs.
    """
    if not isinstance(instrument, SpotInstrument):
        raise EntryPlanError("A validated SpotInstrument is required")
    if instrument.state != "live" or instrument.quote_ccy != "USDT":
        raise EntryPlanError("Only live USDT-quoted Spot instruments are supported")
    if getattr(signal, "action", None) != "buy":
        raise EntryPlanError("Only a confirmed buy signal can produce a long entry plan")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
        raise EntryPlanError("now_ms must be a positive integer")
    if isinstance(bar_ms, bool) or not isinstance(bar_ms, int) or bar_ms <= 0:
        raise EntryPlanError("bar_ms must be a positive integer")

    rows = closed_candles(candles)
    if len(rows) < policy.atr_period + 1:
        raise EntryPlanError("Insufficient confirmed candles for ATR")
    last_ts = int(rows[-1][0])
    if isinstance(getattr(signal, "candle_ts", None), bool) or signal.candle_ts != last_ts:
        raise EntryPlanError("Buy signal timestamp must match the latest confirmed candle")
    close_ts = last_ts + bar_ms
    if close_ts > now_ms or now_ms - close_ts > bar_ms * policy.max_candle_age_intervals:
        raise EntryPlanError("Signal candle is incomplete, future-dated, or stale")

    # ATR uses true range over the most recent N confirmed bars. Volume must be
    # positive; a zero-volume or malformed bar cannot silently influence a stop.
    selected = rows[-(policy.atr_period + 1):]
    true_ranges = []
    previous_close = None
    for row in selected:
        try:
            high = _decimal(row[2], "candle high")
            low = _decimal(row[3], "candle low")
            close = _decimal(row[4], "candle close")
            volume = _decimal(row[5], "candle volume")
        except (IndexError, EntryPlanError) as exc:
            raise EntryPlanError("ATR candle data is invalid") from exc
        if min(high, low, close, volume) <= 0 or high < low:
            raise EntryPlanError("ATR requires positive traded candles with valid high/low")
        if previous_close is not None:
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = close
    if len(true_ranges) != policy.atr_period:
        raise EntryPlanError("ATR sample is incomplete")
    atr = sum(true_ranges, Decimal("0")) / Decimal(policy.atr_period)
    price = _decimal(entry_price, "entry price")
    if price <= 0 or price % instrument.tick_size != 0:
        raise EntryPlanError("Entry price must be positive and aligned to tickSz")

    raw_stop = price - atr * policy.stop_atr_multiple
    stop = _ceil_step(raw_stop, instrument.tick_size)
    risk_per_unit_price = price - stop
    if stop <= 0 or risk_per_unit_price <= 0:
        raise EntryPlanError("ATR stop is not positive and below entry")
    raw_target = price + risk_per_unit_price * policy.reward_risk
    target = _floor_step(raw_target, instrument.tick_size)
    if target <= price:
        raise EntryPlanError("Rounded take-profit is not above entry")

    fee_rate = policy.fee_bps / Decimal("10000")
    slippage_rate = policy.slippage_bps / Decimal("10000")
    entry_cash_per_unit = price * (Decimal("1") + fee_rate)
    stop_proceeds_per_unit = stop * (Decimal("1") - slippage_rate) * (Decimal("1") - fee_rate)
    risk_per_unit = entry_cash_per_unit - stop_proceeds_per_unit
    if risk_per_unit <= 0:
        raise EntryPlanError("Fee/slippage-adjusted stop risk is invalid")
    risk_limited_qty = policy.risk_budget_quote / risk_per_unit
    notional_limited_qty = policy.max_notional_quote / price
    quantity = _floor_step(min(risk_limited_qty, notional_limited_qty), instrument.lot_size)
    if quantity < instrument.min_size:
        raise EntryPlanError("Risk and notional limits produce size below minSz")
    notional = quantity * price
    risk_quote = quantity * risk_per_unit
    if notional > policy.max_notional_quote or risk_quote > policy.risk_budget_quote:
        raise EntryPlanError("Rounded size exceeds configured risk or notional limit")

    return {
        "instrument": instrument.inst_id,
        "signal_action": "buy",
        "candle_closed": True,
        "candle_ts": last_ts,
        "atr": format(atr, "f"),
        "atr_period": policy.atr_period,
        "stop_method": "atr_candidate",
        "stop_atr_multiple": format(policy.stop_atr_multiple, "f"),
        "reward_risk": format(policy.reward_risk, "f"),
        "entry_price": format(price, "f"),
        "size": format(quantity, "f"),
        "stop_loss": format(stop, "f"),
        "take_profit": format(target, "f"),
        "notional_quote": format(notional, "f"),
        "risk_to_stop_quote": format(risk_quote, "f"),
        "fee_bps_assumption": format(policy.fee_bps, "f"),
        "slippage_bps_assumption": format(policy.slippage_bps, "f"),
        "orders_submitted": 0,
    }
