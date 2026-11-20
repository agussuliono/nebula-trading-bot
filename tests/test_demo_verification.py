from nebula.config import DEMO_FLAG, DEMO_REST_URL, Settings
from nebula.demo_verification import verify_demo_read_only


class ReadOnlyDemoStub:
    base_url = DEMO_REST_URL

    def __init__(self, *, config=None, balances=None, regular=None, oco=None,
                 conditional=None, private_error=None):
        self.config = config or {
            "acctLv": "1", "autoLoan": False, "enableSpotBorrow": False,
            "perm": "read_only,trade", "uid": "redacted-test-id",
        }
        self.balances = balances if balances is not None else [{
            "details": [{"ccy": "USDT", "availBal": "1000", "frozenBal": "0"}]
        }]
        self.regular = regular or []
        self.oco = oco or []
        self.conditional = conditional or []
        self.private_error = private_error
        self.calls = []

    def _headers(self, method, path):
        return {"x-simulated-trading": DEMO_FLAG}

    def get_instruments(self):
        self.calls.append("get_instruments")
        return [{"instId": "BTC-USDT"}]

    def get_tickers(self):
        self.calls.append("get_tickers")
        return [{"instId": "BTC-USDT", "bidPx": "1", "askPx": "2"}]

    def _private(self, name, value):
        self.calls.append(name)
        if self.private_error == name:
            raise TimeoutError("simulated private read timeout")
        return value

    def get_account_config(self):
        return self._private("get_account_config", [self.config])

    def get_balance(self):
        return self._private("get_balance", self.balances)

    def get_pending_orders(self, inst_type):
        assert inst_type == "SPOT"
        return self._private("get_pending_orders", self.regular)

    def get_pending_algo_orders(self, ord_type):
        assert ord_type in {"oco", "conditional"}
        return self._private("get_pending_algo_orders_" + ord_type,
                             self.oco if ord_type == "oco" else self.conditional)


def credentials():
    return Settings(api_key="demo-key", secret_key="demo-secret", passphrase="demo-pass")


def test_read_only_demo_verification_checks_public_private_and_spot_safety():
    client = ReadOnlyDemoStub()
    result = verify_demo_read_only(client, credentials())

    assert result["status"] == "read_only_demo_connection_verified"
    assert result["public_api"] == "ok"
    assert result["private_api"] == "ok"
    assert result["account_mode"] == "spot"
    assert result["api_permissions"] == "compliant"
    assert result["open_orders"] == "clear"
    assert result["exchange_writes"] == 0
    assert result["orders_enabled"] is False
    assert result["private_reads_attempted"] is True
    assert "uid" not in str(result)
    assert all(not name.startswith(("place", "cancel", "submit")) for name in client.calls)


def test_withdraw_permission_is_a_blocker_and_no_write_is_possible():
    client = ReadOnlyDemoStub(config={
        "acctLv": "1", "autoLoan": False, "enableSpotBorrow": False,
        "perm": "read_only,trade,withdraw",
    })
    result = verify_demo_read_only(client, credentials())

    assert result["status"] == "connected_but_not_ready"
    assert "api_key_has_withdraw_permission" in result["blockers"]
    assert result["exchange_writes"] == 0


def test_non_spot_or_borrow_enabled_account_is_blocked():
    client = ReadOnlyDemoStub(config={
        "acctLv": "2", "autoLoan": True, "enableSpotBorrow": True,
        "perm": "read_only,trade",
    })
    result = verify_demo_read_only(client, credentials())

    assert result["status"] == "connected_but_not_ready"
    assert "account_not_in_spot_mode" in result["blockers"]
    assert "auto_loan_enabled_or_unknown" in result["blockers"]
    assert "spot_borrow_enabled_or_unknown" in result["blockers"]


def test_open_orders_and_non_quote_assets_are_reported_as_blockers():
    client = ReadOnlyDemoStub(
        balances=[{"details": [
            {"ccy": "USDT", "availBal": "100", "frozenBal": "0"},
            {"ccy": "BTC", "availBal": "0.01", "frozenBal": "0"},
        ]}],
        regular=[{"instId": "ETH-USDT"}],
    )
    result = verify_demo_read_only(client, credentials())

    assert result["status"] == "connected_but_not_ready"
    assert result["non_quote_holdings"] == ["BTC"]
    assert "open_exchange_orders_present" in result["blockers"]
    assert "non_quote_asset_balances_present" in result["blockers"]


def test_missing_credentials_short_circuit_before_any_network_read():
    client = ReadOnlyDemoStub()
    result = verify_demo_read_only(client, Settings())

    assert result["status"] == "blocked"
    assert result["blockers"] == ["private_read_credentials_missing"]
    assert client.calls == []
    assert result["exchange_writes"] == 0


def test_private_read_timeout_fails_closed_without_retry_or_writes():
    client = ReadOnlyDemoStub(private_error="get_balance")
    result = verify_demo_read_only(client, credentials())

    assert result["status"] == "blocked"
    assert result["private_api"] == "failed"
    assert result["account_snapshot"] == "incomplete"
    assert result["exchange_writes"] == 0
    assert client.calls.count("get_balance") == 1
