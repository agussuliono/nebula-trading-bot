from decimal import Decimal

import pytest

from nebula.portfolio import (
    PortfolioError, PortfolioPolicy, PortfolioRiskEngine, calculate_inventory_basis,
)

NOW = 1_800_000_000_000


def _snapshot(*, bid="110", balance_qty="1", balance_ts=None, ticker_ts=None,
              acct_lv="1", auto_loan=False, stop="90", realized="0"):
    return {
        "account_config": [{"acctLv": acct_lv, "autoLoan": auto_loan,
                            "enableSpotBorrow": False, "perm": "read_only,trade"}],
        "balance": [{"uTime": str(balance_ts or NOW), "details": [
            {"ccy": "USDT", "eq": "1000", "availBal": "1000", "frozenBal": "0", "liab": "0"},
            {"ccy": "AAA", "eq": balance_qty, "availBal": balance_qty, "frozenBal": "0", "liab": "0"},
        ]}],
        "tickers": [{"instId": "AAA-USDT", "bidPx": bid, "askPx": str(Decimal(bid) + Decimal("0.1")), "bidSz": "10", "askSz": "10",
                     "ts": str(ticker_ts or NOW)}],
        "position_lock": {"owner_client_order_id": "entry1", "instrument": "AAA-USDT"},
        "position_fills": [{"side": "buy", "fill_size": "1", "fill_price": "100",
                             "fee": "-0.1", "fee_currency": "USDT", "fill_time": "1", "trade_id": "1",
                             "exchange_order_id": "123"}],
        "position_context": {"instrument": "AAA-USDT", "entry_status": "filled",
                             "entry_order_id": "123", "entry_exchange_state": "filled",
                             "entry_snapshot_filled_size": "1", "unresolved_exit_intents": 0,
                             "unresolved_child_orders": 0},
        "protection_intent": {"stop_loss": stop, "status": "active", "instrument": "AAA-USDT",
                              "entry_client_order_id": "entry1", "algo_client_order_id": "algo1",
                              "exchange_algo_id": "9001"},
        "open_regular_orders": [],
        "local_unresolved_intents": [],
        "open_algo_orders": [{"instId": "AAA-USDT", "ordType": "oco", "algoId": "9001", "algoClOrdId": "algo1"}],
        "daily_realized_pnl_quote": realized,
    }


def _engine(**policy_overrides):
    values = {"max_daily_loss_quote": Decimal("50"),
              "max_position_risk_quote": Decimal("30"),
              "max_position_notional_quote": Decimal("1000")}
    values.update(policy_overrides)
    return PortfolioRiskEngine(PortfolioPolicy(**values))


def test_inventory_basis_includes_quote_and_base_fees():
    basis = calculate_inventory_basis([
        {"side": "buy", "fill_size": "2", "fill_price": "100", "fee": "-0.2", "fee_currency": "USDT", "fill_time": "1", "trade_id": "1"},
        {"side": "buy", "fill_size": "1", "fill_price": "110", "fee": "-0.001", "fee_currency": "AAA", "fill_time": "2", "trade_id": "2"},
        {"side": "sell", "fill_size": "0.5", "fill_price": "120", "fee": "-0.05", "fee_currency": "USDT", "fill_time": "3", "trade_id": "3"},
    ], "AAA-USDT")
    assert basis["quantity"] == Decimal("2.499")
    assert basis["remaining_cost_quote"].quantize(Decimal("0.00000001")) == Decimal("258.48276092")


def test_portfolio_marks_at_bid_and_accounts_for_exit_costs():
    data = _snapshot()
    report = _engine().evaluate(**data, now_ms=NOW)
    assert report["mode"] == "read_only_demo_portfolio_analysis"
    assert report["orders_enabled"] is False
    assert report["order_submission_attempts"] == 0
    assert Decimal(report["position"]["bid_mark"]) == Decimal("110")
    assert Decimal(report["position"]["unrealized_pnl_gross_quote"]) == Decimal("9.9")
    assert Decimal(report["position"]["estimated_exit_fee_and_slippage_quote"]) == Decimal("0.22")
    assert report["entry_allowed"] is False  # one-position invariant
    assert "position_already_active" in report["blockers"]
    assert "position_risk_exceeds_limit" not in report["blockers"]


def test_stale_balance_blocks_entry():
    data = _snapshot(balance_ts=NOW - 60_000)
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "stale_balance_snapshot" in report["blockers"]
    assert report["entry_allowed"] is False


