import pytest
from nebula.protection import build_spot_oco_exit, ProtectionError
from nebula.exchange import OkxDemoClient
from nebula.config import Settings, SafetyError


def test_oco_uses_filled_quantity_and_spot_cash_mode():
    p = build_spot_oco_exit(instrument="BTC-USDT", filled_size="0.004",
                            take_profit="52000", stop_loss="48000", algo_client_order_id="NBTP001")
    assert p["sz"] == "0.004"
    assert p["side"] == "sell" and p["tdMode"] == "cash" and p["ordType"] == "oco"
    assert p["tpOrdPx"] == p["slOrdPx"] == "-1"


@pytest.mark.parametrize("kwargs", [
    {"instrument":"BTC-USDT", "filled_size":"0", "take_profit":"10", "stop_loss":"5", "algo_client_order_id":"NB1"},
    {"instrument":"BTC-USDT", "filled_size":"1", "take_profit":"5", "stop_loss":"5", "algo_client_order_id":"NB1"},
    {"instrument":"BTC-USDT", "filled_size":"NaN", "take_profit":"10", "stop_loss":"5", "algo_client_order_id":"NB1"},
    {"instrument":"BTC-USD", "filled_size":"1", "take_profit":"10", "stop_loss":"5", "algo_client_order_id":"NB1"},
    {"instrument":"BTC-USDT", "filled_size":"1", "take_profit":"10", "stop_loss":"5", "algo_client_order_id":"bad-id"},
])
def test_invalid_oco_plans_rejected(kwargs):
    with pytest.raises(ProtectionError):
        build_spot_oco_exit(**kwargs)


def test_oco_submission_stays_disabled_by_default():
    client = OkxDemoClient(Settings())
    with pytest.raises(SafetyError):
        client.place_spot_oco_exit(inst_id="BTC-USDT", size="0.001", take_profit="10",
                                   stop_loss="9", algo_client_order_id="NBTP001")


def test_oco_payload_is_demo_marked_and_uses_market_exit():
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"code":"0", "data":[{"algoId":"123", "sCode":"0"}]}
    class Session:
        def __init__(self): self.call=None
        def request(self, *a, **kw): self.call=(a,kw); return Response()
    settings=Settings(api_key="k", secret_key="s", passphrase="p", enable_demo_orders=True,
                      max_daily_loss_quote=20, max_risk_quote=2, max_position_quote=50)
    session=Session(); client=OkxDemoClient(settings, session=session)
    result=client.place_spot_oco_exit(inst_id="BTC-USDT", size="0.004", take_profit="52000",
                                      stop_loss="48000", algo_client_order_id="NBTP001")
    assert result[0]["algoId"] == "123"
    args, kw=session.call
    assert args[0] == "POST" and args[1].endswith("/api/v5/trade/order-algo")
    assert kw["headers"]["x-simulated-trading"] == "1"
    assert '"ordType":"oco"' in kw["data"]
    assert '"side":"sell"' in kw["data"]


def test_algo_order_details_lookup_uses_persisted_client_algo_id():
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"code":"0", "data":[{"algoClOrdId":"NBP123", "state":"live"}]}
    class Session:
        def __init__(self): self.call=None
        def request(self, *a, **kw): self.call=(a,kw); return Response()
    client=OkxDemoClient(Settings(api_key="k", secret_key="s", passphrase="p"), session=Session())
    rows=client.get_algo_order("NBP123")
    assert rows[0]["state"] == "live"
    args, kwargs=client.session.call
    assert args[0] == "GET" and args[1].endswith("/api/v5/trade/order-algo?algoClOrdId=NBP123")
    assert kwargs["headers"]["x-simulated-trading"] == "1"
