"""Read-only orchestration from a market candidate to portfolio risk preflight."""

from .entry_plan import EntryPlanPolicy
from .risk_authorizer import CandidateRiskPreflight, PortfolioRiskAuthorizer
from .runtime import ReadOnlyRuntime, RuntimePolicy

_FINANCIAL_FIELDS = {
    "account_equity_usdt",
    "available_usdt",
    "projected_daily_pnl_after_planned_stop_quote",
    "planned_entry",
    "candidate",
    "candidate_entry_plan",
    "entry_price",
    "size",
    "stop_loss",
    "take_profit",
    "risk_to_stop_quote",
    "notional_quote",
    "equity_usdt",
    "daily_pnl_quote",
}

_SENSITIVE_KEY_PARTS = (
    "equity",
    "balance",
    "availbal",
    "available",
    "frozen",
    "liab",
    "borrow",
    "pnl",
    "profit",
    "notional",
    "entry_price",
    "stop_loss",
    "take_profit",
    "risk_to_stop",
    "planned_entry",
    "candidate_entry_plan",
    "size",
    "position_size",
    "api_key",
    "secret",
    "passphrase",
    "token",
    "uid",
    "account_id",
)


def _is_sensitive_key(key) -> bool:
    normalized = str(key).strip().lower()
    if normalized in _FINANCIAL_FIELDS:
        return True
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_financials(value):
    """Drop account-financial and credential-adjacent fields for public logs."""
    if isinstance(value, dict):
        return {
            key: _redact_financials(item)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [_redact_financials(item) for item in value]
    return value


def verify_execution_key_auth(client) -> dict:
    """Always-on private API authentication check for the execution key.

    Uses private GETs only. Verifies the key carries exactly the expected
    Demo execution permissions (read_only + trade) on a Spot-mode account
    with borrowing automation off. Returns a log-safe summary without any
    balance or PnL values.
    """
    summary = {
        "status": "auth_failed",
        "account_mode": "unknown",
        "permissions_ok": False,
        "blockers": [],
    }
    try:
        config_rows = client.get_account_config()
    except Exception as exc:  # network, HTTP, or signing failure
        summary["blockers"].append(f"account_config_request_failed:{type(exc).__name__}")
        return summary
    if not isinstance(config_rows, list) or len(config_rows) != 1:
        summary["blockers"].append("account_config_missing_or_ambiguous")
        return summary
    config = config_rows[0]
    if not isinstance(config, dict):
        summary["blockers"].append("account_config_malformed")
        return summary
    summary["account_mode"] = "spot" if str(config.get("acctLv")) == "1" else "not_spot_or_unknown"
    if summary["account_mode"] != "spot":
        summary["blockers"].append("account_not_in_spot_mode")
    raw_permissions = config.get("perm")
    permission_parts = raw_permissions.split(",") if isinstance(raw_permissions, str) else []
    permissions = {value.strip().lower() for value in permission_parts if value.strip()}
    malformed = (
        not isinstance(raw_permissions, str)
        or not raw_permissions.strip()
        or any(not value.strip() for value in permission_parts)
    )
    if malformed:
        summary["blockers"].append("api_key_permissions_malformed")
    elif permissions != {"read_only", "trade"}:
        summary["blockers"].append("api_key_permissions_not_exactly_read_only_trade")
    else:
        summary["permissions_ok"] = True
    if config.get("autoLoan") is not False:
        summary["blockers"].append("auto_loan_not_disabled")
    if config.get("spotBorrowAutoRepay") is not False:
        summary["blockers"].append("spot_borrow_auto_repay_not_disabled")
    try:
        client.get_balance()
    except Exception as exc:
        summary["blockers"].append(f"balance_request_failed:{type(exc).__name__}")
    if not summary["blockers"]:
        summary["status"] = "auth_verified"
    return summary


def run_read_only_preflight(client, state_store, settings, *,
                            bar: str = "1H",
                            entry_plan_policy: EntryPlanPolicy,
                            clock_ms=None) -> dict:
    """Run one market scan and preflight only the highest-ranked buy candidate.

    This orchestration has no order manager and never invokes exchange writes.
    Private API authentication is verified first, unconditionally, so a
    green result always proves the execution key works. Returned reports are
    redacted of account-financial fields for safe use in public CI logs.
    """
    if not isinstance(entry_plan_policy, EntryPlanPolicy):
        raise ValueError("an explicit EntryPlanPolicy is required for preflight")

    auth = verify_execution_key_auth(client)
    if auth["status"] != "auth_verified":
        return {
            "mode": "read_only_candidate_risk_preflight",
            "status": "auth_failed",
            "reason": "private_api_authentication_not_verified",
            "orders_enabled": False,
            "order_submission_attempts": 0,
            "private_api_auth": auth,
            "market_analysis": None,
            "risk_preflight": None,
        }

    runtime = ReadOnlyRuntime(
        client,
        state_store,
        policy=RuntimePolicy(bar=bar, entry_plan_policy=entry_plan_policy),
    )
    analysis = runtime.run_once()
    candidate_signal = next(
        (
            signal for signal in analysis.get("signals", [])
            if signal.get("signal") == "buy"
            and isinstance(signal.get("candidate_entry_plan"), dict)
        ),
        None,
    )
    if candidate_signal is None:
        return {
            "mode": "read_only_candidate_risk_preflight",
            "status": "no_candidate",
            "reason": "no_valid_buy_candidate_from_current_market_scan",
            "orders_enabled": False,
            "order_submission_attempts": 0,
            "private_api_auth": auth,
            "market_analysis": _redact_financials(analysis),
            "risk_preflight": None,
        }

    # The provider is intentionally unused by preflight_candidate: the exact
    # immutable candidate object is passed directly to the read-only evaluator.
    bar_ms = {"1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}.get(bar)
    if bar_ms is None:
        raise ValueError("preflight supports only 1H, 4H, or 1D candles")

    authorizer = PortfolioRiskAuthorizer(
        client,
        state_store,
        settings,
        plan_provider=lambda **kwargs: None,
        bar_ms=bar_ms,
        clock_ms=clock_ms,
    )
    decision = CandidateRiskPreflight(authorizer, state_store).evaluate(
        candidate_signal["candidate_entry_plan"]
    )
    return {
        "mode": "read_only_candidate_risk_preflight",
        "status": decision["status"],
        "orders_enabled": False,
        "order_submission_attempts": 0,
        "private_api_auth": auth,
        "selected_instrument": candidate_signal.get("instrument"),
        "market_analysis": _redact_financials(analysis),
        "risk_preflight": _redact_financials(decision),
    }
