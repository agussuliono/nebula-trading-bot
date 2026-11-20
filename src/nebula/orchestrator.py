"""Fail-closed single-cycle orchestration for NEBULA Spot Demo.

Default operation is shadow-only. Demo order execution requires an explicit
caller opt-in plus every existing Settings, account, startup, portfolio-risk,
position-lock, and OCO protection gate. This module never supports live trading.
"""
from decimal import Decimal
import hashlib

from .config import SafetyError
from .entry_plan import EntryPlanPolicy
from .runtime import ReadOnlyRuntime, RuntimePolicy


class ExecutionOrchestrationError(RuntimeError):
    pass


class ExecutionOrchestrator:
    """Coordinate one scan → recovery → risk → entry → OCO cycle.

    execute=False is strictly read-only and does not perform private account reads.
    execute=True is an explicit Demo-only opt-in; it still cannot override the
    immutable Demo adapter, Settings write gate, kill switch, account attestation,
    StartupRecovery permit, or portfolio authorization.
    """

    BAR_MS = {"1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}

    def __init__(self, client, state_store, settings, *, bar="1H", runtime=None,
                 startup_recovery_cls=None, order_manager_cls=None,
                 protection_manager_cls=None, risk_authorizer_cls=None):
        if bar not in self.BAR_MS:
            raise ValueError("V1 orchestrator supports only 1H, 4H, or 1D")
        self.client = client
        self.state = state_store
        self.settings = settings
        self.bar = bar
        self.startup_recovery_cls = startup_recovery_cls
        self.order_manager_cls = order_manager_cls
        self.protection_manager_cls = protection_manager_cls
        self.risk_authorizer_cls = risk_authorizer_cls

        self.breaker = None
        if getattr(settings, "max_daily_loss_quote", 0) > 0:
            from .risk_controls import CircuitBreaker
            self.breaker = CircuitBreaker(state_store, settings.max_daily_loss_quote)

        plan_policy = None
        if (getattr(settings, "max_risk_quote", 0) > 0
                and getattr(settings, "max_position_quote", 0) > 0):
            plan_policy = EntryPlanPolicy(
                risk_budget_quote=Decimal(str(settings.max_risk_quote)),
                max_notional_quote=Decimal(str(settings.max_position_quote)),
                fee_bps=Decimal(str(settings.fee_bps)),
                slippage_bps=Decimal(str(settings.slippage_bps)),
            )
        policy = RuntimePolicy(bar=bar, entry_plan_policy=plan_policy)
        self.runtime = runtime or ReadOnlyRuntime(
            client, state_store, policy=policy, circuit_breaker=self.breaker
        )

    @staticmethod
    def _client_order_id(instrument: str, candle_ts: int) -> str:
        digest = hashlib.sha256(f"{instrument}|{int(candle_ts)}".encode()).hexdigest()[:24].upper()
        return "NBE" + digest

    def _record(self, event_type, details):
        try:
            self.state.record_event(event_type, None, details)
        except Exception:
            # Audit-write failure must never turn a denied execution into an allowed one.
            pass

    def _candidate(self, scan: dict):
        for item in scan.get("signals", []):
            if (item.get("signal") == "buy" and item.get("blocked_reason") is None
                    and isinstance(item.get("candidate_entry_plan"), dict)):
                plan = item["candidate_entry_plan"]
                if (plan.get("signal_action") == "buy"
                        and plan.get("candle_closed") is True
                        and plan.get("orders_submitted") == 0
                        and plan.get("instrument") == item.get("instrument")):
                    return item
        return None

    def run_once(self, *, execute: bool = False) -> dict:
        if type(execute) is not bool:
            raise ValueError("execute must be a boolean")

        if execute:
            try:
                self.settings.validate_order_permission()
            except SafetyError:
                return {
                    "mode": "demo_execution_blocked",
                    "status": "settings_safety_gate_closed",
                    "execution": "not_started",
                    "order_submission_attempts": 0,
                }
            if getattr(self.settings, "demo_account_exclusive", False) is not True:
                return {
                    "mode": "demo_execution_blocked",
                    "status": "dedicated_demo_account_not_attested",
                    "execution": "not_started",
                    "order_submission_attempts": 0,
                }
            if self.breaker is None:
                return {
                    "mode": "demo_execution_blocked",
                    "status": "daily_loss_limit_not_configured",
                    "execution": "not_started",
                    "order_submission_attempts": 0,
                }
            try:
                self.breaker.check_entry_allowed()
            except SafetyError:
                return {
                    "mode": "demo_execution_blocked",
                    "status": "circuit_breaker_or_emergency_stop",
                    "execution": "not_started",
                    "order_submission_attempts": 0,
                }

        # Public market scan only. It never calls an order-writing endpoint.
        scan = self.runtime.run_once()
        candidate = self._candidate(scan)
        if not execute:
            if candidate is None:
                return {
                    "mode": "shadow_only", "status": "no_eligible_buy_candidate",
                    "scan": scan, "execution": "not_submitted",
                    "order_submission_attempts": 0,
                }
            return {
                "mode": "shadow_only",
                "status": "candidate_ready_but_execution_disabled",
                "candidate": candidate,
                "execution": "not_submitted",
                "order_submission_attempts": 0,
            }

        stage = "startup_recovery"
        entry_attempts = 0
        oco_attempts = 0
        plan = candidate["candidate_entry_plan"] if candidate else None
        client_order_id = (
            self._client_order_id(plan["instrument"], plan["candle_ts"])
            if plan else None
        )
        if candidate:
            self._record("orchestrator_execution_requested", {
                "instrument": plan["instrument"], "candle_ts": plan["candle_ts"],
                "mode": "okx_spot_demo",
            })

        try:
            from .startup import StartupRecovery
            recovery_cls = self.startup_recovery_cls or StartupRecovery
            recovery = recovery_cls(self.client, self.state)
            recovery_result = recovery.run()

            # Existing positions must be reconciled even when the market scan
            # has no new buy candidate. An active OCO remains exchange-protected;
            # a triggered OCO is reconciled and may release the lock only after
            # the fill ledger and a fresh account snapshot prove the account flat.
            recovery_status = recovery_result.get("status")
            if recovery_status == "position_reconciled_protection_active":
                return {
                    "mode": "demo_execution",
                    "status": "existing_position_protected",
                    "recovery": recovery_result,
                    "execution": "no_new_entry",
                    "order_submission_attempts": 0,
                }
            if recovery_status == "position_reconciled_exit_in_progress":
                from .protection_manager import DemoProtectionManager
                protection_cls = self.protection_manager_cls or DemoProtectionManager
                protection = protection_cls(self.client, self.state, circuit_breaker=self.breaker)
                owner = recovery_result.get("position_client_order_id")
                exit_reconciliation = protection.reconcile(owner)
                accounting = exit_reconciliation.get("accounting")
                if not isinstance(accounting, dict) or accounting.get("accounting_complete") is not True:
                    return {
                        "mode": "demo_execution",
                        "status": "existing_position_exit_reconciliation_incomplete",
                        "recovery": recovery_result,
                        "exit_reconciliation": exit_reconciliation,
                        "position_lock_retained": True,
                        "do_not_retry": True,
                        "order_submission_attempts": 0,
                    }
                finalized = protection.finalize_position_if_flat(
                    owner, accounting=accounting
                )
                if finalized.get("released") is not True:
                    return {
                        "mode": "demo_execution",
                        "status": "existing_position_not_yet_flat",
                        "recovery": recovery_result,
                        "exit_reconciliation": exit_reconciliation,
                        "finalization": finalized,
                        "position_lock_retained": True,
                        "order_submission_attempts": 0,
                    }
                # Do not open another position in the same cycle that closes one.
                return {
                    "mode": "demo_execution",
                    "status": "existing_position_closed_next_cycle",
                    "finalization": finalized,
                    "execution": "no_new_entry_this_cycle",
                    "order_submission_attempts": 0,
                }

            if (recovery_status != "clean_no_local_position"
                    or recovery_result.get("entry_permit_issued") is not True
                    or recovery.entry_permit is None):
                return {
                    "mode": "demo_execution_blocked",
                    "status": "startup_recovery_denied",
                    "recovery": recovery_result,
                    "execution": "not_submitted",
                    "order_submission_attempts": 0,
                }
            if candidate is None:
                return {
                    "mode": "demo_execution",
                    "status": "no_eligible_buy_candidate",
                    "scan": scan,
                    "recovery": recovery_result,
                    "execution": "not_submitted",
                    "order_submission_attempts": 0,
                }

            # Do not write to the state ledger between permit issuance and its
            # consumption: the permit is fingerprint-bound to the clean snapshot.
            stage = "portfolio_risk_authorization_and_entry"
            from .risk_authorizer import PortfolioRiskAuthorizer
            authorizer_cls = self.risk_authorizer_cls or PortfolioRiskAuthorizer
            authorizer = authorizer_cls(
                self.client, self.state, self.settings,
                plan_provider=lambda **_: plan,
                bar_ms=self.BAR_MS[self.bar],
                max_candle_age_intervals=getattr(
                    getattr(self.runtime, "policy", None),
                    "max_candle_age_intervals", 2,
                ),
            )
            from .orders import DemoOrderManager
            manager_cls = self.order_manager_cls or DemoOrderManager
            manager = manager_cls(
                self.client, self.state, circuit_breaker=self.breaker,
                risk_authorizer=authorizer, startup_permit=recovery.entry_permit,
            )
            entry_attempts = 1
            submitted = manager.submit_limit(
                inst_id=plan["instrument"], side="buy", size=plan["size"],
                price=plan["entry_price"], client_order_id=client_order_id,
                candle_ts=plan["candle_ts"],
            )

            stage = "entry_reconciliation"
            reconciled = manager.reconcile(client_order_id)
            entry_status = reconciled.get("status")
            if entry_status in {"live", "partially_filled"}:
                stage = "entry_remainder_cancel_and_reconciliation"
                from .protection_manager import DemoProtectionManager
                protection_cls = self.protection_manager_cls or DemoProtectionManager
                protection = protection_cls(self.client, self.state, circuit_breaker=self.breaker)
                final_entry = protection.cancel_entry_remainder_and_reconcile(client_order_id)
                entry_status = final_entry.get("status")
                if entry_status == "cancel_pending" or entry_status not in {
                    "filled", "canceled", "mmp_canceled"
                }:
                    return {
                        "mode": "demo_execution",
                        "status": "entry_cancel_or_fill_reconciliation_pending",
                        "entry": submitted,
                        "reconciliation": final_entry,
                        "position_lock_retained": bool(self.state.get_position_lock()),
                        "do_not_retry": True,
                        "order_submission_attempts": entry_attempts,
                    }
            elif entry_status not in {"filled", "canceled", "mmp_canceled"}:
                return {
                    "mode": "demo_execution",
                    "status": "entry_reconciliation_pending",
                    "entry": submitted,
                    "reconciliation": reconciled,
                    "position_lock_retained": bool(self.state.get_position_lock()),
                    "do_not_retry": True,
                    "order_submission_attempts": entry_attempts,
                }

            stage = "protective_oco_prepare"
            from .protection_manager import DemoProtectionManager
            protection_cls = self.protection_manager_cls or DemoProtectionManager
            protection = protection_cls(self.client, self.state, circuit_breaker=self.breaker)
            prepared = protection.prepare(entry_client_order_id=client_order_id)
            if prepared.get("status") == "no_position_to_protect":
                return {
                    "mode": "demo_execution",
                    "status": "entry_not_filled",
                    "entry": submitted,
                    "protection": prepared,
                    "position_lock_retained": bool(self.state.get_position_lock()),
                    "order_submission_attempts": entry_attempts,
                }
            if prepared.get("status") != "reserved":
                return {
                    "mode": "demo_execution",
                    "status": "protective_oco_not_ready",
                    "entry": submitted,
                    "protection": prepared,
                    "position_lock_retained": bool(self.state.get_position_lock()),
                    "do_not_retry": True,
                    "order_submission_attempts": entry_attempts,
                }

            stage = "protective_oco_submission"
            oco_attempts = 1
            oco = protection.submit_reserved(client_order_id)
            return {
                "mode": "demo_execution",
                "status": "entry_and_protection_active",
                "candidate": candidate,
                "entry": submitted,
                "entry_reconciliation": reconciled,
                "protection": oco,
                "position_lock_retained": bool(self.state.get_position_lock()),
                "order_submission_attempts": entry_attempts + oco_attempts,
            }
        except Exception as exc:
            self._record("orchestrator_execution_interrupted", {
                "stage": stage, "error_type": type(exc).__name__,
                "entry_submission_attempted": entry_attempts > 0,
                "oco_submission_attempted": oco_attempts > 0,
            })
            try:
                lock_active = bool(self.state.get_position_lock())
            except Exception:
                lock_active = True
            return {
                "mode": "demo_execution",
                "status": "interrupted_fail_closed",
                "stage": stage,
                "error_type": type(exc).__name__,
                "entry_submission_may_have_occurred": entry_attempts > 0,
                "oco_submission_may_have_occurred": oco_attempts > 0,
                "position_lock_retained": lock_active,
                "do_not_retry": True,
                "order_submission_attempts": entry_attempts + oco_attempts,
            }
