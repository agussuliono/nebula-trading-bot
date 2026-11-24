"""Read-only OKX Demo connectivity and account-safety verification."""
from decimal import Decimal, InvalidOperation

from .config import DEMO_FLAG, DEMO_REST_URLS, DEFAULT_DEMO_REGION, SafetyError


def verify_demo_read_only(client, settings) -> dict:
    """Probe public and private Demo REST reads; never call an exchange write.

    This check intentionally accepts a Read-only API key. Trade permission is
    not required for connectivity verification and should not be exposed to a
    GitHub Actions read-only job.
    """
    report = {
        "mode": "okx_spot_demo_read_only_verification",
        "status": "blocked",
        "demo_host_fixed": getattr(client, "base_url", None) == DEMO_REST_URLS.get(
            getattr(settings, "okx_api_region", DEFAULT_DEMO_REGION)
        ),
        "simulated_trading_flag": DEMO_FLAG,
        "public_api": "not_checked",
        "private_api": "not_checked",
        "account_mode": "not_checked",
        "api_permissions": "not_checked",
        "trade_permission": "not_checked",
        "account_snapshot": "not_checked",
        "open_orders": "not_checked",
        "non_quote_holdings": [],
        "blockers": [],
        "private_reads_attempted": False,
        "exchange_writes": 0,
        "orders_enabled": False,
    }
    if not report["demo_host_fixed"]:
        report["blockers"].append("unexpected_rest_host")
        return report
    if not hasattr(client, "_headers") or client._headers("GET", "/api/v5/account/config").get(
        "x-simulated-trading"
    ) != DEMO_FLAG:
        report["blockers"].append("demo_header_not_fixed")
        return report
    try:
        settings.validate_private_read_permission()
    except SafetyError:
        report["blockers"].append("private_read_credentials_missing")
        return report

    try:
        instruments = client.get_instruments()
        tickers = client.get_tickers()
        if not isinstance(instruments, list) or not instruments:
            raise ValueError("public_instruments_empty_or_invalid")
        if not isinstance(tickers, list) or not tickers:
            raise ValueError("public_tickers_empty_or_invalid")
        report["public_api"] = "ok"
        report["public_spot_instruments"] = len(instruments)
        report["public_spot_tickers"] = len(tickers)
    except Exception as exc:
        report["public_api"] = "failed"
        report["blockers"].append("public_api_" + type(exc).__name__)
        return report

    # Mark the private-read attempt only immediately before the first private GET.
    report["private_reads_attempted"] = True
    try:
        config_rows = client.get_account_config()
        if not isinstance(config_rows, list) or len(config_rows) != 1:
            raise ValueError("account_config_missing_or_ambiguous")
        config = config_rows[0]
        report["account_mode"] = (
            "spot" if str(config.get("acctLv")) == "1" else "not_spot_or_unknown"
        )
        raw_permissions = config.get("perm")
        permission_parts = raw_permissions.split(",") if isinstance(raw_permissions, str) else []
        permissions = {value.strip().lower() for value in permission_parts if value.strip()}
        malformed_permissions = (
            not isinstance(raw_permissions, str)
            or not raw_permissions.strip()
            or any(not value.strip() for value in permission_parts)
        )
        if str(config.get("acctLv")) != "1":
            report["blockers"].append("account_not_in_spot_mode")
        if config.get("autoLoan") is not False:
            report["blockers"].append("auto_loan_enabled_or_unknown")
        if config.get("enableSpotBorrow") is not False:
            report["blockers"].append("spot_borrow_enabled_or_unknown")
        permission_blocked = malformed_permissions
        if malformed_permissions:
            report["blockers"].append("api_key_permissions_malformed")
        if "read_only" not in permissions:
            report["blockers"].append("api_key_missing_read_permission")
            permission_blocked = True
        restricted_permissions = {
            "trade": "api_key_has_trade_permission",
            "withdraw": "api_key_has_withdraw_permission",
            "transfer": "api_key_has_transfer_permission",
            "loan": "api_key_has_loan_permission",
            "earn": "api_key_has_earn_permission",
            "onchain": "api_key_has_onchain_permission",
        }
        for permission, blocker in restricted_permissions.items():
            if permission in permissions:
                report["blockers"].append(blocker)
                permission_blocked = True
        if permissions - {"read_only"} - set(restricted_permissions):
            report["blockers"].append("api_key_unrecognized_or_unapproved_permission")
            permission_blocked = True
        report["trade_permission"] = "present" if "trade" in permissions else "absent_read_only"
        report["api_permissions"] = "not_compliant" if permission_blocked else "compliant"

        balances = client.get_balance()
        regular = client.get_pending_orders("SPOT")
        pending_oco = client.get_pending_algo_orders("oco")
        pending_conditional = client.get_pending_algo_orders("conditional")
        for label, rows in (
            ("regular_orders", regular),
            ("oco_orders", pending_oco),
            ("conditional_orders", pending_conditional),
        ):
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("malformed_" + label)
        if not isinstance(balances, list) or len(balances) != 1:
            raise ValueError("balance_snapshot_missing_or_ambiguous")
        details = balances[0].get("details")
        if not isinstance(details, list):
            raise ValueError("balance_details_missing")
        non_quote = []
        for row in details:
            if not isinstance(row, dict) or not row.get("ccy"):
                raise ValueError("malformed_balance_detail")
            try:
                available = Decimal(str(row.get("availBal", "0")))
                frozen = Decimal(str(row.get("frozenBal", "0")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("malformed_balance_amount") from exc
            if (not available.is_finite() or not frozen.is_finite()
                    or available < 0 or frozen < 0):
                raise ValueError("invalid_balance_amount")
            if row["ccy"] != "USDT" and available + frozen > 0:
                non_quote.append(row["ccy"])
        report["non_quote_holdings"] = sorted(set(non_quote))
        report["open_regular_orders"] = len(regular)
        report["open_oco_orders"] = len(pending_oco)
        report["open_conditional_orders"] = len(pending_conditional)
        report["account_snapshot"] = "ok"
        report["open_orders"] = (
            "clear" if not (regular or pending_oco or pending_conditional) else "present"
        )
        report["private_api"] = "ok"
        if regular or pending_oco or pending_conditional:
            report["blockers"].append("open_exchange_orders_present")
        if non_quote:
            report["blockers"].append("non_quote_asset_balances_present")
    except Exception as exc:
        report["private_api"] = "failed"
        report["account_snapshot"] = "incomplete"
        report["blockers"].append("private_api_" + type(exc).__name__)
        return report

    report["status"] = "read_only_demo_connection_verified" if not report["blockers"] else "connected_but_not_ready"
    report["manual_demo_account_attestation_required"] = True
    return report