def test_stale_ticker_blocks_entry():
    data = _snapshot(ticker_ts=NOW - 60_000)
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "position_ticker_stale_or_invalid" in report["blockers"]
    assert report["entry_allowed"] is False


def test_future_skew_within_tolerance_is_fresh():
    data = _snapshot(balance_ts=NOW + 5_000, ticker_ts=NOW + 5_000)
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "stale_balance_snapshot" not in report["blockers"]
    assert "position_ticker_stale_or_invalid" not in report["blockers"]
    assert "invalid_or_stale_ticker:AAA" not in report["blockers"]


def test_future_skew_beyond_tolerance_is_stale():
    data = _snapshot(balance_ts=NOW + 5_001, ticker_ts=NOW + 5_001)
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "stale_balance_snapshot" in report["blockers"]
    assert report["entry_allowed"] is False


def test_policy_rejects_widened_snapshot_age_window():
    with pytest.raises(ValueError):
        PortfolioPolicy(max_snapshot_age_ms=30_001)


def test_balance_ledger_mismatch_blocks_entry():
    data = _snapshot(balance_qty="0.9")
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "exchange_balance_ledger_mismatch" in report["blockers"]
    assert report["entry_allowed"] is False


def test_unrealized_loss_counts_toward_daily_limit():
    data = _snapshot(bid="40", stop="90", realized="-10")
    report = _engine().evaluate(**data, now_ms=NOW, daily_unrealized_pnl_quote="-60")
    assert Decimal(report["daily_combined_pnl_quote"]) < Decimal("-50")
    assert "daily_loss_including_unrealized_limit_reached" in report["blockers"]


def test_open_position_without_utc_daily_mark_fails_closed_instead_of_using_lifetime_unrealized():
    data = _snapshot(bid="90", stop="80", realized="0")
    report = _engine().evaluate(**data, now_ms=NOW)
    assert report["daily_unrealized_pnl_quote"] is None
    assert report["daily_unrealized_pnl_baseline_available"] is False
    assert report["daily_combined_pnl_quote"] == "0"
    assert "daily_unrealized_pnl_baseline_missing" in report["blockers"]
    assert report["valuation_complete"] is True
    assert report["entry_allowed"] is False


def test_daily_pnl_uses_daily_mark_delta_not_lifetime_inventory_unrealized():
    data = _snapshot(bid="90", stop="80", realized="-2")
    report = _engine().evaluate(**data, now_ms=NOW, daily_unrealized_pnl_quote="10")
    assert report["daily_unrealized_pnl_quote"] == "10"
    assert report["daily_unrealized_pnl_baseline_available"] is True
    assert Decimal(report["daily_combined_pnl_quote"]) == Decimal("8")
    assert Decimal(report["position"]["unrealized_pnl_net_quote"]) < Decimal("0")


def test_non_spot_or_auto_borrow_blocks_entry():
    data = _snapshot(acct_lv="3", auto_loan=True)
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "account_not_spot_mode" in report["blockers"]
    assert "auto_borrow_enabled" in report["blockers"]


def test_missing_protection_blocks_entry():
    data = _snapshot()
    data["protection_intent"] = None
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "protective_stop_missing" in report["blockers"]


def test_unpriced_asset_blocks_entry():
    data = _snapshot()
    data["balance"][0]["details"].append({"ccy": "XYZ", "eq": "2", "availBal": "2", "frozenBal": "0", "liab": "0"})
    report = _engine().evaluate(**data, now_ms=NOW)
    assert any(x.startswith("unpriced_assets") for x in report["blockers"])
    assert report["valuation_complete"] is False


def test_third_currency_fee_is_rejected_fail_closed():
    with pytest.raises(PortfolioError, match="Unsupported fee currency"):
        calculate_inventory_basis([
            {"side": "buy", "fill_size": "1", "fill_price": "100", "fee": "-0.01", "fee_currency": "XYZ", "fill_time": "1", "trade_id": "1"},
        ], "AAA-USDT")


def test_unconfigured_limits_block_entry_even_if_valuation_ok():
    data = _snapshot()
    policy = PortfolioPolicy()
    report = PortfolioRiskEngine(policy).evaluate(**data, now_ms=NOW)
    assert "risk_limits_not_configured" in report["blockers"]
    assert report["entry_allowed"] is False


def test_flat_or_missing_position_inventory_fails_closed():
    with pytest.raises(PortfolioError):
        calculate_inventory_basis([], "AAA-USDT")


