from types import SimpleNamespace

import pytest

from nebula.config import Settings
from nebula.orchestrator import ExecutionOrchestrator
from nebula.state import StateStore


def candidate_report():
    plan = {
        "instrument": "AAA-USDT", "signal_action": "buy",
        "candle_closed": True, "candle_ts": 1_799_996_400_000,
        "size": "1.0", "entry_price": "99.1",
        "stop_loss": "97.0", "take_profit": "103.3",
        "risk_to_stop_quote": "2.5", "orders_submitted": 0,
    }
    return {
        "mode": "read_only_analysis", "orders_enabled": False,
        "order_submission_attempts": 0,
        "signals": [{
            "instrument": "AAA-USDT", "signal": "buy", "blocked_reason": None,
            "candidate_entry_plan": plan,
        }],
    }


class FakeRuntime:
    def __init__(self, report=None):
        self.report = report or candidate_report()
        self.calls = 0
        self.policy = SimpleNamespace(max_candle_age_intervals=2)

    def run_once(self):
        self.calls += 1
        return self.report


class FakePermit:
    pass


class FakeRecovery:
    def __init__(self, client, state):
        self.entry_permit = FakePermit()

    def run(self):
        return {
            "status": "clean_no_local_position",
            "entry_permit_issued": True,
            "exchange_writes": 0,
        }


class BlockedRecovery(FakeRecovery):
    def run(self):
        return {
            "status": "blocked_reconciliation_failure",
            "entry_permit_issued": False,
            "blockers": ["test_blocker"],
            "exchange_writes": 0,
        }


class FakeAuthorizer:
    def __init__(self, *args, **kwargs):
        self.plan_provider = kwargs["plan_provider"]


class FakeOrders:
    instances = []

    def __init__(self, client, state, *, circuit_breaker, risk_authorizer, startup_permit):
        self.state = state
        self.risk_authorizer = risk_authorizer
        self.startup_permit = startup_permit
        self.calls = []
        self.reconcile_state = "filled"
        self.instances.append(self)

    def submit_limit(self, **kwargs):
        self.calls.append(("submit", kwargs))
        return {"client_order_id": kwargs["client_order_id"], "status": "acknowledged"}

    def reconcile(self, client_order_id):
        self.calls.append(("reconcile", client_order_id))
        return {"client_order_id": client_order_id, "status": self.reconcile_state}


class FakeProtection:
    instances = []

    def __init__(self, client, state, *, circuit_breaker):
        self.calls = []
        self.cancel_result = {"status": "canceled", "filled_size": "1.0"}
        self.prepare_result = {"status": "reserved"}
        self.instances.append(self)

    def cancel_entry_remainder_and_reconcile(self, client_order_id):
        self.calls.append(("cancel", client_order_id))
        return self.cancel_result

    def prepare(self, *, entry_client_order_id):
        self.calls.append(("prepare", entry_client_order_id))
        return self.prepare_result

    def submit_reserved(self, entry_client_order_id):
        self.calls.append(("submit_oco", entry_client_order_id))
        return {"status": "active", "exchange_algo_id": "12345"}


def enabled_settings(**overrides):
    values = dict(
        api_key="demo-key", secret_key="demo-secret", passphrase="demo-pass",
        demo_account_exclusive=True, enable_demo_orders=True, kill_switch=False,
        max_daily_loss_quote=100, max_risk_quote=5, max_position_quote=100,
    )
    values.update(overrides)
    return Settings(**values)


def make_orchestrator(tmp_path, *, settings=None, runtime=None, recovery=FakeRecovery):
    state = StateStore(str(tmp_path / "orchestrator.db"))
    runtime = runtime or FakeRuntime()
    orchestrator = ExecutionOrchestrator(
        object(), state, settings or Settings(), runtime=runtime,
        startup_recovery_cls=recovery, order_manager_cls=FakeOrders,
        protection_manager_cls=FakeProtection, risk_authorizer_cls=FakeAuthorizer,
    )
    return orchestrator, state, runtime


def test_shadow_mode_scans_candidate_but_never_reads_private_or_submits(tmp_path):
    orchestrator, state, runtime = make_orchestrator(tmp_path)
    result = orchestrator.run_once()

    assert runtime.calls == 1
    assert result["status"] == "candidate_ready_but_execution_disabled"
    assert result["execution"] == "not_submitted"
    assert result["order_submission_attempts"] == 0
    assert state.get_position_lock() is None


