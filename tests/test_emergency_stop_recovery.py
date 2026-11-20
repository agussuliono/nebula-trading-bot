"""Emergency-stop and restart-recovery tests using offline fake clients only.

These tests prove that the kill switch blocks new exposure while allowing
read-only recovery of already-persisted orders and protective OCO state.
No OKX credentials, network calls, or exchange writes are used.
"""
import pytest

from nebula.config import SafetyError, Settings
from nebula.exchange import OkxDemoClient
from nebula.orders import DemoOrderManager, OrderManagerError
from nebula.protection_manager import DemoProtectionManager, ProtectionManagerError
from nebula.state import StateStore


def stopped_settings():
    return Settings(
        api_key="demo-key",
        secret_key="demo-secret",
        passphrase="demo-pass",
        enable_demo_orders=True,
        kill_switch=True,
        max_daily_loss_quote=20,
        max_risk_quote=2,
        max_position_quote=100,
    )


class NeverWriteSession:
    def __init__(self):
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("No HTTP request is permitted in this test")


def seed_position(store, entry_id="NBSTOPRECOVERY"):
    store.reserve_entry_intent(entry_id, "BTC-USDT", 123, "buy")
    store.update_intent(entry_id, "filled", "10001")
    store.save_order_snapshot(entry_id, {
        "ordId": "10001",
        "state": "filled",
        "sz": "0.01",
        "accFillSz": "0.004",
        "avgPx": "50000",
        "fee": "-0.01",
        "feeCcy": "USDT",
    })
    store.reserve_protection_intent(
        entry_client_order_id=entry_id,
        instrument="BTC-USDT",
        algo_client_order_id="NBPSTOPRECOVERY",
        filled_size="0.004",
        take_profit="52000",
        stop_loss="48000",
    )
    return entry_id


def test_emergency_stop_blocks_new_entry_before_http_write():
    session = NeverWriteSession()
    client = OkxDemoClient(stopped_settings(), session=session)

    with pytest.raises(SafetyError, match="Emergency stop"):
        client.place_spot_limit(
            inst_id="BTC-USDT",
            side="buy",
            size="0.001",
            price="50000",
            client_order_id="NBSTOPENTRY",
        )

    assert session.calls == []


def test_emergency_stop_keeps_protective_oco_cancel_blocked():
    session = NeverWriteSession()
    client = OkxDemoClient(stopped_settings(), session=session)

    with pytest.raises(SafetyError, match="Emergency stop"):
        client.cancel_algo_order(inst_id="BTC-USDT", algo_id="998877")

    assert session.calls == []


def test_restart_recovers_ambiguous_entry_with_kill_switch_still_active(tmp_path):
    db_path = str(tmp_path / "restart-entry.sqlite3")
    first = StateStore(db_path)
    first.reserve_entry_intent("NBENTRYRESTART", "BTC-USDT", 123, "buy")
    first.update_intent("NBENTRYRESTART", "submission_unknown")

    class ReadOnlyRecoveryClient:
        settings = stopped_settings()

        def __init__(self):
            self.reads = 0
            self.writes = 0

        def get_order(self, instrument, client_order_id):
            self.reads += 1
            return [{
                "ordId": "10002",
                "clOrdId": client_order_id,
                "state": "live",
                "sz": "0.01",
                "accFillSz": "0",
                "avgPx": "0",
                "fee": "0",
                "feeCcy": "USDT",
            }]

        def place_spot_limit(self, **kwargs):
            self.writes += 1
            raise AssertionError("Recovery must never submit an entry")

    client = ReadOnlyRecoveryClient()
    restarted = StateStore(db_path)
    manager = DemoOrderManager(client, restarted)

    result = manager.reconcile("NBENTRYRESTART")

    assert result["status"] == "live"
    assert client.reads == 1
    assert client.writes == 0
    assert restarted.get_position_lock()["owner_client_order_id"] == "NBENTRYRESTART"


@pytest.mark.parametrize(
    "exchange_algo_state,expected_local_state",
    [("live", "active"), ("effective", "triggered")],
)
def test_restart_reconciles_existing_oco_without_writes_under_emergency_stop(
    tmp_path, exchange_algo_state, expected_local_state
):
    db_path = str(tmp_path / f"restart-oco-{exchange_algo_state}.sqlite3")
    first = StateStore(db_path)
    entry_id = seed_position(first)
    first.update_protection_intent(entry_id, "submission_unknown")

    class ReadOnlyOcoClient:
        settings = stopped_settings()

        def __init__(self):
            self.reads = 0
            self.writes = 0

        def get_algo_order(self, algo_client_order_id):
            self.reads += 1
            return [{
                "algoClOrdId": algo_client_order_id,
                "algoId": "998877",
                "state": exchange_algo_state,
                "ordIdList": [],
            }]

        def place_spot_oco_exit(self, **kwargs):
            self.writes += 1
            raise AssertionError("Restart recovery must not submit or replace OCO")

        def cancel_algo_order(self, **kwargs):
            self.writes += 1
            raise AssertionError("Restart recovery must not cancel protective OCO")

    client = ReadOnlyOcoClient()
    restarted = StateStore(db_path)
    manager = DemoProtectionManager(client, restarted)

    result = manager.reconcile(entry_id)

    assert result["status"] == expected_local_state
    assert client.reads == 1
    assert client.writes == 0
    assert restarted.get_position_lock()["owner_client_order_id"] == entry_id


def test_failed_restart_oco_lookup_keeps_protection_and_position_locked(tmp_path):
    db_path = str(tmp_path / "restart-oco-failure.sqlite3")
    first = StateStore(db_path)
    entry_id = seed_position(first)
    first.update_protection_intent(entry_id, "submission_unknown")

    class UnavailableReadClient:
        settings = stopped_settings()

        def get_algo_order(self, algo_client_order_id):
            raise TimeoutError("simulated read timeout")

        def place_spot_oco_exit(self, **kwargs):
            raise AssertionError("Must not write after failed recovery lookup")

    restarted = StateStore(db_path)
    manager = DemoProtectionManager(UnavailableReadClient(), restarted)

    with pytest.raises(ProtectionManagerError, match="position remains locked"):
        manager.reconcile(entry_id)

    assert restarted.get_protection_intent(entry_id)["status"] == "submission_unknown"
    assert restarted.get_position_lock()["owner_client_order_id"] == entry_id


def test_kill_switch_does_not_block_regular_private_reads():
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"code": "0", "data": [{"details": []}]}

    class ReadSession:
        def __init__(self):
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return Response()

    session = ReadSession()
    client = OkxDemoClient(stopped_settings(), session=session)

    rows = client.get_balance()

    assert rows == [{"details": []}]
    assert len(session.calls) == 1
    assert session.calls[0][0][0] == "GET"
    assert session.calls[0][1]["headers"]["x-simulated-trading"] == "1"