def test_planned_entry_risk_includes_entry_exit_fee_and_slippage():
    data = _snapshot(bid="100.9")
    data["position_lock"] = None
    data["position_fills"] = []
    data["position_context"] = None
    data["protection_intent"] = None
    data["open_algo_orders"] = []
    data["balance"][0]["details"] = [
        {"ccy": "USDT", "eq": "1000", "availBal": "1000", "frozenBal": "0", "liab": "0"}
    ]
    data["planned_entry"] = {
        "instrument": "AAA-USDT", "size": "1", "entry_price": "101",
        "stop_loss": "95", "take_profit": "115",
    }
    report = _engine().evaluate(**data, now_ms=NOW)
    plan = report["planned_entry"]
    expected = Decimal("6") + Decimal("0.202") + Decimal("0.19")
    assert Decimal(plan["risk_to_stop_quote"]) == expected
    assert report["entry_allowed"] is True
    assert report["portfolio_risk_to_stop_quote"] == str(expected)


def test_planned_entry_risk_over_limit_blocks_entry():
    data = _snapshot(bid="100.9")
    data["position_lock"] = None
    data["position_fills"] = []
    data["position_context"] = None
    data["protection_intent"] = None
    data["open_algo_orders"] = []
    data["balance"][0]["details"] = [
        {"ccy": "USDT", "eq": "1000", "availBal": "1000", "frozenBal": "0", "liab": "0"}
    ]
    data["planned_entry"] = {
        "instrument": "AAA-USDT", "size": "10", "entry_price": "101",
        "stop_loss": "95", "take_profit": "115",
    }
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "portfolio_risk_exceeds_limit" in report["blockers"]
    assert report["entry_allowed"] is False


def test_planned_entry_must_have_stop_below_entry_and_target_above():
    data = _snapshot(bid="100.9")
    data["position_lock"] = None
    data["position_fills"] = []
    data["position_context"] = None
    data["protection_intent"] = None
    data["open_algo_orders"] = []
    data["balance"][0]["details"] = [
        {"ccy": "USDT", "eq": "1000", "availBal": "1000", "frozenBal": "0", "liab": "0"}
    ]
    data["planned_entry"] = {
        "instrument": "AAA-USDT", "size": "1", "entry_price": "101",
        "stop_loss": "105", "take_profit": "115",
    }
    report = _engine().evaluate(**data, now_ms=NOW)
    assert any(x.startswith("planned_entry_invalid") for x in report["blockers"])
    assert report["entry_allowed"] is False


def test_withdraw_permission_blocks_entry():
    data = _snapshot()
    data["account_config"][0]["perm"] = "read_only,trade,withdraw"
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "api_key_withdraw_permission_forbidden" in report["blockers"]
    assert report["entry_allowed"] is False


def test_spot_borrow_feature_enabled_without_borrowings_does_not_block_entry():
    data = _snapshot()
    data["account_config"][0]["enableSpotBorrow"] = True
    data["account_config"][0]["spotBorrowAutoRepay"] = False
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "spot_borrow_enabled" not in report["blockers"]
    assert "spot_borrowings_present" not in report["blockers"]


def test_spot_borrowings_present_blocks_entry():
    data = _snapshot()
    data["account_config"][0]["enableSpotBorrow"] = True
    data["account_config"][0]["spotBorrowAutoRepay"] = False
    data["balance"][0]["details"][0]["liab"] = "5"
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "spot_borrowings_present" in report["blockers"]
    assert report["entry_allowed"] is False


def test_spot_borrow_auto_repay_enabled_blocks_entry():
    data = _snapshot()
    data["account_config"][0]["enableSpotBorrow"] = True
    data["account_config"][0]["spotBorrowAutoRepay"] = True
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "spot_borrow_auto_repay_enabled" in report["blockers"]
    assert report["entry_allowed"] is False


@pytest.mark.parametrize("liab", ["missing", None, False, "abc", "-1", "NaN", "Infinity"])
def test_missing_or_malformed_liability_fails_closed(liab):
    data = _snapshot()
    if liab == "missing":
        data["balance"][0]["details"][0].pop("liab")
    else:
        data["balance"][0]["details"][0]["liab"] = liab
    with pytest.raises(PortfolioError):
        _engine().evaluate(**data, now_ms=NOW)


@pytest.mark.parametrize("permission", ["transfer", "loan", "earn", "onchain", "future_scope"])
def test_execution_key_rejects_permissions_beyond_read_and_trade(permission):
    data = _snapshot()
    data["account_config"][0]["perm"] = "read_only,trade," + permission
    report = _engine().evaluate(**data, now_ms=NOW)
    assert report["entry_allowed"] is False
    if permission == "future_scope":
        assert "api_key_unrecognized_or_unapproved_permission" in report["blockers"]
    else:
        assert "api_key_" + permission + "_permission_forbidden" in report["blockers"]


