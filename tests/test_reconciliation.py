from decimal import Decimal
import pytest
from nebula.reconciliation import DemoAccountReconciler, ReconciliationError


class FakeAccount:
    def get_balance(self):
        return [{"details":[
            {"ccy":"BTC", "availBal":"0.004", "frozenBal":"0", "liab": "0"},
            {"ccy":"USDT", "availBal":"100", "frozenBal":"5", "liab": "0"},
        ]}]
    def get_pending_orders(self, inst_type="SPOT"):
        return [{"instId":"BTC-USDT", "ordId":"1"}]
    def get_pending_algo_orders(self, ord_type="oco"):
        return []


def test_snapshot_aggregates_base_and_quote_and_detects_regular_orders():
    snap=DemoAccountReconciler(FakeAccount()).snapshot("BTC-USDT")
    assert snap.available_base == Decimal("0.004")
    assert snap.frozen_quote == Decimal("5")
    assert snap.has_open_orders
    assert not snap.is_flat("0.00001")


def test_snapshot_requires_all_reads_and_valid_balance_shape():
    class Bad(FakeAccount):
        def get_pending_algo_orders(self, ord_type="oco"):
            raise RuntimeError("api unavailable")
    with pytest.raises(RuntimeError):
        DemoAccountReconciler(Bad()).snapshot("BTC-USDT")

    class Malformed(FakeAccount):
        def get_balance(self): return [{"details":None}]
    with pytest.raises(ReconciliationError):
        DemoAccountReconciler(Malformed()).snapshot("BTC-USDT")


def test_flat_requires_zero_base_and_no_regular_or_algo_orders():
    class Flat(FakeAccount):
        def get_balance(self): return [{"details":[{"ccy":"USDT","availBal":"10","frozenBal":"0", "liab": "0"}]}]
        def get_pending_orders(self, inst_type="SPOT"): return []
        def get_pending_algo_orders(self, ord_type="oco"): return []
    snap=DemoAccountReconciler(Flat()).snapshot("BTC-USDT")
    assert snap.is_flat("0.00001")


def test_snapshot_counts_pending_conditional_algo_orders_as_open():
    class Conditional(FakeAccount):
        def get_balance(self):
            return [{"details":[
                {"ccy":"BTC", "availBal":"0", "frozenBal":"0", "liab": "0"},
                {"ccy":"USDT", "availBal":"100", "frozenBal":"0", "liab": "0"},
            ]}]
        def get_pending_orders(self, inst_type="SPOT"):
            return []
        def get_pending_algo_orders(self, ord_type="oco"):
            if ord_type == "conditional":
                return [{"instId":"BTC-USDT", "algoId":"123"}]
            return []

    snap = DemoAccountReconciler(Conditional()).snapshot("BTC-USDT")

    assert snap.has_open_orders
    assert not snap.is_flat("0.00001")
    assert len(snap.open_algo_orders) == 1


def _account_with_details(details):
    class Account(FakeAccount):
        def get_balance(self):
            return [{"details": details}]
        def get_pending_orders(self, inst_type="SPOT"):
            return []
    return Account()


@pytest.mark.parametrize("field", ["availBal", "frozenBal"])
@pytest.mark.parametrize("value", ["missing", None, False, "", "abc", "-1", "NaN", "Infinity"])
def test_invalid_balance_amounts_fail_closed(field, value):
    row = {"ccy": "BTC", "availBal": "0", "frozenBal": "0", "liab": "0"}
    if value == "missing":
        del row[field]
    else:
        row[field] = value
    with pytest.raises(ReconciliationError):
        DemoAccountReconciler(_account_with_details([row])).snapshot("BTC-USDT")


@pytest.mark.parametrize("details", [
    [{"ccy": "BTC", "availBal": "0", "frozenBal": "0", "liab": "0"},
     {"ccy": "BTC", "availBal": "1", "frozenBal": "0", "liab": "0"}],
    ["not-a-row"],
    [{"availBal": "0", "frozenBal": "0", "liab": "0"}],
    [{"ccy": "", "availBal": "0", "frozenBal": "0", "liab": "0"}],
    [{"ccy": "BTC", "availBal": "0", "frozenBal": "0"}],
])
def test_malformed_balance_rows_fail_closed(details):
    with pytest.raises(ReconciliationError):
        DemoAccountReconciler(_account_with_details(details)).snapshot("BTC-USDT")
