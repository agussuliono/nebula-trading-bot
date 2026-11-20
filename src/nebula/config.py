from dataclasses import dataclass
import math
import os

# OKX's documented REST host is shared by production and Demo. The mandatory
# x-simulated-trading header is therefore part of every request; never make
# the host or that flag user-configurable.
DEMO_REST_URL = "https://openapi.okx.com"
DEMO_FLAG = "1"


class SafetyError(RuntimeError):
    pass


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name} must be a boolean")
    return value in {"true", "1", "yes"}


def _finite_float(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _positive_int(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""
    enable_demo_orders: bool = False
    kill_switch: bool = False
    max_daily_loss_quote: float = 0.0
    max_risk_quote: float = 0.0
    max_position_quote: float = 0.0
    fee_bps: float = 10.0
    slippage_bps: float = 10.0
    market_data_max_age_ms: int = 30_000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=os.getenv("OKX_API_KEY", ""),
            secret_key=os.getenv("OKX_SECRET_KEY", ""),
            passphrase=os.getenv("OKX_PASSPHRASE", ""),
            enable_demo_orders=_bool("NEBULA_ENABLE_DEMO_ORDERS"),
            kill_switch=_bool("NEBULA_KILL_SWITCH"),
            max_daily_loss_quote=_finite_float("NEBULA_MAX_DAILY_LOSS_QUOTE", "0"),
            max_risk_quote=_finite_float("NEBULA_MAX_RISK_QUOTE", "0"),
            max_position_quote=_finite_float("NEBULA_MAX_POSITION_QUOTE", "0"),
            fee_bps=_finite_float("NEBULA_FEE_BPS", "10"),
            slippage_bps=_finite_float("NEBULA_SLIPPAGE_BPS", "10"),
            market_data_max_age_ms=_positive_int("NEBULA_MARKET_DATA_MAX_AGE_MS", "30000"),
        )

    def validate_order_permission(self) -> None:
        if not self.enable_demo_orders:
            raise SafetyError("Demo order submission is disabled")
        if self.kill_switch:
            raise SafetyError("Emergency stop is active")
        if not all((self.api_key, self.secret_key, self.passphrase)):
            raise SafetyError("Demo API credentials are incomplete")
        limits = (self.max_daily_loss_quote, self.max_risk_quote, self.max_position_quote)
        if not all(math.isfinite(x) and x > 0 for x in limits):
            raise SafetyError("All risk limits must be finite and explicitly configured above zero")
        if self.max_risk_quote > self.max_daily_loss_quote:
            raise SafetyError("Per-trade risk cannot exceed the daily loss limit")
