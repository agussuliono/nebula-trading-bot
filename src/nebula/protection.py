"""Pure protective-order planning; execution remains behind the Demo order gate."""
from decimal import Decimal, InvalidOperation


class ProtectionError(ValueError):
    pass


def build_spot_oco_exit(*, instrument: str, filled_size: str,
                        take_profit: str, stop_loss: str,
                        algo_client_order_id: str) -> dict:
    """Build an OCO sell plan sized only to exchange-confirmed filled base quantity."""
    if not instrument or not instrument.endswith("-USDT"):
        raise ProtectionError("Only USDT-quoted Spot instruments are supported")
    if not algo_client_order_id or len(algo_client_order_id) > 32 or not algo_client_order_id.isalnum():
        raise ProtectionError("Algo client ID must be 1..32 alphanumeric characters")
    try:
        qty, tp, sl = (Decimal(str(v)) for v in (filled_size, take_profit, stop_loss))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProtectionError("Quantity and trigger prices must be valid decimals") from exc
    if not all(v.is_finite() for v in (qty, tp, sl)) or qty <= 0 or sl <= 0 or tp <= sl:
        raise ProtectionError("Require finite positive filled size and stop loss, with take profit above stop loss")
    return {
        "instId": instrument, "tdMode": "cash", "side": "sell", "ordType": "oco",
        "sz": format(qty, "f"), "algoClOrdId": algo_client_order_id,
        "tpTriggerPx": format(tp, "f"), "tpOrdPx": "-1", "tpTriggerPxType": "last",
        "slTriggerPx": format(sl, "f"), "slOrdPx": "-1", "slTriggerPxType": "last",
    }