def test_unknown_spot_borrow_permission_blocks_entry():
    data = _snapshot()
    data["account_config"][0].pop("enableSpotBorrow")
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "spot_borrow_permission_status_unknown" in report["blockers"]
    assert report["entry_allowed"] is False


def test_active_position_over_notional_limit_blocks_entry():
    engine = _engine(max_position_notional_quote=Decimal("50"))
    report = engine.evaluate(**_snapshot(), now_ms=NOW)
    assert "position_notional_exceeds_limit" in report["blockers"]
    assert report["entry_allowed"] is False


def test_bid_at_or_below_stop_blocks_entry():
    data = _snapshot(bid="89", stop="90")
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "position_stop_trigger_may_be_breached" in report["blockers"]
    assert report["entry_allowed"] is False


def test_missing_read_permission_blocks_entry():
    data = _snapshot()
    data["account_config"][0]["perm"] = "trade"
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "api_key_read_permission_missing" in report["blockers"]
    assert report["entry_allowed"] is False


def test_planned_entry_requires_available_quote_balance():
    data = _snapshot(bid="100.9")
    data["position_lock"] = None
    data["position_fills"] = []
    data["position_context"] = None
    data["protection_intent"] = None
    data["open_algo_orders"] = []
    data["balance"][0]["details"] = [
        {"ccy": "USDT", "eq": "50", "availBal": "20", "frozenBal": "30", "liab": "0"}
    ]
    data["planned_entry"] = {
        "instrument": "AAA-USDT", "size": "1", "entry_price": "101",
        "stop_loss": "95", "take_profit": "115",
    }
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "insufficient_available_quote_balance" in report["blockers"]
    assert report["entry_allowed"] is False


def test_inconsistent_available_and_frozen_balance_is_rejected():
    data = _snapshot()
    data["balance"][0]["details"][0]["availBal"] = "1001"
    with pytest.raises(PortfolioError, match="Inconsistent USDT"):
        _engine().evaluate(**data, now_ms=NOW)


def test_inactive_protection_order_blocks_position_gate():
    data = _snapshot()
    data["protection_intent"]["status"] = "triggered"
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "protective_order_not_active" in report["blockers"]
    assert report["entry_allowed"] is False


def test_entry_fill_snapshot_mismatch_blocks_position_gate():
    data = _snapshot()
    data["position_context"]["entry_snapshot_filled_size"] = "0.9"
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "entry_fill_ledger_snapshot_mismatch" in report["blockers"]
    assert report["entry_allowed"] is False


def test_unresolved_exit_intent_blocks_position_gate():
    data = _snapshot()
    data["position_context"]["unresolved_exit_intents"] = 1
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "unresolved_exit_intents" in report["blockers"]
    assert report["entry_allowed"] is False


def test_position_larger_than_top_bid_depth_blocks_valuation():
    data = _snapshot()
    data["tickers"][0]["bidSz"] = "0.5"
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "insufficient_position_bid_depth" in report["blockers"]
    assert report["entry_allowed"] is False


def test_holding_larger_than_top_bid_depth_blocks_valuation():
    data = _snapshot()
    data["tickers"][0]["bidSz"] = "0.5"
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "insufficient_top_bid_depth:AAA" in report["blockers"]
    assert report["valuation_complete"] is False


def test_active_position_requires_exchange_oco_pending_confirmation():
    data = _snapshot()
    data["open_algo_orders"] = []
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "exchange_oco_pending_state_mismatch" in report["blockers"]
    assert report["entry_allowed"] is False


def test_unrelated_open_algo_order_blocks_entry():
    data = _snapshot()
    data["position_lock"] = None
    data["position_context"] = None
    data["position_fills"] = []
    data["protection_intent"] = None
    data["open_algo_orders"] = [{"instId": "BBB-USDT", "ordType": "oco", "algoId": "777"}]
    data["balance"][0]["details"] = [{"ccy": "USDT", "eq": "1000", "availBal": "1000", "frozenBal": "0", "liab": "0"}]
    data["planned_entry"] = {"instrument":"AAA-USDT", "size":"1", "entry_price":"101", "stop_loss":"95", "take_profit":"115"}
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "exchange_open_algo_orders_present" in report["blockers"]
    assert report["entry_allowed"] is False


