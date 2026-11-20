import pytest
from nebula.risk import size_position


def test_position_respects_risk_and_notional_caps():
    p = size_position(equity=1000, entry=100, stop=95, risk_budget=10,
                      max_notional=300, fee_rate=.001, slippage_rate=.001)
    assert p.notional <= 300
    assert p.risk_amount <= 10


def test_invalid_stop_rejected():
    with pytest.raises(ValueError):
        size_position(equity=1000, entry=100, stop=101, risk_budget=10,
                      max_notional=300, fee_rate=.001, slippage_rate=.001)


def test_risk_amount_includes_round_trip_costs():
    p = size_position(equity=1000, entry=100, stop=90, risk_budget=20,
                      max_notional=1000, fee_rate=.01, slippage_rate=.005)
    assert p.risk_amount > p.quantity * 10


def test_nan_inputs_rejected():
    with pytest.raises(ValueError):
        size_position(equity=float("nan"), entry=100, stop=95, risk_budget=10,
                      max_notional=300, fee_rate=.001, slippage_rate=.001)


def test_position_notional_never_exceeds_quote_equity():
    p = size_position(equity=100, entry=100, stop=99, risk_budget=50,
                      max_notional=1000, fee_rate=0, slippage_rate=0)
    assert p.notional <= 100



def test_position_sizing_uses_exact_net_stop_cash_loss():
    entry, stop, fee, slip = 100.10, 98.098, .001, .001
    plan = size_position(equity=1000, entry=entry, stop=stop,
                         risk_budget=10, max_notional=1000,
                         fee_rate=fee, slippage_rate=slip)
    per_unit = entry * (1 + fee) - stop * (1 - slip) * (1 - fee)
    assert plan.risk_amount == pytest.approx(plan.quantity * per_unit)
    assert plan.risk_amount <= 10
    assert plan.notional * (1 + fee) <= 1000


def test_position_sizing_rejects_impossible_cost_rates():
    with pytest.raises(ValueError):
        size_position(equity=1000, entry=100, stop=95, risk_budget=10,
                     max_notional=300, fee_rate=1.0, slippage_rate=.001)
