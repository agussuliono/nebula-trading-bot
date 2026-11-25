"""Fail-closed read-only account reconciliation for OKX Spot Demo."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class ReconciliationError(RuntimeError):
    pass


def _decimal(value, field):
    try:
        result = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReconciliationError(f"Invalid {field} in account response") from exc
    if not result.is_finite() or result < 0:
        raise ReconciliationError(f"Invalid {field} in account response")
    return result


def _liability(value, field):
    # Strict liability parsing: booleans and other non-numeric types are not
    # valid API amounts and must never read as zero borrowings.
    if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
        raise ReconciliationError(f"Invalid {field} in account response")
    if isinstance(value, str) and not value.strip():
        raise ReconciliationError(f"Invalid {field} in account response")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReconciliationError(f"Invalid {field} in account response") from exc
    if not result.is_finite() or result < 0:
        raise ReconciliationError(f"Invalid {field} in account response")
    return result


@dataclass(frozen=True)
class SpotAccountSnapshot:
    instrument: str
    base_currency: str
    available_base: Decimal
    frozen_base: Decimal
    available_quote: Decimal
    frozen_quote: Decimal
    open_orders: tuple[dict, ...]
    open_algo_orders: tuple[dict, ...]
    borrowings_present: bool = False

    @property
    def has_open_orders(self):
        return bool(self.open_orders or self.open_algo_orders)

    def is_flat(self, dust_limit: str = "0") -> bool:
        dust = _decimal(dust_limit, "dust_limit")
        return self.available_base + self.frozen_base <= dust and not self.has_open_orders


class DemoAccountReconciler:
    """Fetches independent account views; never submits, cancels, or releases locks."""
    def __init__(self, client):
        self.client = client

    def snapshot(self, instrument: str) -> SpotAccountSnapshot:
        if not instrument or not instrument.endswith("-USDT"):
            raise ValueError("V1 reconciliation only supports USDT-quoted Spot instruments")
        base, quote = instrument.split("-", 1)
        # All reads must succeed. A partial set of reads is not a reconciliation.
        balances = self.client.get_balance()
        orders = self.client.get_pending_orders("SPOT")
        algos = [
            *self.client.get_pending_algo_orders("oco"),
            *self.client.get_pending_algo_orders("conditional"),
        ]
        if len(balances) != 1 or not isinstance(balances[0].get("details"), list):
            raise ReconciliationError("Account balance response is missing its details array")
        details = {row.get("ccy"): row for row in balances[0]["details"] if isinstance(row, dict)}
        borrowings_present = False
        for row in balances[0]["details"]:
            if not isinstance(row, dict) or "liab" not in row:
                raise ReconciliationError("Account balance liability is missing")
            if _liability(row["liab"], "liab") > 0:
                borrowings_present = True
        base_row, quote_row = details.get(base, {}), details.get(quote, {})
        # An absent currency means zero, but malformed present values fail closed.
        return SpotAccountSnapshot(
            instrument=instrument, base_currency=base,
            available_base=_decimal(base_row.get("availBal", "0"), "base availBal"),
            frozen_base=_decimal(base_row.get("frozenBal", "0"), "base frozenBal"),
            available_quote=_decimal(quote_row.get("availBal", "0"), "quote availBal"),
            frozen_quote=_decimal(quote_row.get("frozenBal", "0"), "quote frozenBal"),
            open_orders=tuple(x for x in orders if x.get("instId") == instrument),
            open_algo_orders=tuple(x for x in algos if x.get("instId") == instrument),
            borrowings_present=borrowings_present,
        )
