"""Startup-entry gate regression tests; all exchange access is locally faked."""
import pytest

from nebula.config import SafetyError, Settings
from nebula.entry_gate import StartupPermitError
from nebula.exchange import OkxDemoClient
from nebula.startup import StartupRecovery
from nebula.state import StateStore


class CleanAccount:
    settings = Settings(api_key="demo-key", secret_key="demo-secret", passphrase="demo-pass",
                       demo_account_exclusive=True, max_daily_loss_quote=20)

    def get_account_config(self):
        return [{"uid": "demo-uid", "acctLv": "1", "autoLoan": False,
                 "enableSpotBorrow": False, "perm": "read_only,trade"}]

    def get_balance(self):
        return [{"details": [{"ccy": "USDT", "availBal": "1000", "frozenBal": "0"}]}]

    def get_pending_orders(self, inst_type="SPOT"):
        return []

    def get_pending_algo_orders(self, ord_type="oco"):
        return []


def test_permit_is_one_shot_and_bound_to_exact_state_and_client(tmp_path):
    state = StateStore(str(tmp_path / "permit.db"))
    client = CleanAccount()
    recovery = StartupRecovery(client, state)
    result = recovery.run()

    assert result["entry_permit_issued"] is True
    permit = recovery.entry_permit
    bound = {"instrument": "BTC-USDT", "size": "0.01", "price": "50000",
             "client_order_id": "NBONESHOT"}
    permit.consume(client, state, **bound)
    with pytest.raises(StartupPermitError, match="already been consumed"):
        permit.consume(client, state, **bound)


def test_permit_is_invalidated_when_local_state_changes_after_recovery(tmp_path):
    state = StateStore(str(tmp_path / "stale.db"))
    client = CleanAccount()
    recovery = StartupRecovery(client, state)
    assert recovery.run()["entry_permit_issued"] is True

    state.record_event("state_changed_after_recovery", None, {"test": True})
    with pytest.raises(StartupPermitError, match="state changed"):
        recovery.entry_permit.consume(client, state, instrument="BTC-USDT",
                                       size="0.01", price="50000",
                                       client_order_id="NBSTALE")


def test_permit_cannot_be_used_with_another_state_store(tmp_path):
    state = StateStore(str(tmp_path / "source.db"))
    other = StateStore(str(tmp_path / "other.db"))
    client = CleanAccount()
    recovery = StartupRecovery(client, state)
    assert recovery.run()["entry_permit_issued"] is True

    with pytest.raises(StartupPermitError, match="binding mismatch"):
        recovery.entry_permit.consume(client, other, instrument="BTC-USDT",
                                       size="0.01", price="50000",
                                       client_order_id="NBOTHER")


def test_non_spot_or_withdraw_enabled_account_never_gets_permit(tmp_path):
    class UnsafeAccount(CleanAccount):
        def get_account_config(self):
            return [{"uid": "demo-uid", "acctLv": "2", "autoLoan": False,
                     "enableSpotBorrow": False, "perm": "read_only,trade,withdraw"}]

    state = StateStore(str(tmp_path / "unsafe-account.db"))
    recovery = StartupRecovery(UnsafeAccount(), state)
    result = recovery.run()

    assert result["status"] == "blocked_reconciliation_failure"
    assert recovery.entry_permit is None


def test_direct_authenticated_buy_request_cannot_bypass_startup_gate():
    class NeverCallSession:
        def request(self, *args, **kwargs):
            raise AssertionError("network must not be called")

    settings = Settings(
        api_key="demo-key", secret_key="demo-secret", passphrase="demo-pass",
        enable_demo_orders=True, kill_switch=False,
        max_daily_loss_quote=20, max_risk_quote=2, max_position_quote=100,
    )
    client = OkxDemoClient(settings, session=NeverCallSession())

    with pytest.raises(SafetyError, match="startup recovery permit"):
        client.request("POST", "/api/v5/trade/order",
                       payload={"instId": "BTC-USDT", "tdMode": "cash", "side": "buy"},
                       private=True)
