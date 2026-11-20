from types import SimpleNamespace

import pytest

from nebula.entry_plan import EntryPlanError, EntryPlanPolicy, build_entry_plan
from nebula.instruments import SpotInstrument
from decimal import Decimal


BAR_MS = 3_600_000
BASE_TS = 1_700_000_000_000


def _instrument():
    return SpotInstrument(
        inst_id="AAA-USDT", base_ccy="AAA", quote_ccy="USDT",
        tick_size=Decimal("0.1"), lot_size=Decimal("0.001"),
        min_size=Decimal("0.001"), state="live",
    )


def _candles(*, zero_volume=False):
    rows = []
    for index, close in enumerate((100, 101, 102)):
        ts = BASE_TS + index * BAR_MS
        volume = "0" if zero_volume and index == 2 else "10"
        rows.append([str(ts), str(close), str(close + 2), str(close - 2),
                     str(close), volume, "1000", "1000", "1"])
    return list(reversed(rows))


def _policy(**kwargs):
    values = dict(risk_budget_quote="10", max_notional_quote="1000",
                  atr_period=2, stop_atr_multiple="2", reward_risk="2",
                  fee_bps="10", slippage_bps="10")
    values.update(kwargs)
    return EntryPlanPolicy(**values)


def _build(**kwargs):
    candles = kwargs.pop("candles", _candles())
    signal = kwargs.pop("signal", SimpleNamespace(action="buy", candle_ts=BASE_TS + 2 * BAR_MS))
    return build_entry_plan(
        candles=candles, signal=signal, entry_price="110.0",
        instrument=_instrument(), policy=_policy(),
        now_ms=BASE_TS + 3 * BAR_MS, bar_ms=BAR_MS, **kwargs
    )


def test_builds_closed_candle_atr_plan_with_fee_and_slippage_sizing():
    plan = _build()
    assert plan["signal_action"] == "buy"
    assert plan["candle_closed"] is True
    assert plan["candle_ts"] == BASE_TS + 2 * BAR_MS
    assert plan["atr"] == "4"
    assert plan["stop_loss"] == "102.0"
    assert plan["take_profit"] == "126.0"
    assert Decimal(plan["size"]) % Decimal("0.001") == 0
    assert Decimal(plan["risk_to_stop_quote"]) <= Decimal("10")
    assert Decimal(plan["notional_quote"]) <= Decimal("1000")
    assert plan["orders_submitted"] == 0


def test_rejects_non_buy_signal():
    with pytest.raises(EntryPlanError, match="buy signal"):
        _build(signal=SimpleNamespace(action="hold", candle_ts=BASE_TS + 2 * BAR_MS))


def test_rejects_signal_for_different_candle():
    with pytest.raises(EntryPlanError, match="timestamp"):
        _build(signal=SimpleNamespace(action="buy", candle_ts=BASE_TS + BAR_MS))


def test_rejects_stale_or_incomplete_candle():
    with pytest.raises(EntryPlanError, match="stale"):
        build_entry_plan(
            candles=_candles(), signal=SimpleNamespace(action="buy", candle_ts=BASE_TS + 2 * BAR_MS),
            entry_price="110.0", instrument=_instrument(), policy=_policy(),
            now_ms=BASE_TS + 10 * BAR_MS, bar_ms=BAR_MS,
        )


def test_rejects_zero_volume_atr_bar():
    with pytest.raises(EntryPlanError, match="positive traded"):
        _build(candles=_candles(zero_volume=True))


def test_rejects_unaligned_entry_price():
    with pytest.raises(EntryPlanError, match="tickSz"):
        build_entry_plan(
            candles=_candles(), signal=SimpleNamespace(action="buy", candle_ts=BASE_TS + 2 * BAR_MS),
            entry_price="110.05", instrument=_instrument(), policy=_policy(),
            now_ms=BASE_TS + 3 * BAR_MS, bar_ms=BAR_MS,
        )


def test_rejects_plan_whose_size_is_below_minimum():
    with pytest.raises(EntryPlanError, match="below minSz"):
        build_entry_plan(
            candles=_candles(), signal=SimpleNamespace(action="buy", candle_ts=BASE_TS + 2 * BAR_MS),
            entry_price="110.0", instrument=_instrument(),
            policy=_policy(risk_budget_quote="0.001", max_notional_quote="1"),
            now_ms=BASE_TS + 3 * BAR_MS, bar_ms=BAR_MS,
        )


@pytest.mark.parametrize("kwargs", [
    {"atr_period": True},
    {"atr_period": 1},
    {"stop_atr_multiple": "NaN"},
    {"reward_risk": "0"},
    {"risk_budget_quote": "0"},
    {"fee_bps": "10000"},
])
def test_policy_rejects_invalid_parameters(kwargs):
    with pytest.raises((ValueError, EntryPlanError)):
        _policy(**kwargs)
