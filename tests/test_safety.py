import pytest
from nebula.config import Settings, SafetyError, DEMO_FLAG
from nebula.exchange import OkxDemoClient


def test_orders_disabled_by_default():
    with pytest.raises(SafetyError):
        Settings().validate_order_permission()


def test_kill_switch_blocks_orders():
    s = Settings(api_key="a", secret_key="b", passphrase="c", enable_demo_orders=True,
                 kill_switch=True, max_daily_loss_quote=10, max_risk_quote=2, max_position_quote=20)
    with pytest.raises(SafetyError):
        s.validate_order_permission()


def test_risk_limits_required():
    s = Settings(api_key="a", secret_key="b", passphrase="c", enable_demo_orders=True, kill_switch=False)
    with pytest.raises(SafetyError):
        s.validate_order_permission()


def test_header_is_demo_only():
    s = Settings(api_key="a", secret_key="b", passphrase="c")
    headers = OkxDemoClient(s)._headers("GET", "/api/v5/account/balance", private=True)
    assert headers["x-simulated-trading"] == DEMO_FLAG
    assert "OK-ACCESS-SIGN" in headers


def test_order_request_is_guarded_and_marked_demo(tmp_path):
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"code": "0", "data": [{"sCode": "0"}]}

    class Session:
        def __init__(self):
            self.call = None
        def request(self, *args, **kwargs):
            self.call = (args, kwargs)
            return Response()

    s = Settings(api_key="demo-key", secret_key="demo-secret", passphrase="demo-pass",
                 enable_demo_orders=True, kill_switch=False, max_daily_loss_quote=20,
                 max_risk_quote=2, max_position_quote=100)
    session = Session()
    client = OkxDemoClient(s, session=session)
    # The adapter requires the one-shot startup permit even when the Demo
    # write gate is explicitly enabled. This unit test uses the internal issuer;
    # production permits are issued only by a successful StartupRecovery run.
    from nebula.entry_gate import issue_startup_entry_permit
    from nebula.state import StateStore
    state = StateStore(str(tmp_path / "startup-permit.db"))
    permit = issue_startup_entry_permit(client, state, "demo-test-uid")
    client.install_startup_entry_permit(permit, state)
    permit.consume(client, state)
    result = client.place_spot_limit(inst_id="BTC-USDT", side="buy", size="0.001",
                                     price="50000", client_order_id="NB123")
    assert result[0]["sCode"] == "0"
    args, kwargs = session.call
    assert args[0] == "POST"
    assert args[1].startswith("https://openapi.okx.com/api/v5/trade/order")
    assert kwargs["headers"]["x-simulated-trading"] == "1"
    assert '"tdMode":"cash"' in kwargs["data"]


def test_live_host_cannot_be_selected_from_environment(monkeypatch):
    monkeypatch.setenv("OKX_BASE_URL", "https://openapi.okx.com")
    client = OkxDemoClient(Settings())
    assert client.base_url == "https://openapi.okx.com"


def test_non_finite_risk_limits_rejected():
    s = Settings(api_key="a", secret_key="b", passphrase="c", enable_demo_orders=True, kill_switch=False,
                 max_daily_loss_quote=float("inf"), max_risk_quote=2, max_position_quote=20)
    with pytest.raises(SafetyError):
        s.validate_order_permission()


def test_order_id_must_be_alphanumeric():
    s = Settings(api_key="a", secret_key="b", passphrase="c", enable_demo_orders=True, kill_switch=False,
                 max_daily_loss_quote=20, max_risk_quote=2, max_position_quote=100)
    client = OkxDemoClient(s)
    with pytest.raises(ValueError):
        client.place_spot_limit(inst_id="BTC-USDT", side="buy", size="1", price="10", client_order_id="bad-id!")


def test_per_trade_risk_cannot_exceed_daily_loss_limit():
    s = Settings(api_key="a", secret_key="b", passphrase="c", enable_demo_orders=True, kill_switch=False,
                 max_daily_loss_quote=5, max_risk_quote=10, max_position_quote=100)
    with pytest.raises(SafetyError):
        s.validate_order_permission()


def test_nan_order_size_rejected():
    s = Settings(api_key="a", secret_key="b", passphrase="c", enable_demo_orders=True, kill_switch=False,
                 max_daily_loss_quote=20, max_risk_quote=2, max_position_quote=100)
    client = OkxDemoClient(s)
    with pytest.raises(ValueError):
        client.place_spot_limit(inst_id="BTC-USDT", side="buy", size="nan", price="10", client_order_id="NB123")


