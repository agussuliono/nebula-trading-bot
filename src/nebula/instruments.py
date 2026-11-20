"""Exact OKX Spot instrument rules and safe order-size normalization."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP


class InstrumentRuleError(ValueError):
    pass


def _decimal(value, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InstrumentRuleError(f"{field} must be a valid decimal") from exc
    if not result.is_finite():
        raise InstrumentRuleError(f"{field} must be finite")
    return result


@dataclass(frozen=True)
class SpotInstrument:
    inst_id: str
    base_ccy: str
    quote_ccy: str
    tick_size: Decimal
    lot_size: Decimal
    min_size: Decimal
    state: str = "live"

    @classmethod
    def from_okx(cls, row: dict) -> "SpotInstrument":
        if row.get("instType") != "SPOT":
            raise InstrumentRuleError("Only SPOT instruments are supported")
        spec = cls(
            inst_id=str(row.get("instId", "")),
            base_ccy=str(row.get("baseCcy", "")),
            quote_ccy=str(row.get("quoteCcy", "")),
            tick_size=_decimal(row.get("tickSz"), "tickSz"),
            lot_size=_decimal(row.get("lotSz"), "lotSz"),
            min_size=_decimal(row.get("minSz"), "minSz"),
            state=str(row.get("state", "")),
        )
        if not spec.inst_id or not spec.base_ccy or not spec.quote_ccy:
            raise InstrumentRuleError("Instrument identifiers/currencies are missing")
        if min(spec.tick_size, spec.lot_size, spec.min_size) <= 0:
            raise InstrumentRuleError("tickSz, lotSz and minSz must be positive")
        return spec

    def normalize_order(self, side: str, size, price) -> tuple[str, str]:
        if self.state != "live":
            raise InstrumentRuleError(f"Instrument is not tradable (state={self.state})")
        if side not in {"buy", "sell"}:
            raise InstrumentRuleError("side must be buy or sell")
        raw_size = _decimal(size, "size")
        raw_price = _decimal(price, "price")
        if raw_size <= 0 or raw_price <= 0:
            raise InstrumentRuleError("size and price must be positive")
        normalized_size = (raw_size / self.lot_size).to_integral_value(rounding=ROUND_DOWN) * self.lot_size
        # A buy is rounded down and a sell up, avoiding a more aggressive limit price.
        rounding = ROUND_DOWN if side == "buy" else ROUND_UP
        normalized_price = (raw_price / self.tick_size).to_integral_value(rounding=rounding) * self.tick_size
        if normalized_size < self.min_size:
            raise InstrumentRuleError("Normalized size is below OKX minSz")
        return format(normalized_size, "f"), format(normalized_price, "f")