def test_execution_flag_cannot_override_default_settings_gate(tmp_path):
    orchestrator, _, runtime = make_orchestrator(tmp_path)
    result = orchestrator.run_once(execute=True)

    assert result["status"] == "settings_safety_gate_closed"
    assert runtime.calls == 0
    assert result["order_submission_attempts"] == 0


def test_execution_requires_explicit_demo_account_attestation(tmp_path):
    orchestrator, _, runtime = make_orchestrator(
        tmp_path, settings=enabled_settings(demo_account_exclusive=False)
    )
    result = orchestrator.run_once(execute=True)

    assert result["status"] == "dedicated_demo_account_not_attested"
    assert runtime.calls == 0


def test_no_candidate_does_not_run_private_recovery_or_create_order_manager(tmp_path):
    FakeOrders.instances.clear()
    report = candidate_report()
    report["signals"][0]["signal"] = "hold"
    orchestrator, _, runtime = make_orchestrator(
        tmp_path, settings=enabled_settings(), runtime=FakeRuntime(report)
    )
    result = orchestrator.run_once(execute=True)

    assert result["status"] == "no_eligible_buy_candidate"
    assert runtime.calls == 1
    assert FakeOrders.instances == []


def test_blocked_startup_recovery_prevents_entry_submission(tmp_path):
    FakeOrders.instances.clear()
    orchestrator, _, _ = make_orchestrator(
        tmp_path, settings=enabled_settings(), recovery=BlockedRecovery
    )
    result = orchestrator.run_once(execute=True)

    assert result["status"] == "startup_recovery_denied"
    assert result["order_submission_attempts"] == 0
    assert FakeOrders.instances == []


def test_complete_demo_lifecycle_orders_entry_then_reconciles_and_protects(tmp_path):
    FakeOrders.instances.clear()
    FakeProtection.instances.clear()
    orchestrator, _, _ = make_orchestrator(tmp_path, settings=enabled_settings())
    result = orchestrator.run_once(execute=True)

    assert result["status"] == "entry_and_protection_active"
    assert result["entry"]["status"] == "acknowledged"
    assert result["protection"]["status"] == "active"
    assert result["order_submission_attempts"] == 2
    manager = FakeOrders.instances[-1]
    assert manager.startup_permit is not None
    assert [call[0] for call in manager.calls] == ["submit", "reconcile"]
    assert FakeProtection.instances[-1].calls == [
        ("prepare", manager.calls[0][1]["client_order_id"]),
        ("submit_oco", manager.calls[0][1]["client_order_id"]),
    ]


def test_live_entry_must_be_canceled_and_finalized_before_oco(tmp_path):
    FakeOrders.instances.clear()
    FakeProtection.instances.clear()
    orchestrator, _, _ = make_orchestrator(tmp_path, settings=enabled_settings())
    # Configure a live entry state through a deterministic fake manager.
    class LiveOrders(FakeOrders):
        def reconcile(self, client_order_id):
            self.calls.append(("reconcile", client_order_id))
            return {"client_order_id": client_order_id, "status": "live"}

    class PendingProtection(FakeProtection):
        def cancel_entry_remainder_and_reconcile(self, client_order_id):
            self.calls.append(("cancel", client_order_id))
            return {"status": "cancel_pending", "filled_size": "0.5"}

    orchestrator.order_manager_cls = LiveOrders
    orchestrator.protection_manager_cls = PendingProtection
    pending = orchestrator.run_once(execute=True)

    assert pending["status"] == "entry_cancel_or_fill_reconciliation_pending"
    assert pending["do_not_retry"] is True
    assert PendingProtection.instances[-1].calls[0][0] == "cancel"
    assert "prepare" not in [call[0] for call in PendingProtection.instances[-1].calls]


def test_entry_exception_is_fail_closed_and_never_blindly_retried(tmp_path):
    class FailingOrders(FakeOrders):
        def submit_limit(self, **kwargs):
            self.calls.append(("submit", kwargs))
            raise TimeoutError("simulated ambiguous timeout")

    orchestrator, _, _ = make_orchestrator(tmp_path, settings=enabled_settings())
    orchestrator.order_manager_cls = FailingOrders
    result = orchestrator.run_once(execute=True)

    assert result["status"] == "interrupted_fail_closed"
    assert result["stage"] == "portfolio_risk_authorization_and_entry"
    assert result["entry_submission_may_have_occurred"] is True
    assert result["do_not_retry"] is True
    assert result["oco_submission_may_have_occurred"] is False
