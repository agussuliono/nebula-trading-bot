"""Conservative quote-currency cash-flow accounting from immutable OKX Spot fills."""
from decimal import Decimal, InvalidOperation


class AccountingError(ValueError):
    pass


def round_trip_accounting(fills: list[dict], *, instrument: str, dust_limit: str = "0") -> dict:
    """Calculate quote cash flow and realized P&L only when base inventory is flat.

    OKX fee sign is preserved: negative is charged, positive is a rebate. Fees in
    base currency change base inventory; fees in quote currency change quote cash
    flow. Third-currency fees are explicitly unsupported rather than guessed.
    """
    try:
        base_ccy, quote_ccy = instrument.split("-", 1)
        dust = Decimal(str(dust_limit))
    except (ValueError, InvalidOperation) as exc:
        raise AccountingError("Invalid instrument or dust limit") from exc
    if not dust.is_finite() or dust < 0:
        raise AccountingError("Dust limit must be finite and non-negative")

    base_delta = Decimal("0")
    quote_cash_flow = Decimal("0")
    unsupported_fees = []
    buy_size = sell_size = Decimal("0")
    for fill in fills:
        try:
            size = Decimal(str(fill["fill_size"]))
            price = Decimal(str(fill["fill_price"]))
            fee = Decimal(str(fill["fee"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise AccountingError("Malformed persisted fill") from exc
        if not all(x.is_finite() for x in (size, price, fee)) or size <= 0 or price <= 0:
            raise AccountingError("Non-finite or non-positive fill values")
        side, fee_ccy = fill["side"], fill["fee_currency"]
        gross_quote = size * price
        if side == "buy":
            base_delta += size
            buy_size += size
            quote_cash_flow -= gross_quote
        elif side == "sell":
            base_delta -= size
            sell_size += size
            quote_cash_flow += gross_quote
        else:
            raise AccountingError("Unsupported fill side")

        if fee_ccy == base_ccy:
            base_delta += fee
        elif fee_ccy == quote_ccy:
            quote_cash_flow += fee
        else:
            unsupported_fees.append(fee_ccy)

    flat = base_delta == 0
    within_dust = abs(base_delta) <= dust
    return {
        "instrument": instrument,
        "base_currency": base_ccy,
        "quote_currency": quote_ccy,
        "buy_fill_size": str(buy_size),
        "sell_fill_size": str(sell_size),
        "base_inventory_delta": str(base_delta),
        "quote_cash_flow": str(quote_cash_flow),
        "is_flat_by_fill_ledger": flat,
        "is_within_dust_by_fill_ledger": within_dust,
        "realized_pnl_quote": str(quote_cash_flow) if flat and not unsupported_fees else None,
        "unsupported_fee_currencies": sorted(set(unsupported_fees)),
        "accounting_complete": flat and not unsupported_fees,
    }
