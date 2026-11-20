from datetime import datetime, timezone
from decimal import Decimal

import pytest

from nebula.account_ledger import AccountLedger, AccountLedgerError
from nebula.state import StateStore


def _bill(**overrides):
    row = {"billId": "1001", "ccy": "USDT", "balChg": "-0.1", "bal": "999.9",
           "type": "2", "subType": "1", "ts": "1795132800000",
           "instId": "BTC-USDT", "ordId": "2001", "tradeId": "3001"}
    row.update(overrides)
    return row


def test_account_bills_are_idempotent_and_conflicts_fail_closed(tmp_path):
    ledger = AccountLedger(StateStore(str(tmp_path / "ledger.db")))
    assert ledger.save_bills([_bill()]) == {"received": 1, "inserted": 1}
    assert ledger.save_bills([_bill()]) == {"received": 1, "inserted": 0}
    with pytest.raises(AccountLedgerError, match="Conflicting duplicate"):
        ledger.save_bills([_bill(balChg="-0.2")])
    assert len(ledger.list_bills()) == 1


def test_account_bill_validation_rejects_missing_and_nonfinite_values(tmp_path):
    ledger = AccountLedger(StateStore(str(tmp_path / "ledger.db")))
    with pytest.raises(AccountLedgerError, match="required"):
        ledger.save_bills([_bill(billId="")])
    with pytest.raises(AccountLedgerError, match="balChg"):
        ledger.save_bills([_bill(balChg="NaN")])


def test_daily_equity_baseline_is_immutable_and_survives_restart(tmp_path):
    path = str(tmp_path / "equity.db")
    ledger = AccountLedger(StateStore(path))
    ts = 1795132800000
    ledger.establish_daily_baseline(equity_quote="1000", timestamp_ms=ts,
                                    account_fingerprint="demo-account-hash")
    with pytest.raises(AccountLedgerError, match="immutable"):
        ledger.establish_daily_baseline(equity_quote="900", timestamp_ms=ts,
                                        account_fingerprint="demo-account-hash")
    restarted = AccountLedger(StateStore(path))
    assert restarted.record_equity_mark(equity_quote="1010", timestamp_ms=ts+60000,
                                        account_fingerprint="demo-account-hash")["equity_quote"] == "1010"
    result = restarted.daily_equity_delta(timestamp_ms=ts+60000,
        external_flows_quote="0", account_fingerprint="demo-account-hash")
    assert Decimal(result["daily_pnl_quote"]) == Decimal("10")


def test_daily_equity_delta_requires_explicit_external_flow_reconciliation(tmp_path):
    ledger = AccountLedger(StateStore(str(tmp_path / "equity.db")))
    ts = 1795132800000
    ledger.establish_daily_baseline(equity_quote="1000", timestamp_ms=ts,
                                    account_fingerprint="demo-account-hash")
    ledger.record_equity_mark(equity_quote="990", timestamp_ms=ts+1000,
                              account_fingerprint="demo-account-hash")
    with pytest.raises(AccountLedgerError, match="cash flows"):
        ledger.daily_equity_delta(timestamp_ms=ts+1000,
            account_fingerprint="demo-account-hash")


def test_equity_mark_rejects_account_change_and_time_regression(tmp_path):
    ledger = AccountLedger(StateStore(str(tmp_path / "equity.db")))
    ts = 1795132800000
    ledger.establish_daily_baseline(equity_quote="1000", timestamp_ms=ts,
                                    account_fingerprint="demo-account-hash")
    with pytest.raises(AccountLedgerError, match="fingerprint"):
        ledger.record_equity_mark(equity_quote="1000", timestamp_ms=ts+1000,
                                  account_fingerprint="different-account")
    ledger.record_equity_mark(equity_quote="1010", timestamp_ms=ts+2000,
                              account_fingerprint="demo-account-hash")
    with pytest.raises(AccountLedgerError, match="regressed"):
        ledger.record_equity_mark(equity_quote="1005", timestamp_ms=ts+1000,
                                  account_fingerprint="demo-account-hash")