def test_timeout_is_reported_as_ambiguous_and_not_retried():
    import requests
    from nebula.exchange import OkxError
    class TimeoutSession:
        def __init__(self): self.calls = 0
        def request(self, *args, **kwargs):
            self.calls += 1
            raise requests.Timeout("timeout")
    session = TimeoutSession()
    client = OkxDemoClient(Settings(), session=session)
    with pytest.raises(OkxError, match="reconcile before retry"):
        client.request("GET", "/api/v5/market/tickers", {"instType":"SPOT"})
    assert session.calls == 1


def test_cancel_order_is_demo_gated_and_uses_fixed_demo_header():
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"code":"0", "data":[{"ordId":"123", "sCode":"0"}]}
    class Session:
        def __init__(self): self.call=None
        def request(self, *args, **kwargs): self.call=(args,kwargs); return Response()
    settings=Settings(api_key="demo-key",secret_key="demo-secret",passphrase="demo-pass",
        enable_demo_orders=True, kill_switch=False,max_daily_loss_quote=20,max_risk_quote=2,max_position_quote=100)
    session=Session(); client=OkxDemoClient(settings,session=session)
    result=client.cancel_order(inst_id="BTC-USDT",order_id="123")
    assert result[0]["sCode"] == "0"
    args,kwargs=session.call
    assert args[0] == "POST"
    assert args[1].endswith("/api/v5/trade/cancel-order")
    assert kwargs["headers"]["x-simulated-trading"] == "1"
    assert '"ordId":"123"' in kwargs["data"]


def test_cancel_order_requires_gate_and_valid_exchange_id():
    client=OkxDemoClient(Settings())
    with pytest.raises(SafetyError):
        client.cancel_order(inst_id="BTC-USDT",order_id="123")
    enabled=Settings(api_key="a",secret_key="b",passphrase="c",enable_demo_orders=True, kill_switch=False,
        max_daily_loss_quote=20,max_risk_quote=2,max_position_quote=100)
    with pytest.raises(ValueError):
        OkxDemoClient(enabled).cancel_order(inst_id="BTC-USDT",order_id="bad")


def test_read_only_order_and_fill_history_endpoints_use_spot_filters():
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"code":"0", "data":[]}
    class Session:
        def __init__(self): self.calls=[]
        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs)); return Response()
    session=Session(); client=OkxDemoClient(Settings(api_key="a",secret_key="b",passphrase="c"),session=session)
    client.get_order_by_id("BTC-USDT", "123")
    client.get_fills_history("BTC-USDT", "123", limit=50, after="999")
    assert session.calls[0][0][1].endswith("/api/v5/trade/order?instId=BTC-USDT&ordId=123")
    assert "/api/v5/trade/fills-history?" in session.calls[1][0][1]
    assert "instType=SPOT" in session.calls[1][0][1]
    assert "ordId=123" in session.calls[1][0][1]
    assert "after=999" in session.calls[1][0][1]
    assert all(call[1]["headers"]["x-simulated-trading"] == "1" for call in session.calls)


def test_fill_history_rejects_bad_cursor_and_non_numeric_order_id():
    client=OkxDemoClient(Settings())
    with pytest.raises(ValueError):
        client.get_order_by_id("BTC-USDT", "bad")
    with pytest.raises(ValueError):
        client.get_fills_history("BTC-USDT", "bad")
    with pytest.raises(ValueError):
        client.get_fills_history("BTC-USDT", "123", after="not-a-bill-id")


def test_portfolio_account_reads_are_private_get_only_and_demo_marked():
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"code": "0", "data": [{"acctLv": "1", "autoLoan": False,
                                                       "enableSpotBorrow": False, "perm": "read_only"}]}
    class Session:
        def __init__(self): self.calls=[]
        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return Response()
    session = Session()
    client = OkxDemoClient(Settings(api_key="read-key", secret_key="read-secret", passphrase="read-pass"), session=session)
    client.get_account_config()
    client.get_balance()
    assert [call[0][0] for call in session.calls] == ["GET", "GET"]
    assert session.calls[0][0][1].endswith("/api/v5/account/config")
    assert session.calls[1][0][1].endswith("/api/v5/account/balance")
    assert all(call[1]["headers"]["x-simulated-trading"] == "1" for call in session.calls)


