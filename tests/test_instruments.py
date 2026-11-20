import pytest
from nebula.instruments import SpotInstrument, InstrumentRuleError


def spec(**overrides):
    row = {"instType": "SPOT", "instId": "BTC-USDT", "baseCcy": "BTC", "quoteCcy": "USDT",
           "tickSz": "0.1", "lotSz": "0.001", "minSz": "0.001", "state": "live"}
    row.update(overrides)
    return SpotInstrument.from_okx(row)


def test_buy_price_and_size_round_down():
    assert spec().normalize_order("buy", "0.0129", "50000.19") == ("0.012", "50000.1")


def test_sell_price_rounds_up():
    assert spec().normalize_order("sell", "0.0129", "50000.11") == ("0.012", "50000.2")


def test_rejects_below_min_size():
    with pytest.raises(InstrumentRuleError, match="below OKX minSz"):
        spec().normalize_order("buy", "0.0009", "50000")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "0"])
def test_rejects_invalid_tick_size(value):
    with pytest.raises(InstrumentRuleError):
        spec(tickSz=value)


def test_rejects_non_live_instrument():
    with pytest.raises(InstrumentRuleError, match="not tradable"):
        spec(state="suspend").normalize_order("buy", "0.01", "50000")


def test_rejects_non_spot_instrument():
    with pytest.raises(InstrumentRuleError, match="Only SPOT"):
        spec(instType="SWAP")
