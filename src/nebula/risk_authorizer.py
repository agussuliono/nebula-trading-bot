"""Live-snapshot portfolio authorization for Demo Spot entries.

This module authorizes only an already-sized order whose plan includes a closed
buy signal and precomputed stop/target. It never submits an order.
"""
from decimal import Decimal
import hashlib

from .portfolio import PortfolioPolicy, PortfolioRiskEngine
from .risk_controls import CircuitBreaker
from .account_ledger import AccountLedger
from .bill_reconciliation import reconcile_spot_bills, BillReconciliationError


class RiskAuthorizationError(RuntimeError):
    pass


class PortfolioRiskAuthorizer:
    """Fail-closed adapter between order sizing and the read-only portfolio engine."""

    def __init__(self, client, state_store, settings, *, plan_provider, clock_ms=None,
                 bar_ms=3_600_000, max_candle_age_intervals=2):
        if not callable(plan_provider):
            raise ValueError("plan_provider must provide an explicit stop/target plan")
        self.client = client
        self.state = state_store
        self.settings = settings
        if isinstance(bar_ms, bool) or not isinstance(bar_ms, int) or bar_ms <= 0:
            raise ValueError("bar_ms must be a positive integer")
        if (isinstance(max_candle_age_intervals, bool)
                or not isinstance(max_candle_age_intervals, int)
                or max_candle_age_intervals < 1):
            raise ValueError("max_candle_age_intervals must be a positive integer")
        if hasattr(client, "settings") and client.settings != settings:
            raise ValueError("authorizer settings must exactly match the exchange client settings")
        self.plan_provider = plan_provider
        self.clock_ms = clock_ms or (lambda: __import__("time").time_ns() // 1_000_000)
        self.bar_ms = bar_ms
        self.max_candle_age_intervals = max_candle_age_intervals

    def __call__(self, *, instrument: str, requested_size: str,
                 requested_price: str, candle_ts: int) -> dict:
        """Order-manager authorization; still requires the Demo write gate."""
        return self._authorize(
            instrument=instrument, requested_size=requested_size,
            requested_price=requested_price, candle_ts=candle_ts,
            plan_override=None, require_order_gate=True,
        )

    def preflight_candidate(self, candidate_plan: dict) -> dict:
        """Read-only portfolio preflight; never requires or enables exchange writes."""
        candidate = CandidateRiskPreflight._validate_candidate(candidate_plan)
        return self._authorize(
            instrument=candidate["instrument"],
            requested_size=candidate["size"],
            requested_price=candidate["entry_price"],
            candle_ts=candidate["candle_ts"],
            plan_override=candidate,
            require_order_gate=False,
        )

    def _daily_account_pnl(self, *, account_config, balance, tickers,
                           regular, oco, conditional, lock, unresolved, now_ms, daily_realized):
        """Reconcile durable UTC equity baseline and every account bill since it."""
        if getattr(self.settings, "demo_account_exclusive", False) is not True:
            raise RiskAuthorizationError("Dedicated Demo account attestation is required")
        if not isinstance(account_config, list) or len(account_config) != 1:
            raise RiskAuthorizationError("Account identity snapshot is missing or ambiguous")
        uid = str(account_config[0].get("uid") or "")
        if not uid:
            raise RiskAuthorizationError("OKX account UID is missing; cannot bind the daily baseline")
        fingerprint = hashlib.sha256(uid.encode("utf-8")).hexdigest()
        ledger = AccountLedger(self.state)
        baseline = ledger.get_daily_baseline(now_ms)
        policy = PortfolioPolicy(
            max_snapshot_age_ms=self.settings.market_data_max_age_ms,
            fee_bps=Decimal(str(self.settings.fee_bps)),
            slippage_bps=Decimal(str(self.settings.slippage_bps)),
            max_daily_loss_quote=Decimal(str(self.settings.max_daily_loss_quote)),
            max_position_risk_quote=Decimal(str(self.settings.max_risk_quote)),
            max_position_notional_quote=Decimal(str(self.settings.max_position_quote)),
        )
        preliminary = PortfolioRiskEngine(policy).evaluate(
            account_config=account_config, balance=balance, tickers=tickers,
            position_lock=lock, local_unresolved_intents=unresolved,
            open_regular_orders=regular, open_algo_orders=[*oco, *conditional],
            now_ms=now_ms, daily_realized_pnl_quote="0",
        )
        if not preliminary.get("valuation_complete"):
            raise RiskAuthorizationError(
                "Cannot mark daily equity from incomplete valuation: "
                + ",".join(preliminary.get("blockers", []))
            )
        equity = preliminary.get("account_equity_usdt")
        if baseline is None:
            if lock or regular or oco or conditional or unresolved:
                raise RiskAuthorizationError(
                    "Daily baseline missing while account has existing exposure or unresolved state"
                )
            if Decimal(str(daily_realized)) != 0:
                raise RiskAuthorizationError(
                    "Daily baseline missing after a realized trade; daily loss history cannot be reconstructed"
                )
            day_start_ms = now_ms - (now_ms % 86_400_000)
            opening_bills = self.client.get_account_bills(
                begin=str(day_start_ms), end=str(now_ms)
            )
            ledger.save_bills(opening_bills)
            if opening_bills:
                raise RiskAuthorizationError(
                    "Daily baseline missing after account activity; wait for next clean UTC day"
                )
            ledger.establish_daily_baseline(
                equity_quote=equity, timestamp_ms=now_ms,
                account_fingerprint=fingerprint,
            )
            baseline = ledger.get_daily_baseline(now_ms)
            # Re-fetch against the exact immutable baseline interval. The earlier
            # day-start scan only proves the account was clean before baseline creation.
            bills = self.client.get_account_bills(
                begin=str(baseline["opening_timestamp_ms"]), end=str(now_ms)
            )
        else:
            bills = self.client.get_account_bills(
                begin=str(baseline["opening_timestamp_ms"]), end=str(now_ms)
            )
        if baseline["account_fingerprint"] != fingerprint:
            raise RiskAuthorizationError("Daily baseline belongs to a different OKX account")
        age_ms = now_ms - int(baseline["opening_timestamp_ms"])
        if age_ms < 0 or age_ms > 86_400_000:
            raise RiskAuthorizationError("Daily baseline is outside the current UTC day")
        # The OKX pager returns only after it has observed a terminal short page;
        # an exhausted page cap or malformed/repeated cursor raises before here.
        # Baselines are limited to the current UTC day, inside the 7-day endpoint.
        ledger.save_bills(bills)
        try:
            reconciliation = reconcile_spot_bills(
                bills, account_fingerprint=fingerprint,
                begin_ms=int(baseline["opening_timestamp_ms"]), end_ms=now_ms,
            )
        except BillReconciliationError as exc:
            raise RiskAuthorizationError("Account bills are not fully reconciled: " + str(exc)) from exc
        ledger.record_equity_mark(
            equity_quote=equity, timestamp_ms=now_ms,
            account_fingerprint=fingerprint,
        )
        result = ledger.daily_equity_delta(
            timestamp_ms=now_ms, reconciliation=reconciliation,
            account_fingerprint=fingerprint,
        )
        return result["daily_pnl_quote"]

    def _authorize(self, *, instrument: str, requested_size: str,
                   requested_price: str, candle_ts: int,
                   plan_override: dict | None, require_order_gate: bool) -> dict:
        try:
            if require_order_gate:
                self.settings.validate_order_permission()
            else:
                self.settings.validate_private_read_permission()
            if getattr(self.settings, "demo_account_exclusive", False) is not True:
                raise RiskAuthorizationError(
                    "Demo account exclusivity must be explicitly confirmed; "
                    "the local daily P&L ledger is not an account-wide loss record"
                )
            plan = plan_override if plan_override is not None else self.plan_provider(
                instrument=instrument, size=requested_size,
                entry_price=requested_price, candle_ts=candle_ts,
            )
            if not isinstance(plan, dict):
                raise RiskAuthorizationError("Entry plan provider returned no plan")
            plan_candle_ts = plan.get("candle_ts")
            if (plan.get("signal_action") != "buy"
                    or plan.get("candle_closed") is not True
                    or isinstance(plan_candle_ts, bool)
                    or not isinstance(plan_candle_ts, int)
                    or plan_candle_ts <= 0
                    or plan_candle_ts != int(candle_ts)):
                raise RiskAuthorizationError("Entry requires a matching confirmed closed-candle buy signal")

            # The plan provider must describe this exact normalized order. Do not
            # combine a stop/target from another symbol, size, or price with the
            # caller's requested order parameters.
            if str(plan.get("instrument") or "") != str(instrument):
                raise RiskAuthorizationError("Entry plan instrument does not match requested instrument")
            try:
                for field, requested in (("size", requested_size), ("entry_price", requested_price)):
                    planned_value = Decimal(str(plan[field]))
                    requested_value = Decimal(str(requested))
                    if (not planned_value.is_finite() or not requested_value.is_finite()
                            or planned_value <= 0 or planned_value != requested_value):
                        raise RiskAuthorizationError(
                            f"Entry plan {field} does not match requested order"
                        )
                stop_value = Decimal(str(plan["stop_loss"]))
                target_value = Decimal(str(plan["take_profit"]))
                entry_value = Decimal(str(requested_price))
                if (not stop_value.is_finite() or not target_value.is_finite()
                        or not (Decimal("0") < stop_value < entry_value < target_value)):
                    raise RiskAuthorizationError("Entry plan stop/target levels are invalid")
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                if isinstance(exc, RiskAuthorizationError):
                    raise
                raise RiskAuthorizationError("Entry plan price/size fields are invalid") from exc

            # Re-read current Spot rules at preflight time. A previously valid
            # candidate must not pass if tickSz/lotSz/minSz or listing state changed.
            from .instruments import SpotInstrument
            spec = SpotInstrument.from_okx(self.client.get_spot_instrument(instrument))
            if spec.inst_id != instrument or spec.quote_ccy != "USDT":
                raise RiskAuthorizationError("Current instrument metadata is not the requested USDT Spot market")
            normalized_size, normalized_price = spec.normalize_order(
                "buy", requested_size, requested_price
            )
            if (Decimal(normalized_size) != Decimal(str(requested_size))
                    or Decimal(normalized_price) != Decimal(str(requested_price))):
                raise RiskAuthorizationError("Candidate size or price no longer matches current Spot precision rules")

            now_ms = int(self.clock_ms())
            candle_close_ts = plan_candle_ts + self.bar_ms
            if (candle_close_ts > now_ms
                    or now_ms - candle_close_ts > self.bar_ms * self.max_candle_age_intervals):
                raise RiskAuthorizationError("Closed-candle signal is stale or its candle is not complete")
            planned = {
                "instrument": instrument,
                "size": str(requested_size),
                "entry_price": str(requested_price),
                "stop_loss": str(plan["stop_loss"]),
                "take_profit": str(plan["take_profit"]),
            }
            lock = self.state.get_position_lock()
            unresolved = self.state.list_unresolved_intents()
            account_config = self.client.get_account_config()
            balance = self.client.get_balance()
            tickers = self.client.get_tickers()
            regular = self.client.get_pending_orders("SPOT")
            oco = self.client.get_pending_algo_orders("oco")
            conditional = self.client.get_pending_algo_orders("conditional")
            breaker = CircuitBreaker(self.state, self.settings.max_daily_loss_quote)
            daily_realized = breaker.status()["daily_realized_pnl_quote"]
            daily_account_pnl = self._daily_account_pnl(
                account_config=account_config, balance=balance, tickers=tickers,
                regular=regular, oco=oco, conditional=conditional,
                lock=lock, unresolved=unresolved, now_ms=now_ms,
                daily_realized=daily_realized,
            )
            policy = PortfolioPolicy(
                max_snapshot_age_ms=self.settings.market_data_max_age_ms,
                fee_bps=Decimal(str(self.settings.fee_bps)),
                slippage_bps=Decimal(str(self.settings.slippage_bps)),
                max_daily_loss_quote=Decimal(str(self.settings.max_daily_loss_quote)),
                max_position_risk_quote=Decimal(str(self.settings.max_risk_quote)),
                max_position_notional_quote=Decimal(str(self.settings.max_position_quote)),
            )
            report = PortfolioRiskEngine(policy).evaluate(
                account_config=account_config, balance=balance, tickers=tickers,
                position_lock=lock, local_unresolved_intents=unresolved,
                open_regular_orders=regular, open_algo_orders=[*oco, *conditional],
                planned_entry=planned, now_ms=now_ms,
                daily_realized_pnl_quote=daily_realized,
                daily_account_pnl_quote=daily_account_pnl,
                require_daily_account_pnl=True,
            )
            if report.get("valuation_complete") is not True or report.get("entry_allowed") is not True:
                raise RiskAuthorizationError(
                    "Portfolio snapshot denied entry: " + ",".join(report.get("blockers", []))
                )
            approved = report.get("planned_entry")
            if not isinstance(approved, dict):
                raise RiskAuthorizationError("Portfolio engine did not produce a priced risk plan")
            return {
                "entry_allowed": True,
                "planned_entry": {
                    "instrument": approved["instrument"],
                    "size": approved["size"],
                    "entry_price": approved["entry_price"],
                    "stop_loss": approved["stop_loss"],
                    "take_profit": approved["take_profit"],
                    "risk_to_stop_quote": approved["risk_to_stop_quote"],
                },
                "portfolio_snapshot": {
                    "account_equity_usdt": report["account_equity_usdt"],
                    "available_usdt": report["available_usdt"],
                    "projected_daily_pnl_after_planned_stop_quote":
                        report["projected_daily_pnl_after_planned_stop_quote"],
                },
            }
        except RiskAuthorizationError:
            raise
        except Exception as exc:
            raise RiskAuthorizationError("Portfolio risk authorization failed closed") from exc

class CandidateRiskPreflight:
    """Evaluate a read-only runtime candidate against live portfolio risk controls.

    This boundary can only call PortfolioRiskAuthorizer (private GET snapshots).
    It has no order manager and never submits, cancels, or modifies exchange orders.
    """

    def __init__(self, authorizer: PortfolioRiskAuthorizer, state_store):
        if not isinstance(authorizer, PortfolioRiskAuthorizer):
            raise TypeError("authorizer must be a PortfolioRiskAuthorizer")
        self.authorizer = authorizer
        self.state = state_store

    @staticmethod
    def _validate_candidate(candidate: dict) -> dict:
        if not isinstance(candidate, dict):
            raise RiskAuthorizationError("Candidate entry plan is missing")
        if candidate.get("signal_action") != "buy" or candidate.get("candle_closed") is not True:
            raise RiskAuthorizationError("Candidate must be a confirmed closed-candle buy plan")
        candle_ts = candidate.get("candle_ts")
        if isinstance(candle_ts, bool) or not isinstance(candle_ts, int) or candle_ts <= 0:
            raise RiskAuthorizationError("Candidate candle timestamp is invalid")
        submitted = candidate.get("orders_submitted")
        if isinstance(submitted, bool) or not isinstance(submitted, int) or submitted != 0:
            raise RiskAuthorizationError("Candidate plan must be read-only and have zero submitted orders")
        instrument = str(candidate.get("instrument") or "")
        if not instrument or not instrument.endswith("-USDT"):
            raise RiskAuthorizationError("Candidate instrument must be a USDT Spot market")
        try:
            values = {}
            for field in ("size", "entry_price", "stop_loss", "take_profit"):
                value = Decimal(str(candidate[field]))
                if not value.is_finite() or value <= 0:
                    raise ValueError(field)
                values[field] = value
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise RiskAuthorizationError("Candidate plan contains invalid price or size values") from exc
        if not (values["stop_loss"] < values["entry_price"] < values["take_profit"]):
            raise RiskAuthorizationError("Candidate stop/entry/target ordering is invalid")
        return {
            "instrument": instrument,
            "signal_action": "buy",
            "candle_closed": True,
            "size": str(candidate["size"]),
            "entry_price": str(candidate["entry_price"]),
            "stop_loss": str(candidate["stop_loss"]),
            "take_profit": str(candidate["take_profit"]),
            "candle_ts": candle_ts,
            "orders_submitted": 0,
        }

    def evaluate(self, candidate_plan: dict) -> dict:
        """Return an auditable approve/reject preflight result; never place an order."""
        instrument = None
        candle_ts = None
        try:
            candidate = self._validate_candidate(candidate_plan)
            instrument, candle_ts = candidate["instrument"], candidate["candle_ts"]
            authorization = self.authorizer.preflight_candidate(candidate_plan)
            if not isinstance(authorization, dict) or authorization.get("entry_allowed") is not True:
                raise RiskAuthorizationError("Portfolio authorizer did not approve the candidate")
            approved = authorization.get("planned_entry")
            if not isinstance(approved, dict):
                raise RiskAuthorizationError("Portfolio authorizer returned no approved plan")
            for field in ("instrument", "size", "entry_price", "stop_loss", "take_profit"):
                if field == "instrument":
                    matches = str(approved.get(field)) == candidate[field]
                else:
                    try:
                        matches = Decimal(str(approved.get(field))) == Decimal(candidate[field])
                    except (TypeError, ValueError, ArithmeticError):
                        matches = False
                if not matches:
                    raise RiskAuthorizationError(
                        f"Approved {field} does not match the candidate plan"
                    )
            status = "approved"
            reason = None
            result = {
                "status": status,
                "entry_allowed": True,
                "candidate": candidate,
                "authorization": authorization,
                "execution": "not_submitted_preflight_only",
                "order_submission_attempts": 0,
            }
        except Exception as exc:
            status = "rejected"
            reason = str(exc)[:240] or type(exc).__name__
            result = {
                "status": status,
                "entry_allowed": False,
                "reason": reason,
                "authorization": "not_approved",
                "execution": "not_submitted_preflight_only",
                "order_submission_attempts": 0,
            }
        self.state.record_event(
            "entry_risk_preflight_" + status,
            None,
            {"instrument": instrument, "candle_ts": candle_ts,
             "reason": reason, "order_submission_attempts": 0},
        )
        return result
