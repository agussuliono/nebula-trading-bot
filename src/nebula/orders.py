"""Fail-closed order lifecycle coordinator for OKX Spot Demo."""
from .config import SafetyError
from .exchange import OkxError
from .instruments import SpotInstrument


TERMINAL_STATES = {"filled", "canceled", "rejected", "mmp_canceled"}
TRACKED_STATES = {"live", "partially_filled", "filled", "canceled", "mmp_canceled"}


class OrderManagerError(RuntimeError):
    pass


class DemoOrderManager:
    """Coordinates durable intent reservation, submission, and reconciliation.

    A client order ID is never resubmitted by this manager. If the response is
    ambiguous or the order cannot be found, the intent remains blocked until a
    human resolves it; absence from the pending list is not proof of rejection.
    """
    def __init__(self, client, state_store, circuit_breaker=None, risk_authorizer=None,
                 startup_permit=None):
        self.client = client
        self.state = state_store
        self.risk_authorizer = risk_authorizer
        self.startup_permit = startup_permit
        if hasattr(client, "install_startup_entry_permit") and startup_permit is not None:
            client.install_startup_entry_permit(startup_permit, state_store)
        if (circuit_breaker is None and hasattr(client, "settings")
                and float(getattr(client.settings, "max_daily_loss_quote", 0)) > 0):
            from .risk_controls import CircuitBreaker
            circuit_breaker = CircuitBreaker(state_store, client.settings.max_daily_loss_quote)
        self.circuit_breaker = circuit_breaker

    def submit_limit(self, *, inst_id: str, side: str, size: str, price: str,
                     client_order_id: str, candle_ts: int,
                     position_client_order_id: str | None = None) -> dict:
        # The exchange adapter independently enforces the immutable Demo header
        # and the Settings order gate. This manager adds durable idempotency.
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if self.circuit_breaker is not None:
            self.circuit_breaker.check_order_allowed(side)
        # Always use current exchange precision rules. No implicit rounding without
        # the exchange's current tickSz/lotSz/minSz metadata.
        raw_spec = self.client.get_spot_instrument(inst_id)
        spec = SpotInstrument.from_okx(raw_spec)
        if spec.inst_id != inst_id or spec.quote_ccy != "USDT":
            raise OrderManagerError("V1 only permits the requested USDT-quoted Spot instrument")
        size, price = spec.normalize_order(side, size, price)
        approved_risk_plan = None
        # A real OKX client must be paired with a portfolio/risk authorization
        # callback before any new long exposure can be reserved or submitted.
        # Fake clients used by offline unit tests intentionally have no settings.
        if side == "buy" and hasattr(self.client, "settings"):
            # A generic callback can fabricate an approval and bypass portfolio
            # controls. Real exchange clients must use the concrete fail-closed
            # authorizer that owns the private snapshot and risk-evaluation path.
            from .risk_authorizer import PortfolioRiskAuthorizer
            if type(self.risk_authorizer) is not PortfolioRiskAuthorizer:
                raise OrderManagerError(
                    "No integrated PortfolioRiskAuthorizer; refusing Spot entry"
                )
            try:
                approval = self.risk_authorizer(
                    instrument=inst_id, requested_size=size, requested_price=price,
                    candle_ts=candle_ts,
                )
            except Exception as exc:
                raise OrderManagerError(
                    "Portfolio risk authorization failed; refusing Spot entry"
                ) from exc
            if not isinstance(approval, dict) or approval.get("entry_allowed") is not True:
                raise OrderManagerError("Portfolio risk authorization denied Spot entry")
            approved_plan = approval.get("planned_entry")
            if not isinstance(approved_plan, dict):
                raise OrderManagerError("Risk authorization has no approved entry plan")
            if (str(approved_plan.get("instrument")) != str(inst_id)
                    or str(approved_plan.get("size")) != str(size)
                    or str(approved_plan.get("entry_price")) != str(price)):
                raise OrderManagerError("Approved risk plan does not match requested order")
            try:
                from decimal import Decimal, InvalidOperation
                approved_risk = Decimal(str(approved_plan["risk_to_stop_quote"]))
                stop = Decimal(str(approved_plan["stop_loss"]))
                target = Decimal(str(approved_plan["take_profit"]))
                entry = Decimal(price)
                settings = self.client.settings
                if (not all(value.is_finite() and value > 0 for value in (approved_risk, stop, target))
                        or not stop < entry < target
                        or approved_risk > Decimal(str(settings.max_risk_quote))):
                    raise ValueError("approved risk or protective levels are invalid")
                approved_risk_plan = {
                    "instrument": inst_id, "size": size, "entry_price": price,
                    "stop_loss": str(stop), "take_profit": str(target),
                    "risk_to_stop_quote": str(approved_risk),
                }
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise OrderManagerError("Approved risk amount or protective levels are invalid") from exc
        # Production exchange-backed buys require a fresh permit from a
        # clean, read-only StartupRecovery run. It is consumed before local
        # reservation, so even a crash cannot reuse it for a second entry.
        if side == "buy" and hasattr(self.client, "install_startup_entry_permit"):
            if self.startup_permit is None:
                raise OrderManagerError("No clean StartupRecovery permit; refusing Spot entry")
            try:
                self.startup_permit.consume(
                    self.client, self.state, instrument=inst_id, size=size,
                    price=price, client_order_id=client_order_id,
                )
            except Exception as exc:
                raise OrderManagerError("Startup recovery permit is invalid or stale") from exc

        try:
            if side == "buy":
                reserved = self.state.reserve_entry_intent(
                    client_order_id, inst_id, candle_ts, side,
                    risk_plan=approved_risk_plan,
                )
            else:
                if not position_client_order_id:
                    raise OrderManagerError("Spot sell requires the active position_client_order_id")
                reserved = self.state.reserve_exit_intent(client_order_id, inst_id, candle_ts, side,
                                                          position_client_order_id)
        except RuntimeError as exc:
            raise OrderManagerError(str(exc)) from exc
        if not reserved:
            existing = self.state.get_intent(client_order_id)
            raise OrderManagerError(
                f"Duplicate or previously attempted client_order_id; not resubmitted (status={existing['status'] if existing else 'unknown'})"
            )
        self.state.record_event("order_intent_reserved", client_order_id,
                                {"inst_id": inst_id, "side": side, "candle_ts": candle_ts})
        self.state.update_intent(client_order_id, "submitting")
        try:
            response = self.client.place_spot_limit(
                inst_id=inst_id, side=side, size=size, price=price,
                client_order_id=client_order_id,
            )
        except (ValueError, SafetyError) as exc:
            # Local validation and the explicit order gate fail before HTTP.
            self.state.update_intent(client_order_id, "rejected")
            if side == "buy":
                self.state.release_rejected_entry_lock(client_order_id)
            self.state.record_event("order_submission_blocked", client_order_id,
                                    {"error_type": type(exc).__name__})
            raise
        except OkxError as exc:
            message = str(exc)
            # Only adapter's explicit per-order sCode rejection is definitive.
            status = "rejected" if message.startswith("OKX order rejected") else "submission_unknown"
            self.state.update_intent(client_order_id, status)
            if side == "buy" and status == "rejected":
                self.state.release_rejected_entry_lock(client_order_id)
            self.state.record_event("order_submission_error", client_order_id,
                                    {"classification": status, "error_type": type(exc).__name__})
            raise
        except Exception as exc:
            self.state.update_intent(client_order_id, "submission_unknown")
            self.state.record_event("order_submission_error", client_order_id,
                                    {"classification": "submission_unknown", "error_type": type(exc).__name__})
            raise

        if not response or not response[0].get("ordId"):
            self.state.update_intent(client_order_id, "submission_unknown")
            self.state.record_event("order_ack_missing_id", client_order_id)
            raise OrderManagerError("Order response lacked ordId; reconcile by clOrdId; do not retry")
        ord_id = str(response[0]["ordId"])
        self.state.update_intent(client_order_id, "acknowledged", ord_id)
        self.state.record_event("order_acknowledged", client_order_id, {"ord_id": ord_id})
        return {"client_order_id": client_order_id, "exchange_order_id": ord_id, "status": "acknowledged"}

    def reconcile(self, client_order_id: str) -> dict:
        intent = self.state.get_intent(client_order_id)
        if not intent:
            raise OrderManagerError("Unknown client_order_id")
        if intent["status"] in TERMINAL_STATES:
            return intent
        try:
            result = self.client.get_order(intent["instrument"], client_order_id)
        except Exception as exc:
            self.state.record_event("reconcile_error", client_order_id,
                                    {"error_type": type(exc).__name__})
            raise OrderManagerError("Order reconciliation failed; intent remains blocked") from exc

        order = result[0] if result else None
        if order is None:
            # Pending list is a useful secondary lookup, but absence from it is
            # not enough to infer rejection: the order may already be terminal.
            try:
                pending = self.client.get_pending_orders("SPOT")
            except Exception as exc:
                self.state.record_event("pending_lookup_error", client_order_id,
                                        {"error_type": type(exc).__name__})
                raise OrderManagerError("Pending-order lookup failed; intent remains blocked") from exc
            order = next((item for item in pending if item.get("clOrdId") == client_order_id), None)

        if order is None:
            self.state.update_intent(client_order_id, "submission_unknown")
            self.state.record_event("order_not_found_still_blocked", client_order_id)
            return self.state.get_intent(client_order_id)

        status = order.get("state")
        try:
            snapshot = self.state.save_order_snapshot(client_order_id, order)
        except (ValueError, TypeError) as exc:
            self.state.record_event("order_snapshot_invalid", client_order_id,
                                    {"error_type": type(exc).__name__})
            raise OrderManagerError("Exchange fill snapshot failed validation; order remains blocked") from exc
        if status not in TRACKED_STATES:
            self.state.update_intent(client_order_id, "submission_unknown", order.get("ordId"))
            self.state.record_event("order_state_unrecognized", client_order_id,
                                    {"exchange_state": status})
            return self.state.get_intent(client_order_id)

        self.state.update_intent(client_order_id, status, order.get("ordId"))
        if (intent["side"] == "buy"
                and status in {"canceled", "mmp_canceled"}
                and snapshot["cumulative_fill_size"] == "0"):
            # This is an exchange-confirmed terminal zero-fill, not a local
            # rejection. Use the state transition that validates both the
            # persisted intent and terminal exchange snapshot before unlocking.
            self.state.release_terminal_zero_fill_entry_lock(client_order_id)
        self.state.record_event("order_reconciled", client_order_id,
                                {"state": status, "acc_fill_sz": snapshot["cumulative_fill_size"],
                                 "avg_px": snapshot["average_fill_price"], "fee": snapshot["fee"],
                                 "fee_currency": snapshot["fee_currency"]})
        return self.state.get_intent(client_order_id)

    def reconcile_all(self) -> list[dict]:
        outcomes = []
        for intent in self.state.list_unresolved_intents():
            outcomes.append(self.reconcile(intent["client_order_id"]))
        return outcomes
