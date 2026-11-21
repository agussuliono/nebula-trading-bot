"""Offline HTTP-contract integration tests against the OKX V5 Demo REST schema.

No network, API credentials, or exchange account is used by these tests.
"""
import base64
import hashlib
import hmac
import json

import pytest
import requests

from nebula.config import Settings
from nebula.exchange import OkxDemoClient


class Response:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}

    def raise_for_status(self):
        return None

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class RecordingSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP request")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def enabled_settings():
    return Settings(
        api_key="demo-api-key",
        secret_key="demo-secret-key",
        passphrase="demo-passphrase",
        enable_demo_orders=True,
        kill_switch=False,
        max_daily_loss_quote=100,
        max_risk_quote=5,
        max_position_quote=200,
    )


def test_demo_private_read_contract_signs_exact_path_and_always_marks_demo():
    session = RecordingSession(Response({"code": "0", "msg": "", "data": []}))
    client = OkxDemoClient(enabled_settings(), session=session)

    assert client.get_pending_algo_orders("oco") == []

    args, kwargs = session.calls[0]
    method, url = args[:2]
    headers = kwargs["headers"]
    path = "/api/v5/trade/orders-algo-pending?ordType=oco&instType=SPOT&limit=100"
    timestamp = headers["OK-ACCESS-TIMESTAMP"]
    expected = base64.b64encode(
        hmac.new(
            b"demo-secret-key",
            f"{timestamp}GET{path}".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()

    assert method == "GET"
    assert url == "https://openapi.okx.com" + path
    assert headers["x-simulated-trading"] == "1"
    assert headers["OK-ACCESS-KEY"] == "demo-api-key"
    assert headers["OK-ACCESS-SIGN"] == expected
    assert kwargs["allow_redirects"] is False
    assert "instType=SPOT" in url


def test_algo_history_uses_only_documented_ordtype_and_state_filters():
    session = RecordingSession(Response({"code": "0", "msg": "", "data": []}))
    client = OkxDemoClient(enabled_settings(), session=session)

    assert client.get_algo_order_history("oco", "effective") == []

    url = session.calls[0][0][1]
    assert url.endswith("/api/v5/trade/orders-algo-history?ordType=oco&state=effective&instType=SPOT")
    assert "instType=SPOT" in url


def test_spot_oco_contract_has_cash_mode_market_exit_prices_and_demo_header():
    session = RecordingSession(Response({
        "code": "0", "msg": "",
        "data": [{"algoId": "123456789", "algoClOrdId": "NBP123", "sCode": "0", "sMsg": ""}],
    }))
    client = OkxDemoClient(enabled_settings(), session=session)
    client.get_spot_instrument = lambda inst_id: {
        "instType": "SPOT", "instId": inst_id, "baseCcy": "BTC", "quoteCcy": "USDT",
        "tickSz": "0.1", "lotSz": "0.0001", "minSz": "0.0001", "state": "live",
    }

    result = client.place_spot_oco_exit(
        inst_id="BTC-USDT",
        size="0.001",
        take_profit="70000",
        stop_loss="60000",
        algo_client_order_id="NBP123",
    )

    assert result[0]["algoId"] == "123456789"
    args, kwargs = session.calls[0]
    assert args[0] == "POST"
    assert args[1] == "https://openapi.okx.com/api/v5/trade/order-algo"
    assert kwargs["headers"]["x-simulated-trading"] == "1"
    assert kwargs["allow_redirects"] is False
    payload = json.loads(kwargs["data"])
    assert payload == {
        "instId": "BTC-USDT",
        "tdMode": "cash",
        "side": "sell",
        "ordType": "oco",
        "sz": "0.001",
        "algoClOrdId": "NBP123",
        "tpTriggerPx": "70000",
        "tpOrdPx": "-1",
        "tpTriggerPxType": "last",
        "slTriggerPx": "60000",
        "slOrdPx": "-1",
        "slTriggerPxType": "last",
    }


def test_oco_rejection_from_exchange_is_not_reported_as_success():
    session = RecordingSession(Response({
        "code": "0", "msg": "",
        "data": [{"algoId": "", "sCode": "51000", "sMsg": "Parameter error"}],
    }))
    client = OkxDemoClient(enabled_settings(), session=session)
    client.get_spot_instrument = lambda inst_id: {
        "instType": "SPOT", "instId": inst_id, "baseCcy": "BTC", "quoteCcy": "USDT",
        "tickSz": "0.1", "lotSz": "0.0001", "minSz": "0.0001", "state": "live",
    }

    from nebula.exchange import OkxError
    import pytest
    with pytest.raises(OkxError, match="OKX OCO rejected 51000"):
        client.place_spot_oco_exit(
            inst_id="BTC-USDT", size="0.001",
            take_profit="70000", stop_loss="60000",
            algo_client_order_id="NBP123",
        )
    assert len(session.calls) == 1


def test_spot_regular_pending_orders_keep_the_supported_insttype_filter():
    session = RecordingSession(Response({"code": "0", "msg": "", "data": []}))
    client = OkxDemoClient(enabled_settings(), session=session)

    assert client.get_pending_orders("SPOT") == []
    url = session.calls[0][0][1]
    assert url.endswith("/api/v5/trade/orders-pending?instType=SPOT&limit=100")



def test_pending_regular_orders_paginate_to_complete_account_snapshot():
    first_page = [{"ordId": str(index), "instId": "BTC-USDT"} for index in range(100, 0, -1)]
    second_page = [{"ordId": "0", "instId": "ETH-USDT"}]
    session = RecordingSession(
        Response({"code": "0", "msg": "", "data": first_page}),
        Response({"code": "0", "msg": "", "data": second_page}),
    )
    client = OkxDemoClient(enabled_settings(), session=session)

    rows = client.get_pending_orders("SPOT")

    assert len(rows) == 101
    assert session.calls[0][0][1].endswith(
        "/api/v5/trade/orders-pending?instType=SPOT&limit=100"
    )
    assert session.calls[1][0][1].endswith(
        "/api/v5/trade/orders-pending?instType=SPOT&limit=100&after=1"
    )


def test_pending_algo_orders_paginate_by_algo_id():
    first_page = [{"algoId": str(index), "instId": "BTC-USDT"} for index in range(100, 0, -1)]
    second_page = [{"algoId": "0", "instId": "ETH-USDT"}]
    session = RecordingSession(
        Response({"code": "0", "msg": "", "data": first_page}),
        Response({"code": "0", "msg": "", "data": second_page}),
    )
    client = OkxDemoClient(enabled_settings(), session=session)

    rows = client.get_pending_algo_orders("oco")

    assert len(rows) == 101
    assert session.calls[0][0][1].endswith(
        "/api/v5/trade/orders-algo-pending?ordType=oco&instType=SPOT&limit=100"
    )
    assert session.calls[1][0][1].endswith(
        "/api/v5/trade/orders-algo-pending?ordType=oco&instType=SPOT&limit=100&after=1"
    )


def test_pending_order_pagination_rejects_non_advancing_or_duplicate_cursor():
    page = [{"ordId": str(index)} for index in range(100, 0, -1)]
    repeated_page = [{"ordId": "1"}]
    session = RecordingSession(
        Response({"code": "0", "msg": "", "data": page}),
        Response({"code": "0", "msg": "", "data": repeated_page}),
    )
    client = OkxDemoClient(enabled_settings(), session=session)

    from nebula.exchange import OkxError
    import pytest
    with pytest.raises(OkxError, match="missing or duplicate cursor"):
        client.get_pending_orders("SPOT")



def test_read_only_demo_verification_uses_only_demo_marked_get_requests():
    from nebula.demo_verification import verify_demo_read_only

    session = RecordingSession(
        Response({"code": "0", "msg": "", "data": [{"instId": "BTC-USDT"}]}),
        Response({"code": "0", "msg": "", "data": [{"instId": "BTC-USDT"}]}),
        Response({"code": "0", "msg": "", "data": [{
            "acctLv": "1", "autoLoan": False, "enableSpotBorrow": False,
            "perm": "read_only",
        }]}),
        Response({"code": "0", "msg": "", "data": [{
            "details": [{"ccy": "USDT", "availBal": "100", "frozenBal": "0"}],
        }]}),
        Response({"code": "0", "msg": "", "data": []}),
        Response({"code": "0", "msg": "", "data": []}),
        Response({"code": "0", "msg": "", "data": []}),
    )
    settings = enabled_settings()
    client = OkxDemoClient(settings, session=session)

    report = verify_demo_read_only(client, settings)

    assert report["status"] == "read_only_demo_connection_verified"
    assert report["exchange_writes"] == 0
    assert len(session.calls) == 7
    assert all(call[0][0] == "GET" for call in session.calls)
    assert all(call[1]["headers"]["x-simulated-trading"] == "1" for call in session.calls)
    assert all(call[1]["allow_redirects"] is False for call in session.calls)


def test_public_403_non_json_response_has_safe_structured_diagnostics_and_no_retry():
    from nebula.exchange import OkxError

    response = Response(
        ValueError("SECRET_RESPONSE_BODY_MUST_NOT_LEAK"),
        status_code=403,
        headers={"Content-Type": "text/html; charset=UTF-8", "Server": "cloudflare"},
    )
    response.text = "SECRET_RESPONSE_BODY_MUST_NOT_LEAK"
    session = RecordingSession(
        response,
        Response({"code": "0", "msg": "", "data": []}),
    )
    client = OkxDemoClient(Settings(), session=session)

    with pytest.raises(OkxError, match="HTTP 403") as caught:
        client.get_instruments()

    diagnostic = caught.value.diagnostic()
    assert diagnostic["http_status"] == 403
    assert diagnostic["content_type"] == "text/html"
    assert "SECRET_RESPONSE_BODY_MUST_NOT_LEAK" not in str(caught.value)
    assert "SECRET_RESPONSE_BODY_MUST_NOT_LEAK" not in repr(diagnostic)
    assert len(session.calls) == 1

    # A definitive 403 is not a transient transport failure and does not open
    # the public circuit; a subsequent request is allowed.
    assert client.get_instruments() == []
    assert len(session.calls) == 2


def test_public_get_retries_only_bounded_transient_http_statuses(monkeypatch):
    delays = []
    monkeypatch.setattr("nebula.exchange.time.sleep", delays.append)
    session = RecordingSession(
        Response({}, status_code=503),
        Response({}, status_code=429),
        Response({"code": "0", "msg": "", "data": []}),
    )
    client = OkxDemoClient(Settings(), session=session)

    assert client.get_tickers() == []
    assert len(session.calls) == 3
    assert delays == [0.25, 0.5]
    assert all(call[0][0] == "GET" for call in session.calls)
    assert all(call[1]["allow_redirects"] is False for call in session.calls)


def test_public_get_retries_transient_network_errors_but_private_reads_do_not(monkeypatch):
    delays = []
    monkeypatch.setattr("nebula.exchange.time.sleep", delays.append)
    public_session = RecordingSession(
        requests.Timeout("timeout"),
        requests.ConnectionError("dns"),
        Response({"code": "0", "msg": "", "data": []}),
    )
    assert OkxDemoClient(Settings(), session=public_session).get_instruments() == []
    assert len(public_session.calls) == 3
    assert delays == [0.25, 0.5]

    private_session = RecordingSession(Response({}, status_code=503))
    client = OkxDemoClient(enabled_settings(), session=private_session)
    from nebula.exchange import OkxError
    with pytest.raises(OkxError, match="HTTP 503"):
        client.get_balance()
    assert len(private_session.calls) == 1


def test_public_circuit_breaker_opens_after_three_exhausted_calls_and_recovers(monkeypatch):
    from nebula.exchange import OkxError

    clock = [100.0]
    monkeypatch.setattr("nebula.exchange.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("nebula.exchange.time.sleep", lambda _delay: None)
    session = RecordingSession(*[Response({}, status_code=503) for _ in range(9)])
    client = OkxDemoClient(Settings(), session=session)

    for _ in range(3):
        with pytest.raises(OkxError, match="HTTP 503"):
            client.get_instruments()
    assert len(session.calls) == 9

    with pytest.raises(OkxError, match="circuit breaker is open"):
        client.get_instruments()
    assert len(session.calls) == 9

    clock[0] += 16
    session.responses.append(Response({"code": "0", "msg": "", "data": []}))
    assert client.get_instruments() == []
    assert len(session.calls) == 10


def test_private_cancel_write_is_never_retried_after_http_failure():
    from nebula.exchange import OkxError

    session = RecordingSession(Response({}, status_code=503))
    client = OkxDemoClient(enabled_settings(), session=session)

    with pytest.raises(OkxError, match="HTTP 503"):
        client.cancel_order(inst_id="BTC-USDT", order_id="12345")
    assert len(session.calls) == 1
    assert session.calls[0][0][0] == "POST"


def test_public_request_host_and_demo_header_ignore_environment_override(monkeypatch):
    monkeypatch.setenv("OKX_BASE_URL", "https://www.okx.com")
    session = RecordingSession(Response({"code": "0", "msg": "", "data": []}))
    client = OkxDemoClient(Settings(), session=session)

    assert client.get_tickers() == []
    args, kwargs = session.calls[0]
    assert args[1].startswith("https://openapi.okx.com/")
    assert kwargs["headers"]["x-simulated-trading"] == "1"
    assert kwargs["allow_redirects"] is False