def test_open_regular_order_blocks_entry():
    data = _snapshot()
    data["position_lock"] = None
    data["position_context"] = None
    data["position_fills"] = []
    data["protection_intent"] = None
    data["open_algo_orders"] = []
    data["open_regular_orders"] = [{"instId":"AAA-USDT", "ordId":"12"}]
    data["balance"][0]["details"] = [{"ccy": "USDT", "eq": "1000", "availBal": "1000", "frozenBal": "0", "liab": "0"}]
    data["planned_entry"] = {"instrument":"AAA-USDT", "size":"1", "entry_price":"101", "stop_loss":"95", "take_profit":"115"}
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "exchange_open_regular_orders_present" in report["blockers"]
    assert report["entry_allowed"] is False


def test_local_unresolved_order_intent_blocks_entry():
    data = _snapshot()
    data["position_lock"] = None
    data["position_context"] = None
    data["position_fills"] = []
    data["protection_intent"] = None
    data["open_algo_orders"] = []
    data["balance"][0]["details"] = [{"ccy": "USDT", "eq": "1000", "availBal": "1000", "frozenBal": "0", "liab": "0"}]
    data["planned_entry"] = {"instrument":"AAA-USDT", "size":"1", "entry_price":"101", "stop_loss":"95", "take_profit":"115"}
    data["local_unresolved_intents"] = [{"client_order_id":"pending1", "status":"submission_unknown"}]
    report = _engine().evaluate(**data, now_ms=NOW)
    assert "local_unresolved_order_intents_present" in report["blockers"]
    assert report["entry_allowed"] is False


def test_per_position_risk_cannot_exceed_daily_loss_limit():
    with pytest.raises(ValueError, match="cannot exceed"):
        PortfolioPolicy(max_daily_loss_quote=Decimal("5"),
                        max_position_risk_quote=Decimal("10"),
                        max_position_notional_quote=Decimal("100"))



def test_valuation_can_be_complete_while_entry_is_blocked_by_policy():
    data = _snapshot()
    report = _engine().evaluate(**data, now_ms=NOW)
    assert report["valuation_complete"] is True
    assert report["entry_allowed"] is False
    assert "position_already_active" in report["blockers"]
    assert "planned_entry_not_provided" in report["blockers"]



def test_entry_fill_sync_failure_marks_valuation_incomplete_and_blocks_entry():
    data = _snapshot()
    report = _engine().evaluate(
        **data, now_ms=NOW, position_fill_sync_error="Timeout"
    )
    assert "position_fill_sync_failed:Timeout" in report["blockers"]
    assert report["valuation_complete"] is False
    assert report["entry_allowed"] is False


def test_api_key_must_have_read_and_trade_but_never_withdraw_permission():
    data = _snapshot()
    data["position_lock"] = None
    data["position_fills"] = []
    data["position_context"] = None
    data["protection_intent"] = None
    data["open_algo_orders"] = []
    data["tickers"] = [{"instId": "AAA-USDT", "bidPx": "100.95", "askPx": "101", "bidSz": "10", "askSz": "10", "ts": str(NOW)}]
    data["account_config"][0]["perm"] = "read_only"
    report = _engine().evaluate(**data, now_ms=NOW, planned_entry={
        "instrument": "AAA-USDT", "size": "1", "entry_price": "101",
        "stop_loss": "90", "take_profit": "120"})
    assert "api_key_trade_permission_missing" in report["blockers"]
    assert report["entry_allowed"] is False



def test_daily_account_equity_pnl_is_authoritative_and_not_double_counted():
    data = _snapshot(bid="90", stop="80", realized="-2")
    report = _engine().evaluate(
        **data, now_ms=NOW, daily_unrealized_pnl_quote="10",
        daily_account_pnl_quote="-60", require_daily_account_pnl=True,
    )
    assert report["daily_account_pnl_quote"] == "-60"
    assert report["daily_combined_pnl_quote"] == "-60"
    assert "daily_loss_including_unrealized_limit_reached" in report["blockers"]


def test_required_daily_account_pnl_missing_blocks_new_entry():
    data = _snapshot()
    data["position_lock"] = None
    data["position_fills"] = []
    data["position_context"] = None
    data["protection_intent"] = None
    data["open_algo_orders"] = []
    report = _engine().evaluate(**data, now_ms=NOW, require_daily_account_pnl=True)
    assert report["daily_account_pnl_quote"] is None
    assert "daily_account_pnl_baseline_missing" in report["blockers"]
    assert report["entry_allowed"] is False