def test_portfolio_assumption_environment_is_validated(monkeypatch):
    monkeypatch.setenv("NEBULA_FEE_BPS", "12.5")
    monkeypatch.setenv("NEBULA_SLIPPAGE_BPS", "20")
    monkeypatch.setenv("NEBULA_MARKET_DATA_MAX_AGE_MS", "15000")
    settings = Settings.from_env()
    assert settings.fee_bps == 12.5
    assert settings.slippage_bps == 20
    assert settings.market_data_max_age_ms == 15000
    monkeypatch.setenv("NEBULA_MARKET_DATA_MAX_AGE_MS", "1.5")
    with pytest.raises(ValueError, match="must be an integer"):
        Settings.from_env()


def test_history_candles_uses_public_endpoint_and_validates_cursor():
    from nebula.config import Settings
    from nebula.exchange import OkxDemoClient
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"code": "0", "data": []}
    class Session:
        def __init__(self): self.call = None
        def request(self, *args, **kwargs):
            self.call = (args, kwargs)
            return Response()
    session = Session()
    client = OkxDemoClient(Settings(), session=session)
    assert client.get_history_candles("BTC-USDT", "1H", 100, after="1700000000000") == []
    assert "/api/v5/market/history-candles" in session.call[0][1]
    assert "x-simulated-trading" in session.call[1]["headers"]
    with pytest.raises(ValueError):
        client.get_history_candles("BTC-USDT", limit=101)
    with pytest.raises(ValueError):
        client.get_history_candles("BTC-USDT", after="not-a-timestamp")



def test_unreviewed_endpoint_is_blocked_before_network_io():
    from nebula.config import SafetyError
    class NeverCallSession:
        def request(self, *args, **kwargs):
            raise AssertionError("network must not be called")
    client = OkxDemoClient(Settings(), session=NeverCallSession())
    with pytest.raises(SafetyError, match="not allowlisted"):
        client.request("POST", "/api/v5/account/set-position-mode",
                       payload={"posMode": "long_short_mode"}, private=True)


def test_write_endpoint_cannot_bypass_demo_order_gate():
    from nebula.config import SafetyError
    class NeverCallSession:
        def request(self, *args, **kwargs):
            raise AssertionError("network must not be called")
    client = OkxDemoClient(Settings(), session=NeverCallSession())
    with pytest.raises(SafetyError, match="disabled"):
        client.request("POST", "/api/v5/trade/order",
                       payload={"instId": "BTC-USDT"}, private=True)


def test_regular_order_cancel_remains_available_during_emergency_stop():
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"code": "0", "data": [{"ordId": "123", "sCode": "0"}]}
    class Session:
        def __init__(self): self.calls = 0
        def request(self, *args, **kwargs):
            self.calls += 1
            return Response()
    settings = Settings(api_key="demo-key", secret_key="demo-secret", passphrase="demo-pass",
        enable_demo_orders=True, kill_switch=True, max_daily_loss_quote=20,
        max_risk_quote=2, max_position_quote=100)
    session = Session()
    client = OkxDemoClient(settings, session=session)
    assert client.cancel_order(inst_id="BTC-USDT", order_id="123")[0]["sCode"] == "0"
    assert session.calls == 1


@pytest.mark.parametrize(
    "method,path,payload,status_code",
    [
        ("GET", "/api/v5/account/balance", None, 302),
        ("POST", "/api/v5/trade/order", {"instId": "BTC-USDT"}, 307),
    ],
)
def test_http_redirects_are_rejected_without_following_or_replaying_private_headers(
    method, path, payload, status_code
):
    from nebula.exchange import OkxError

    class RedirectResponse:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            raise AssertionError("redirect must be rejected before normal status handling")

        def json(self):
            raise AssertionError("redirect response must not be parsed as an OKX payload")

    class RedirectSession:
        def __init__(self):
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return RedirectResponse(status_code)

    settings = Settings(
        api_key="demo-key",
        secret_key="demo-secret",
        passphrase="demo-pass",
        enable_demo_orders=True,
        kill_switch=False,
        max_daily_loss_quote=20,
        max_risk_quote=2,
        max_position_quote=100,
    )
    session = RedirectSession()
    client = OkxDemoClient(settings, session=session)

    with pytest.raises(OkxError, match="redirect"):
        client.request(method, path, payload=payload, private=True)

    # The HTTP client must not follow a redirect to another host or replay a
    # signed POST; exactly one request is made to the fixed Demo REST host.
    assert len(session.calls) == 1
    args, kwargs = session.calls[0]
    assert args[1].startswith("https://openapi.okx.com/")
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["OK-ACCESS-KEY"] == "demo-key"
    assert kwargs["headers"]["x-simulated-trading"] == "1"
