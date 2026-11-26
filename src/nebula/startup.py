"""Read-only startup recovery gate for OKX Spot Demo.

This module reconciles durable local intents against authenticated Demo reads.
It never submits, cancels, replaces, or releases an exchange order/position lock.
"""
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path


_CCY_PATTERN = re.compile(r"^[A-Z0-9]{2,15}$")


class StartupRecoveryError(RuntimeError):
    pass


class StartupRecovery:
    ACTIVE_INTENT_STATES = {
        "reserved", "submitting", "submission_unknown", "acknowledged",
        "live", "partially_filled",
    }

    def __init__(self, client, state_store, *, orders=None, protection=None):
        self.client = client
        self.state = state_store
        if orders is None:
            from .orders import DemoOrderManager
            orders = DemoOrderManager(client, state_store)
        if protection is None:
            from .protection_manager import DemoProtectionManager
            protection = DemoProtectionManager(client, state_store)
        self.orders = orders
        self.protection = protection
        self.entry_permit = None

    @staticmethod
    def _positive(value, field):
        try:
            number = Decimal(str(value or "0"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise StartupRecoveryError(f"Invalid {field} in account snapshot") from exc
        if not number.is_finite() or number < 0:
            raise StartupRecoveryError(f"Invalid {field} in account snapshot")
        return number

    @staticmethod
    def _balance_amount(value, field):
        # Strict balance parsing without truthiness fallback, matching the
        # reconciliation standard: missing/invalid amounts never read as 0.
        if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
            raise StartupRecoveryError(f"Invalid {field} in account snapshot")
        if isinstance(value, str) and not value.strip():
            raise StartupRecoveryError(f"Invalid {field} in account snapshot")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise StartupRecoveryError(f"Invalid {field} in account snapshot") from exc
        if not number.is_finite() or number < 0:
            raise StartupRecoveryError(f"Invalid {field} in account snapshot")
        return number

    @staticmethod
    def _liability(value):
        # Strict liability parsing without truthiness fallback: False, null,
        # containers, and empty strings are not valid API amounts.
        if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
            raise StartupRecoveryError("Invalid liability in account snapshot")
        if isinstance(value, str) and not value.strip():
            raise StartupRecoveryError("Invalid liability in account snapshot")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise StartupRecoveryError("Invalid liability in account snapshot") from exc
        if not number.is_finite() or number < 0:
            raise StartupRecoveryError("Invalid liability in account snapshot")
        return number

    def validate_local_database(self):
        """Read-only SQLite integrity and cross-table invariant audit."""
        path = str(self.state.path)
        if path == ":memory:":
            # A separate read-only connection cannot inspect a private in-memory DB.
            with self.state._connect() as db:
                integrity = db.execute("PRAGMA integrity_check").fetchall()
                foreign = db.execute("PRAGMA foreign_key_check").fetchall()
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                return self._validate_database_connection(db, integrity, foreign, tables)
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=5) as db:
                db.row_factory = sqlite3.Row
                integrity = db.execute("PRAGMA integrity_check").fetchall()
                foreign = db.execute("PRAGMA foreign_key_check").fetchall()
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                return self._validate_database_connection(db, integrity, foreign, tables)
        except (sqlite3.Error, OSError, ValueError) as exc:
            return {"ok": False, "errors": [f"database_unreadable:{type(exc).__name__}"]}

    def _validate_database_connection(self, db, integrity, foreign, tables):
        errors = []
        required = {
            "intents", "order_snapshots", "position_lock", "protection_intents",
            "exit_child_orders", "audit_events", "fills",
        }
        missing = sorted(required - tables)
        if missing:
            errors.append("missing_tables:" + ",".join(missing))
        if not integrity or any(row[0] != "ok" for row in integrity):
            errors.append("sqlite_integrity_check_failed")
        if foreign:
            errors.append("foreign_key_check_failed")
        if errors:
            return {"ok": False, "errors": errors}
        # Ledger continuity: prior trading history without equity history
        # means the drawdown high-water mark was lost; fail closed.
        history = db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] + \
            db.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        if history:
            if not {"daily_equity_baselines", "daily_equity_marks"}.issubset(tables):
                errors.append("equity_ledger_history_missing")
                return {"ok": False, "errors": errors}
            baselines = db.execute(
                "SELECT trading_day,opening_timestamp_ms FROM daily_equity_baselines").fetchall()
            if not baselines:
                errors.append("equity_ledger_history_missing")
                return {"ok": False, "errors": errors}
            for baseline in baselines:
                opening_mark = db.execute(
                    "SELECT COUNT(*) FROM daily_equity_marks WHERE trading_day=? AND timestamp_ms=?",
                    (baseline[0], baseline[1])).fetchone()[0]
                if not opening_mark:
                    errors.append("equity_ledger_history_incomplete")
                    return {"ok": False, "errors": errors}

        lock_rows = db.execute("SELECT * FROM position_lock").fetchall()
        if len(lock_rows) > 1:
            errors.append("multiple_position_locks")
        lock = dict(lock_rows[0]) if lock_rows else None

        intents = db.execute("SELECT * FROM intents").fetchall()
        by_id = {row["client_order_id"]: dict(row) for row in intents}
        snapshots = {
            row["client_order_id"]: dict(row)
            for row in db.execute("SELECT * FROM order_snapshots").fetchall()
        }
        if lock:
            owner = by_id.get(lock["owner_client_order_id"])
            if (not owner or owner["side"] != "buy"
                    or owner["instrument"] != lock["instrument"]):
                errors.append("position_lock_owner_mismatch")

        for cid, intent in by_id.items():
            if intent["side"] != "buy":
                continue
            snap = snapshots.get(cid)
            if snap:
                try:
                    filled = self._positive(snap["cumulative_fill_size"], "local cumulative fill")
                except StartupRecoveryError:
                    errors.append("invalid_local_cumulative_fill:" + cid)
                    continue
                if filled > 0 and (not lock or lock["owner_client_order_id"] != cid):
                    released = db.execute(
                        "SELECT 1 FROM audit_events WHERE client_order_id=? "
                        "AND event_type='position_lock_released' LIMIT 1", (cid,)
                    ).fetchone()
                    if not released:
                        errors.append("filled_entry_without_lock_or_release:" + cid)
            elif intent["status"] in self.ACTIVE_INTENT_STATES:
                if not lock or lock["owner_client_order_id"] != cid:
                    errors.append("active_entry_without_position_lock:" + cid)

        fills = db.execute(
            "SELECT instrument,exchange_order_id,client_order_id,side FROM fills"
        ).fetchall()
        for fill in fills:
            intent_matches = db.execute(
                "SELECT client_order_id,instrument,side FROM intents WHERE exchange_order_id=?",
                (fill["exchange_order_id"],),
            ).fetchall()
            child_matches = db.execute(
                "SELECT instrument FROM exit_child_orders WHERE exchange_order_id=?",
                (fill["exchange_order_id"],),
            ).fetchall()
            if len(intent_matches) + len(child_matches) != 1:
                errors.append("fill_order_link_missing_or_ambiguous:" + fill["exchange_order_id"])
                continue
            if intent_matches:
                parent = intent_matches[0]
                if (parent["instrument"] != fill["instrument"]
                        or parent["side"] != fill["side"]
                        or (fill["client_order_id"]
                            and fill["client_order_id"] != parent["client_order_id"])):
                    errors.append("fill_order_link_mismatch:" + fill["exchange_order_id"])
            else:
                child = child_matches[0]
                if child["instrument"] != fill["instrument"] or fill["side"] != "sell":
                    errors.append("fill_child_order_link_mismatch:" + fill["exchange_order_id"])

        protections = db.execute(
            "SELECT entry_client_order_id,instrument FROM protection_intents"
        ).fetchall()
        for item in protections:
            parent = by_id.get(item["entry_client_order_id"])
            if not parent or parent["side"] != "buy" or parent["instrument"] != item["instrument"]:
                errors.append("protection_parent_mismatch:" + item["entry_client_order_id"])

        children = db.execute(
            "SELECT entry_client_order_id,instrument FROM exit_child_orders"
        ).fetchall()
        for item in children:
            parent = db.execute(
                "SELECT instrument FROM protection_intents WHERE entry_client_order_id=?",
                (item["entry_client_order_id"],)
            ).fetchone()
            if not parent or parent["instrument"] != item["instrument"]:
                errors.append("exit_child_parent_mismatch:" + item["entry_client_order_id"])

        return {
            "ok": not errors,
            "errors": errors,
            "position_lock_present": bool(lock),
            "intent_count": len(intents),
            "order_snapshot_count": len(snapshots),
            "protection_intent_count": len(protections),
            "exit_child_order_count": len(children),
        }

    def _account_without_local_position(self):
        account_config = self.client.get_account_config()
        if not isinstance(account_config, list) or len(account_config) != 1:
            raise StartupRecoveryError("Account configuration snapshot is missing or ambiguous")
        config = account_config[0]
        if str(config.get("acctLv")) != "1":
            raise StartupRecoveryError("OKX account is not in Spot account mode")
        if config.get("autoLoan") is not False:
            raise StartupRecoveryError("Auto-loan is enabled or its state is unknown")
        # The spot-borrowing feature flag is enabled by OKX in Spot mode and
        # cannot be switched off; actual borrowing state is enforced instead
        # (auto-repay off here, zero liabilities in the balance check below).
        spot_borrow = config.get("enableSpotBorrow")
        if spot_borrow is True:
            if config.get("spotBorrowAutoRepay") is not False:
                raise StartupRecoveryError("Spot borrow auto-repay is enabled or its state is unknown")
        elif spot_borrow is not False:
            raise StartupRecoveryError("Spot borrow state is unknown")
        raw_permissions = config.get("perm")
        permission_parts = raw_permissions.split(",") if isinstance(raw_permissions, str) else []
        permissions = {part.strip().lower() for part in permission_parts if part.strip()}
        malformed_permissions = (
            not isinstance(raw_permissions, str)
            or not raw_permissions.strip()
            or any(not part.strip() for part in permission_parts)
        )
        if malformed_permissions or permissions != {"read_only", "trade"}:
            raise StartupRecoveryError(
                "API key must have exactly read_only and trade permissions, with no additional or malformed scopes"
            )
        balances = self.client.get_balance()
        regular = self.client.get_pending_orders("SPOT")
        algos = [
            *self.client.get_pending_algo_orders("oco"),
            *self.client.get_pending_algo_orders("conditional"),
        ]
        if len(balances) != 1 or not isinstance(balances[0].get("details"), list):
            raise StartupRecoveryError("Account balance snapshot is malformed")
        holdings = []
        seen_currencies = set()
        for row in balances[0]["details"]:
            if not isinstance(row, dict):
                raise StartupRecoveryError("Account balance row is malformed")
            if not isinstance(row.get("ccy"), str) or not _CCY_PATTERN.fullmatch(row["ccy"]):
                raise StartupRecoveryError("Account balance row has a non-canonical currency")
            if row["ccy"] in seen_currencies:
                raise StartupRecoveryError("Account balance response has duplicate currencies")
            seen_currencies.add(row["ccy"])
            for field in ("availBal", "frozenBal"):
                if field not in row:
                    raise StartupRecoveryError(f"Account balance {field} is missing")
            amount = self._balance_amount(row.get("availBal"), "available balance")
            frozen = self._balance_amount(row.get("frozenBal"), "frozen balance")
            if "liab" not in row:
                raise StartupRecoveryError("Account balance liability is missing")
            if self._liability(row.get("liab")) > 0:
                raise StartupRecoveryError("Spot borrowings are present in the account balance")
            if row["ccy"] != "USDT" and amount + frozen > 0:
                holdings.append(row["ccy"])
        return {
            "account_uid": config.get("uid"),
            "non_quote_holdings": sorted(set(holdings)),
            "open_regular_orders": len(regular),
            "open_algo_orders": len(algos),
        }

    def _reject_untracked_other_assets(self, base_currency):
        balances = self.client.get_balance()
        if len(balances) != 1 or not isinstance(balances[0].get("details"), list):
            raise StartupRecoveryError("Account balance snapshot is malformed")
        seen_currencies = set()
        for row in balances[0]["details"]:
            if not isinstance(row, dict):
                raise StartupRecoveryError("Account balance row is malformed")
            if not isinstance(row.get("ccy"), str) or not _CCY_PATTERN.fullmatch(row["ccy"]):
                raise StartupRecoveryError("Account balance row has a non-canonical currency")
            if row["ccy"] in seen_currencies:
                raise StartupRecoveryError("Account balance response has duplicate currencies")
            seen_currencies.add(row["ccy"])
            for field in ("availBal", "frozenBal"):
                if field not in row:
                    raise StartupRecoveryError(f"Account balance {field} is missing")
            total = (self._balance_amount(row.get("availBal"), "available balance")
                     + self._balance_amount(row.get("frozenBal"), "frozen balance"))
            if row["ccy"] not in {"USDT", base_currency} and total > 0:
                raise StartupRecoveryError("Untracked non-position asset balance exists")

    def run(self):
        """Reconcile local intents and account state without exchange writes."""
        local = self.validate_local_database()
        if not local["ok"]:
            return {
                "status": "blocked_local_state_integrity",
                "entry_authorized": False,
                "local_database": local,
                "blockers": local["errors"],
                "exchange_writes": 0,
            }

        try:
            outcomes = self.orders.reconcile_all()
            unresolved = self.state.list_unresolved_intents()
            if unresolved:
                return {
                    "status": "blocked_unresolved_orders",
                    "entry_authorized": False,
                    "local_database": local,
                    "reconciled_intents": len(outcomes),
                    "unresolved_client_order_ids": [
                        item["client_order_id"] for item in unresolved
                    ],
                    "exchange_writes": 0,
                }

            lock = self.state.get_position_lock()
            if not lock:
                account = self._account_without_local_position()
                blockers = []
                if account["non_quote_holdings"]:
                    blockers.append("untracked_non_quote_asset_balances")
                if account["open_regular_orders"] or account["open_algo_orders"]:
                    blockers.append("untracked_open_exchange_orders")
                exclusive = bool(getattr(getattr(self.client, "settings", None),
                                         "demo_account_exclusive", False))
                if not exclusive:
                    blockers.append("dedicated_demo_account_not_attested")
                if not blockers:
                    from .entry_gate import issue_startup_entry_permit
                    self.entry_permit = issue_startup_entry_permit(
                        self.client, self.state, account.get("account_uid")
                    )
                return {
                    "status": "clean_no_local_position" if not blockers else "blocked_account_mismatch",
                    "entry_authorized": False,
                    "entry_permit_issued": self.entry_permit is not None,
                    "local_database": local,
                    "account": account,
                    "reconciled_intents": len(outcomes),
                    "blockers": blockers,
                    "exchange_writes": 0,
                }

            owner = lock["owner_client_order_id"]
            entry = self.state.get_intent(owner)
            snapshot = self.state.get_order_snapshot(owner)
            if not entry or entry.get("side") != "buy" or not snapshot:
                raise StartupRecoveryError("Position lock has no reconciled buy entry snapshot")
            if self._positive(snapshot["cumulative_fill_size"], "entry fill") <= 0:
                raise StartupRecoveryError("Position lock exists without a confirmed positive entry fill")
            self.protection.sync_entry_fills(owner)
            protection_intent = self.state.get_protection_intent(owner)
            if not protection_intent:
                raise StartupRecoveryError("Filled position has no durable protective OCO intent")

            protection_result = self.protection.reconcile(owner)
            protection_status = protection_result.get("status")
            if protection_status == "active":
                from .reconciliation import DemoAccountReconciler
                account = DemoAccountReconciler(self.client).snapshot(entry["instrument"])
                expected_algo = protection_intent["algo_client_order_id"]
                matching_algos = [
                    row for row in account.open_algo_orders
                    if row.get("algoClOrdId") == expected_algo
                ]
                all_regular = self.client.get_pending_orders("SPOT")
                all_algos = [
                    *self.client.get_pending_algo_orders("oco"),
                    *self.client.get_pending_algo_orders("conditional"),
                ]
                if (len(matching_algos) != 1 or account.open_orders or all_regular
                        or len(all_algos) != 1
                        or all_algos[0].get("algoClOrdId") != expected_algo):
                    raise StartupRecoveryError("Active OCO does not match the fresh account-wide open-order snapshot")
                self._reject_untracked_other_assets(account.base_currency)
                if account.available_base + account.frozen_base <= 0:
                    raise StartupRecoveryError("Active OCO exists but base inventory is zero")
                return {
                    "status": "position_reconciled_protection_active",
                    "entry_authorized": False,
                    "local_database": local,
                    "position_client_order_id": owner,
                    "protection": protection_status,
                    "account_base_inventory": str(account.available_base + account.frozen_base),
                    "borrowings_present": account.borrowings_present,
                    "exchange_writes": 0,
                    "blockers": ["existing_position_requires_protective_monitoring"]
                    + (["spot_borrowings_present"] if account.borrowings_present else []),
                }

            if protection_status in {"triggered", "partially_failed"}:
                accounting = protection_result.get("accounting")
                if not isinstance(accounting, dict) or accounting.get("accounting_complete") is not True:
                    raise StartupRecoveryError("Triggered OCO child/fill ledger is incomplete")
                from .reconciliation import DemoAccountReconciler
                account = DemoAccountReconciler(self.client).snapshot(entry["instrument"])
                expected = self._positive(accounting.get("base_inventory_delta"), "ledger base inventory")
                actual = account.available_base + account.frozen_base
                if expected != actual:
                    raise StartupRecoveryError("Local fill ledger and fresh base balance disagree")
                child_ids = {
                    row["exchange_order_id"]
                    for row in self.state.list_exit_child_orders(owner)
                }
                all_regular = self.client.get_pending_orders("SPOT")
                all_algos = [
                    *self.client.get_pending_algo_orders("oco"),
                    *self.client.get_pending_algo_orders("conditional"),
                ]
                if any(str(row.get("ordId")) not in child_ids for row in all_regular):
                    raise StartupRecoveryError("Untracked regular exchange order exists during OCO exit recovery")
                if any(row.get("algoClOrdId") != protection_intent["algo_client_order_id"]
                       for row in all_algos):
                    raise StartupRecoveryError("Untracked algo order exists during OCO exit recovery")
                self._reject_untracked_other_assets(account.base_currency)
                return {
                    "status": "position_reconciled_exit_in_progress",
                    "entry_authorized": False,
                    "local_database": local,
                    "position_client_order_id": owner,
                    "protection": protection_status,
                    "account_base_inventory": str(actual),
                    "accounting_complete": True,
                    "borrowings_present": account.borrowings_present,
                    "exchange_writes": 0,
                    "blockers": ["existing_position_requires_exit_finalization"]
                    + (["spot_borrowings_present"] if account.borrowings_present else []),
                }

            raise StartupRecoveryError(
                f"Protective OCO is not confirmed active or triggered (state={protection_status!r})"
            )
        except Exception as exc:
            self.state.record_event(
                "startup_recovery_blocked", None,
                {"error_type": type(exc).__name__},
            )
            return {
                "status": "blocked_reconciliation_failure",
                "entry_authorized": False,
                "local_database": local,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "exchange_writes": 0,
            }
