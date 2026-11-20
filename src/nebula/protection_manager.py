"""Durable protective OCO lifecycle; all exchange writes remain Demo-gated."""
import hashlib

from .config import SafetyError
from .exchange import OkxError
from .protection import build_spot_oco_exit
from .instruments import SpotInstrument, InstrumentRuleError
from decimal import Decimal


class ProtectionManagerError(RuntimeError):
    pass


ENTRY_TERMINAL = {"filled", "canceled", "mmp_canceled"}


class DemoProtectionManager:
    """Create one OCO only after entry execution is terminal and fill size is final.

    This deliberately waits for the entry order to stop accepting fills. Callers
    must cancel any unfilled entry remainder and reconcile it before preparation.
    """
    def __init__(self, client, state_store, circuit_breaker=None):
        self.client = client
        self.state = state_store
        if circuit_breaker is None and hasattr(client, "settings"):
            from .risk_controls import CircuitBreaker
            circuit_breaker = CircuitBreaker(state_store, client.settings.max_daily_loss_quote)
        self.circuit_breaker = circuit_breaker

    @staticmethod
    def _algo_id(entry_client_order_id: str) -> str:
        return "NBP" + hashlib.sha256(entry_client_order_id.encode()).hexdigest()[:25]

    def cancel_entry_remainder_and_reconcile(self, entry_client_order_id: str) -> dict:
        """Cancel any live entry remainder, then independently verify terminal state.

        A cancel acknowledgement is not a terminal order state. The method never
        creates protection until a subsequent order lookup reports filled/canceled.
        It is safe to call again after restart; it does not resubmit the entry.
        """
        entry = self.state.get_intent(entry_client_order_id)
        if not entry or entry["side"] != "buy":
            raise ProtectionManagerError("A known Spot buy entry is required")
        if entry["status"] in {"rejected", "mmp_canceled"}:
            return {"status": entry["status"], "filled_size": "0"}
        snapshot = self.state.get_order_snapshot(entry_client_order_id)
        if snapshot is None:
            raise ProtectionManagerError("Entry must be reconciled before cancellation")
        current = snapshot["exchange_state"]
        if current in ENTRY_TERMINAL:
            return {"status": current, "filled_size": snapshot["cumulative_fill_size"]}
        if current not in {"live", "partially_filled"}:
            raise ProtectionManagerError(f"Cannot safely cancel entry in state {current!r}")
        order_id = snapshot.get("exchange_order_id") or entry.get("exchange_order_id")
        if not order_id or not str(order_id).isdigit():
            raise ProtectionManagerError("Exchange order ID is missing; refusing blind cancellation")
        cancel_error = None
        try:
            self.client.cancel_order(inst_id=entry["instrument"], order_id=str(order_id))
        except SafetyError:
            # The order gate blocked the request before network I/O; leave state unchanged.
            raise ProtectionManagerError("Entry cancellation is blocked by the Demo order safety gate")
        except Exception as exc:
            # A cancel can race with a fill or time out after exchange acceptance.
            # Always query the order before deciding what happened.
            cancel_error = exc
        try:
            rows = self.client.get_order(entry["instrument"], entry_client_order_id)
        except Exception as exc:
            self.state.record_event("entry_cancel_or_lookup_ambiguous", entry_client_order_id,
                                    {"cancel_error_type": type(cancel_error).__name__ if cancel_error else None,
                                     "lookup_error_type": type(exc).__name__})
            raise ProtectionManagerError("Cancel outcome is not confirmed; keep position locked and reconcile again") from exc
        order = next((row for row in rows if row.get("clOrdId") == entry_client_order_id), None)
        if order is None:
            raise ProtectionManagerError("Order missing after cancel acknowledgement; do not assume canceled")
        try:
            new_snapshot = self.state.save_order_snapshot(entry_client_order_id, order)
        except (ValueError, TypeError) as exc:
            raise ProtectionManagerError("Final fill snapshot is invalid; position remains locked") from exc
        state = str(order.get("state", "unknown"))
        if state not in ENTRY_TERMINAL:
            self.state.update_intent(entry_client_order_id, state, order.get("ordId"))
            self.state.record_event("entry_cancel_not_terminal", entry_client_order_id,
                                    {"exchange_state": state,
                                     "cumulative_fill_size": new_snapshot["cumulative_fill_size"]})
            return {"status": "cancel_pending", "exchange_state": state,
                    "filled_size": new_snapshot["cumulative_fill_size"]}
        self.state.update_intent(entry_client_order_id, state, order.get("ordId"))
        if state in {"canceled", "mmp_canceled"} and Decimal(new_snapshot["cumulative_fill_size"]) == 0:
            self.state.release_terminal_zero_fill_entry_lock(entry_client_order_id)
        self.state.record_event("entry_cancel_finalized", entry_client_order_id,
                                {"exchange_state": state,
                                 "cumulative_fill_size": new_snapshot["cumulative_fill_size"],
                                 "cancel_ack_error_type": type(cancel_error).__name__ if cancel_error else None})
        return {"status": state, "filled_size": new_snapshot["cumulative_fill_size"]}

    def prepare(self, *, entry_client_order_id: str,
                take_profit: str | None = None, stop_loss: str | None = None) -> dict:
        entry = self.state.get_intent(entry_client_order_id)
        snapshot = self.state.get_order_snapshot(entry_client_order_id)
        if not entry or entry["side"] != "buy" or not snapshot:
            raise ProtectionManagerError("A reconciled Spot buy entry and fill snapshot are required")
        if snapshot["exchange_state"] not in ENTRY_TERMINAL:
            return {"status": "waiting_entry_terminal", "filled_size": snapshot["cumulative_fill_size"]}
        if entry["status"] not in ENTRY_TERMINAL:
            raise ProtectionManagerError("Local entry state is not terminal; reconcile it before protection")
        filled = snapshot["cumulative_fill_size"]
        if Decimal(filled) <= 0:
            return {"status": "no_position_to_protect", "filled_size": filled}
        lock = self.state.get_position_lock()
        if not lock or lock["owner_client_order_id"] != entry_client_order_id:
            raise ProtectionManagerError("Entry no longer owns the single-position lock")

        # For exchange-backed entries, protection must use the exact stop/target
        # that passed portfolio authorization before the entry was submitted.
        # Never accept a newly supplied, unreviewed OCO price pair.
        try:
            approved_plan = self.state.get_entry_risk_plan(entry_client_order_id)
        except ValueError as exc:
            raise ProtectionManagerError("Persisted entry risk plan is corrupt; protection is blocked") from exc
        exchange_backed = hasattr(self.client, "settings")
        if approved_plan is None and exchange_backed:
            raise ProtectionManagerError("No persisted portfolio-approved stop/target for this entry")

        # OKX cumulative fill size is gross execution size. If the fee is charged
        # in base currency, the spendable Spot inventory is smaller. Reconcile
        # every entry trade and size OCO from the net base inventory, not accFillSz.
        effective_average_fill = Decimal(str(snapshot["average_fill_price"]))
        gross_filled_quantity = Decimal(str(filled))
        if exchange_backed:
            try:
                self.sync_entry_fills(entry_client_order_id)
                order_id = snapshot.get("exchange_order_id") or entry.get("exchange_order_id")
                fills = self.state.list_fills(entry["instrument"], [str(order_id)])
                if not fills or any(row.get("side") != "buy" for row in fills):
                    raise ValueError("entry fill ledger is empty or contains a non-buy execution")
                ledger_gross = sum(
                    (Decimal(str(row["fill_size"])) for row in fills), Decimal("0")
                )
                if ledger_gross != gross_filled_quantity:
                    raise ValueError("trade-level fills do not reconcile to cumulative order fill")
                from .portfolio import calculate_inventory_basis
                inventory = calculate_inventory_basis(fills, entry["instrument"])
                net_base_quantity = inventory["quantity"]
                effective_average_fill = inventory["average_entry_price"]
                if not net_base_quantity.is_finite() or net_base_quantity <= 0:
                    raise ValueError("net base inventory after fees is not positive")
                filled = format(net_base_quantity, "f")
            except Exception as exc:
                raise ProtectionManagerError(
                    "Entry fills/fees could not be reconciled to net Spot inventory; OCO is blocked"
                ) from exc

        if approved_plan is not None:
            try:
                approved_size = Decimal(str(approved_plan["size"]))
                approved_entry = Decimal(str(approved_plan["entry_price"]))
                approved_tp = Decimal(str(approved_plan["take_profit"]))
                approved_sl = Decimal(str(approved_plan["stop_loss"]))
                if (approved_plan.get("instrument") != entry["instrument"]
                        or not all(value.is_finite() and value > 0 for value in
                                   (approved_size, approved_entry, approved_tp, approved_sl,
                                    effective_average_fill, gross_filled_quantity))
                        or gross_filled_quantity > approved_size
                        or not approved_sl < effective_average_fill < approved_tp):
                    raise ValueError("approved plan does not safely cover actual entry fills")
                if ((take_profit is not None and Decimal(str(take_profit)) != approved_tp)
                        or (stop_loss is not None and Decimal(str(stop_loss)) != approved_sl)):
                    raise ProtectionManagerError(
                        "Requested OCO levels differ from the portfolio-approved entry plan"
                    )
            except ProtectionManagerError:
                raise
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                raise ProtectionManagerError(
                    "Persisted approved entry plan is invalid; protection is blocked"
                ) from exc
            take_profit, stop_loss = str(approved_tp), str(approved_sl)
        if take_profit is None or stop_loss is None:
            raise ProtectionManagerError("Approved take-profit and stop-loss are required")
        try:
            spec = SpotInstrument.from_okx(self.client.get_spot_instrument(entry["instrument"]))
            quantity = Decimal(filled)
            if spec.inst_id != entry["instrument"] or spec.quote_ccy != "USDT" or spec.state != "live":
                raise ProtectionManagerError("Protective exit instrument is not a live USDT Spot market")
            if quantity < spec.min_size or quantity % spec.lot_size != 0:
                raise ProtectionManagerError("Final filled quantity violates current Spot lotSz/minSz")
        except (AttributeError, InstrumentRuleError, ArithmeticError) as exc:
            if isinstance(exc, ProtectionManagerError):
                raise
            raise ProtectionManagerError("Could not validate protective quantity against current instrument rules") from exc
        algo_id = self._algo_id(entry_client_order_id)
        build_spot_oco_exit(instrument=entry["instrument"], filled_size=filled,
                            take_profit=take_profit, stop_loss=stop_loss,
                            algo_client_order_id=algo_id)
        try:
            return self.state.reserve_protection_intent(
                entry_client_order_id=entry_client_order_id, instrument=entry["instrument"],
                algo_client_order_id=algo_id, filled_size=filled,
                take_profit=take_profit, stop_loss=stop_loss)
        except ValueError as exc:
            raise ProtectionManagerError(str(exc)) from exc

    def reconcile(self, entry_client_order_id: str) -> dict:
        """Recover an OCO after restart/timeout by its durable algoClOrdId."""
        intent = self.state.get_protection_intent(entry_client_order_id)
        if not intent:
            raise ProtectionManagerError("No durable OCO intent exists")
        if intent["status"] in {"canceled", "failed"}:
            return intent
        try:
            rows = self.client.get_algo_order(intent["algo_client_order_id"])
        except Exception as exc:
            raise ProtectionManagerError("OCO lookup failed; position remains locked") from exc
        row = next((item for item in rows if item.get("algoClOrdId") == intent["algo_client_order_id"]), None)
        if row is None:
            if intent["status"] in {"active", "triggered", "canceled", "failed"}:
                raise ProtectionManagerError("Previously acknowledged/terminal OCO is missing from lookup; do not downgrade, release or resubmit")
            self.state.update_protection_intent(entry_client_order_id, "submission_unknown",
                                                error_type="AlgoNotFound")
            return self.state.get_protection_intent(entry_client_order_id)
        state = row.get("state")
        mapping = {"live": "active", "pause": "active", "partially_effective": "active",
                   "effective": "triggered", "canceled": "canceled",
                   "order_failed": "failed", "partially_failed": "failed"}
        mapped = mapping.get(state)
        if mapped is None:
            self.state.update_protection_intent(entry_client_order_id, "submission_unknown",
                                                error_type="UnrecognizedAlgoState")
            raise ProtectionManagerError(f"Unrecognized OKX algo state: {state!r}")
        updated = self.state.update_protection_intent(
            entry_client_order_id, mapped, exchange_algo_id=str(row.get("algoId") or "") or None,
            error_type=("ExchangeAlgoFailed" if mapped == "failed" else None))
        if mapped == "triggered":
            child_ids = row.get("ordIdList") or ([row["ordId"]] if row.get("ordId") else [])
            if child_ids or self.state.list_exit_child_orders(entry_client_order_id):
                return self.reconcile_exit_children(entry_client_order_id, child_order_ids=child_ids or None)
        return updated

    def _ingest_order_fills(self, instrument: str, order_id: str, *, page_limit: int = 100,
                            max_pages: int = 10) -> int:
        """Fetch all available fill pages or fail closed instead of using a partial ledger."""
        cursor = None
        inserted = 0
        for page_number in range(max_pages + 1):
            rows = self.client.get_fills_history(instrument, order_id, limit=page_limit, after=cursor)
            if page_number == max_pages and rows:
                raise ProtectionManagerError("Fill history exceeds safe pagination limit; ledger is incomplete")
            for fill in rows:
                inserted += int(self.state.save_fill(fill))
            if len(rows) < page_limit:
                return inserted
            next_cursor = str(rows[-1].get("billId") or "")
            if not next_cursor.isdigit() or next_cursor == cursor:
                raise ProtectionManagerError("Fill history pagination cursor is missing or repeated")
            cursor = next_cursor
        raise ProtectionManagerError("Fill history exceeds safe pagination limit; ledger is incomplete")

    def sync_entry_fills(self, entry_client_order_id: str) -> int:
        """Synchronize entry executions before portfolio inventory is valued."""
        lock = self.state.get_position_lock()
        if not lock or lock.get("owner_client_order_id") != entry_client_order_id:
            raise ProtectionManagerError("Entry does not own the active position lock")
        entry = self.state.get_intent(entry_client_order_id)
        snapshot = self.state.get_order_snapshot(entry_client_order_id)
        if not entry or entry.get("side") != "buy" or not snapshot:
            raise ProtectionManagerError("A reconciled Spot entry and order snapshot are required")
        order_id = snapshot.get("exchange_order_id") or entry.get("exchange_order_id")
        if not order_id or not str(order_id).isdigit():
            raise ProtectionManagerError("Entry exchange order ID is unavailable")
        return self._ingest_order_fills(entry["instrument"], str(order_id))

    def reconcile_exit_children(self, entry_client_order_id: str, *,
                                child_order_ids: list[str] | None = None,
                                dust_limit: str = "0") -> dict:
        """Reconcile spawned sell orders and trade-level fills after OCO trigger.

        This is read-only. It never submits/replaces exits and never releases the
        position lock. A triggered algo is not proof that its child sell filled.
        """
        from .accounting import round_trip_accounting
        intent = self.state.get_protection_intent(entry_client_order_id)
        entry = self.state.get_intent(entry_client_order_id)
        if not intent or not entry:
            raise ProtectionManagerError("Persisted entry and OCO intent are required")
        ids = child_order_ids
        if ids is None:
            ids = [row["exchange_order_id"] for row in self.state.list_exit_child_orders(entry_client_order_id)]
        if not ids:
            return {"status": intent["status"], "child_orders": [],
                    "accounting": None, "position_lock_retained": True}
        try:
            stored = self.state.save_exit_child_orders(entry_client_order_id, intent["instrument"], ids)
            child_rows = []
            for child in stored:
                response = self.client.get_order_by_id(intent["instrument"], child["exchange_order_id"])
                order = next((item for item in response if str(item.get("ordId")) == child["exchange_order_id"]), None)
                if order is None:
                    raise ProtectionManagerError("OCO child order missing from order details; retain position lock")
                child_rows.append(self.state.update_exit_child_order(
                    entry_client_order_id, child["exchange_order_id"], order))
                self._ingest_order_fills(intent["instrument"], child["exchange_order_id"])
            entry_snapshot = self.state.get_order_snapshot(entry_client_order_id)
            order_ids = [row["exchange_order_id"] for row in child_rows]
            if entry_snapshot and entry_snapshot.get("exchange_order_id"):
                self._ingest_order_fills(intent["instrument"], entry_snapshot["exchange_order_id"])
                order_ids.append(entry_snapshot["exchange_order_id"])
            ledger = self.state.list_fills(intent["instrument"], order_ids)
            accounting = round_trip_accounting(ledger, instrument=intent["instrument"], dust_limit=dust_limit)
            self.state.record_event("oco_children_reconciled", entry_client_order_id,
                                    {"child_order_ids": order_ids[:-1] if entry_snapshot and entry_snapshot.get("exchange_order_id") else order_ids,
                                     "fill_count": len(ledger), "accounting_complete": accounting["accounting_complete"]})
            return {"status": "triggered", "child_orders": child_rows,
                    "fill_count": len(ledger), "accounting": accounting,
                    "position_lock_retained": True}
        except ProtectionManagerError:
            raise
        except Exception as exc:
            self.state.record_event("oco_child_reconcile_ambiguous", entry_client_order_id,
                                    {"error_type": type(exc).__name__})
            raise ProtectionManagerError("OCO child/fill reconciliation incomplete; position lock remains held") from exc

    def finalize_position_if_flat(self, entry_client_order_id: str, *,
                                  accounting: dict, dust_limit: str = "0") -> dict:
        """Release the one-position lock only after fill-ledger AND fresh account checks."""
        from decimal import Decimal
        from .reconciliation import DemoAccountReconciler
        entry = self.state.get_intent(entry_client_order_id)
        protection = self.state.get_protection_intent(entry_client_order_id)
        if not entry or not protection:
            raise ProtectionManagerError("Entry and protection intent are required for finalization")
        if accounting.get("accounting_complete") is not True or accounting.get("is_flat_by_fill_ledger") is not True:
            return {"released": False, "reason": "fill_ledger_not_flat_or_incomplete"}
        try:
            snapshot = DemoAccountReconciler(self.client).snapshot(entry["instrument"])
        except Exception as exc:
            self.state.record_event("flat_reconcile_failed", entry_client_order_id,
                                    {"error_type": type(exc).__name__})
            raise ProtectionManagerError("Fresh account reconciliation failed; position lock remains held") from exc
        if not snapshot.is_flat(dust_limit):
            return {"released": False, "reason": "account_not_flat_or_open_orders_remain",
                    "available_base": str(snapshot.available_base),
                    "frozen_base": str(snapshot.frozen_base),
                    "has_open_orders": snapshot.has_open_orders}
        base_balance = snapshot.available_base + snapshot.frozen_base
        # Record realized P&L only after both the fill ledger and independent account
        # snapshot prove flat. The event key makes restart/retry idempotent.
        if self.circuit_breaker is not None:
            pnl = accounting.get("realized_pnl_quote")
            if pnl is None:
                return {"released": False, "reason": "realized_pnl_unavailable"}
            try:
                breaker_result = self.circuit_breaker.record_closed_trade(entry_client_order_id, pnl)
            except Exception as exc:
                self.state.record_event("daily_pnl_record_failed", entry_client_order_id,
                                         {"error_type": type(exc).__name__})
                raise ProtectionManagerError("Daily P&L could not be durably recorded; position lock remains held") from exc
        else:
            breaker_result = None
        try:
            released = self.state.release_position_lock_after_flat_check(
                position_client_order_id=entry_client_order_id,
                base_balance=str(base_balance), dust_limit=dust_limit,
                open_orders_checked=not snapshot.has_open_orders)
        except (ValueError, RuntimeError) as exc:
            raise ProtectionManagerError("Local execution state is not safe to release; position lock remains held") from exc
        return {"released": released, "reason": "verified_flat" if released else "position_lock_not_owned",
                "base_balance": str(base_balance), "has_open_orders": snapshot.has_open_orders,
                "daily_risk": breaker_result}

    def submit_reserved(self, entry_client_order_id: str) -> dict:
        intent = self.state.get_protection_intent(entry_client_order_id)
        if not intent:
            raise ProtectionManagerError("No durable OCO intent exists")
        if intent["status"] != "reserved":
            raise ProtectionManagerError(f"OCO is {intent['status']}; never resubmit automatically")
        # Durable state is changed before the network call. A crash or timeout
        # leaves an ambiguous intent that must be reconciled by algoClOrdId.
        self.state.update_protection_intent(entry_client_order_id, "submitting")
        try:
            response = self.client.place_spot_oco_exit(
                inst_id=intent["instrument"], size=intent["filled_size"],
                take_profit=intent["take_profit"], stop_loss=intent["stop_loss"],
                algo_client_order_id=intent["algo_client_order_id"])
        except (SafetyError, ValueError) as exc:
            # These errors are raised by local safety/parameter validation before
            # the HTTP request. Keep the immutable OCO intent reserved so it can
            # be retried manually after the gate or input is corrected. Network
            # errors remain submission_unknown and are never retried blindly.
            self.state.defer_protection_intent(
                entry_client_order_id, error_type=type(exc).__name__)
            raise
        except OkxError as exc:
            definitive = str(exc).startswith("OKX OCO rejected")
            status = "failed" if definitive else "submission_unknown"
            self.state.update_protection_intent(entry_client_order_id, status, error_type=type(exc).__name__)
            raise ProtectionManagerError("OCO submission failed; reconcile by algoClOrdId before any further action") from exc
        except Exception as exc:
            self.state.update_protection_intent(entry_client_order_id, "submission_unknown", error_type=type(exc).__name__)
            raise ProtectionManagerError("OCO outcome may be unknown; reconcile before retry") from exc
        if not response or not response[0].get("algoId") or response[0].get("sCode", "0") != "0":
            self.state.update_protection_intent(entry_client_order_id, "submission_unknown")
            raise ProtectionManagerError("OCO response is incomplete; reconcile by algoClOrdId")
        return self.state.update_protection_intent(entry_client_order_id, "active",
                                                   exchange_algo_id=str(response[0]["algoId"]))
