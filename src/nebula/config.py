from dataclasses import dataclass
import math
import os

# OKX REST domains depend on the region where the account was registered.
# Keep this as a closed allowlist: callers may select a region, never a URL.
# Demo requests to every regional host must still carry the mandatory
# x-simulated-trading: 1 header; no production-mode switch exists in NEBULA.
DEMO_REST_URLS = {
    "global": "https://openapi.okx.com",
    "us_au": "https://us.okx.com",
    "eea": "https://eea.okx.com",
}
DEFAULT_DEMO_REGION = "global"
# Backward-compatible name for the default global Demo endpoint.
DEMO_REST_URL = DEMO_REST_URLS[DEFAULT_DEMO_REGION]
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


def _okx_region() -> str:
    value = os.getenv("NEBULA_OKX_REGION", DEFAULT_DEMO_REGION).strip().lower()
    if value not in DEMO_REST_URLS:
        allowed = ", ".join(sorted(DEMO_REST_URLS))
        raise ValueError(f"NEBULA_OKX_REGION must be one of: {allowed}")
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""
    okx_api_region: str = DEFAULT_DEMO_REGION
    demo_account_exclusive: bool = False
    enable_demo_orders: bool = False
    # Fail closed: even if the Demo order gate is explicitly enabled, the
    # emergency stop remains active until the operator explicitly clears it.
    kill_switch: bool = True
    max_daily_loss_quote: float = 0.0
    max_risk_quote: float = 0.0
    max_position_quote: float = 0.0
    fee_bps: float = 10.0
    slippage_bps: float = 10.0
    market_data_max_age_ms: int = 30_000

    def __post_init__(self):
        if self.okx_api_region not in DEMO_REST_URLS:
            allowed = ", ".join(sorted(DEMO_REST_URLS))
            raise ValueError(f"okx_api_region must be one of: {allowed}")

    @classmethod
    def from_public_env(cls) -> "Settings":
        """Load non-secret market-data settings without reading API credentials.

        Public-only commands deliberately cannot inspect OKX key variables or
        execution permissions. Credential fields and order gates retain their
        safe dataclass defaults.
        """
        return cls(
            okx_api_region=_okx_region(),
            fee_bps=_finite_float("NEBULA_FEE_BPS", "10"),
            slippage_bps=_finite_float("NEBULA_SLIPPAGE_BPS", "10"),
            market_data_max_age_ms=_positive_int("NEBULA_MARKET_DATA_MAX_AGE_MS", "30000"),
        )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=os.getenv("OKX_API_KEY", ""),
            secret_key=os.getenv("OKX_SECRET_KEY", ""),
            passphrase=os.getenv("OKX_PASSPHRASE", ""),
            okx_api_region=_okx_region(),
            demo_account_exclusive=_bool("NEBULA_DEMO_ACCOUNT_EXCLUSIVE"),
            enable_demo_orders=_bool("NEBULA_ENABLE_DEMO_ORDERS"),
            kill_switch=_bool("NEBULA_KILL_SWITCH", default=True),
            max_daily_loss_quote=_finite_float("NEBULA_MAX_DAILY_LOSS_QUOTE", "0"),
            max_risk_quote=_finite_float("NEBULA_MAX_RISK_QUOTE", "0"),
            max_position_quote=_finite_float("NEBULA_MAX_POSITION_QUOTE", "0"),
            fee_bps=_finite_float("NEBULA_FEE_BPS", "10"),
            slippage_bps=_finite_float("NEBULA_SLIPPAGE_BPS", "10"),
            market_data_max_age_ms=_positive_int("NEBULA_MARKET_DATA_MAX_AGE_MS", "30000"),
        )

    def _validate_demo_write_base(self) -> None:
        if not self.enable_demo_orders:
            raise SafetyError("Demo order submission is disabled")
        if not all((self.api_key, self.secret_key, self.passphrase)):
            raise SafetyError("Demo API credentials are incomplete")
        limits = (self.max_daily_loss_quote, self.max_risk_quote, self.max_position_quote)
        if not all(math.isfinite(x) and x > 0 for x in limits):
            raise SafetyError("All risk limits must be finite and explicitly configured above zero")
        if self.max_risk_quote > self.max_daily_loss_quote:
            raise SafetyError("Per-trade risk cannot exceed the daily loss limit")

    def validate_private_read_permission(self) -> None:
        """Allow authenticated Demo reads without enabling any exchange writes."""
        if not all((self.api_key, self.secret_key, self.passphrase)):
            raise SafetyError("Private API credentials are incomplete")

    def validate_order_permission(self) -> None:
        self._validate_demo_write_base()
        if self.kill_switch:
            raise SafetyError("Emergency stop is active")

    def validate_cancel_permission(self) -> None:
        """Allow canceling a regular order during emergency stop; never allows new exposure."""
        self._validate_demo_write_base()
