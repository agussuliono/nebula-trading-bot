from datetime import datetime, timezone
from decimal import Decimal

import pytest

from nebula.account_ledger import AccountLedger, AccountLedgerError
from nebula.bill_reconciliation import reconcile_spot_bills, BillReconciliationError
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


def test_duplicate_bill_id_metadata_conflict_is_rejected(tmp_path):
    ledger = AccountLedger(StateStore(str(tmp_path / "ledger.db")))
    original = _bill(instType="SPOT")
    ledger.save_bills([original])
    # Identical balance arithmetic must not hide a changed instrument class.
    with pytest.raises(AccountLedgerError, match="Conflicting duplicate"):
        ledger.save_bills([_bill(instType="MARGIN")])
    with pytest.raises(AccountLedgerError, match="Conflicting duplicate"):
        ledger.save_bills([_bill(instId="BTC-USDC", instType="SPOT")])


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
    evidence = reconcile_spot_bills([], account_fingerprint="demo-account-hash",
                                      begin_ms=ts, end_ms=ts+60000)
    result = restarted.daily_equity_delta(timestamp_ms=ts+60000,
        reconciliation=evidence, account_fingerprint="demo-account-hash")
    assert Decimal(result["daily_pnl_quote"]) == Decimal("10")


def test_daily_equity_delta_requires_explicit_external_flow_reconciliation(tmp_path):
    ledger = AccountLedger(StateStore(str(tmp_path / "equity.db")))
    ts = 1795132800000
    ledger.establish_daily_baseline(equity_quote="1000", timestamp_ms=ts,
                                    account_fingerprint="demo-account-hash")
    ledger.record_equity_mark(equity_quote="990", timestamp_ms=ts+1000,
                              account_fingerprint="demo-account-hash")
    with pytest.raises(AccountLedgerError, match="evidence"):
        ledger.daily_equity_delta(timestamp_ms=ts+1000,
            reconciliation=None, account_fingerprint="demo-account-hash")


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


def test_daily_baseline_lookup_is_scoped_to_utc_day(tmp_path):
    ledger = AccountLedger(StateStore(str(tmp_path / "equity.db")))
    ts = 1795132800000
    ledger.establish_daily_baseline(equity_quote="1000", timestamp_ms=ts,
                                    account_fingerprint="demo-account-hash")
    assert ledger.get_daily_baseline(ts)["opening_equity_quote"] == "1000"
    assert ledger.get_daily_baseline(ts + 86_400_000) is None



def test_daily_equity_delta_rejects_evidence_for_wrong_account_or_interval(tmp_path):
    ledger = AccountLedger(StateStore(str(tmp_path / "bound-evidence.db")))
    ts = 1795132800000
    ledger.establish_daily_baseline(equity_quote="1000", timestamp_ms=ts,
                                    account_fingerprint="demo-account-hash")
    ledger.record_equity_mark(equity_quote="1010", timestamp_ms=ts+1000,
                              account_fingerprint="demo-account-hash")
    wrong_account = reconcile_spot_bills([], account_fingerprint="other-account",
                                          begin_ms=ts, end_ms=ts+1000)
    with pytest.raises(AccountLedgerError, match="does not cover"):
        ledger.daily_equity_delta(timestamp_ms=ts+1000,
            reconciliation=wrong_account, account_fingerprint="demo-account-hash")
    wrong_interval = reconcile_spot_bills([], account_fingerprint="demo-account-hash",
                                          begin_ms=ts+1, end_ms=ts+1000)
    with pytest.raises(AccountLedgerError, match="does not cover"):
        ledger.daily_equity_delta(timestamp_ms=ts+1000,
            reconciliation=wrong_interval, account_fingerprint="demo-account-hash")


def test_bill_reconciliation_rejects_unknown_or_unlinked_bill(tmp_path):
    ts = 1795132800000
    valid = {"billId":"1001","ccy":"USDT","balChg":"-0.1","bal":"999.9",
             "type":"2","subType":"1","instType":"SPOT","instId":"BTC-USDT",
             "ordId":"2001","tradeId":"3001","ts":str(ts)}
    evidence = reconcile_spot_bills([valid], account_fingerprint="demo-account-hash",
                                    begin_ms=ts, end_ms=ts+1000)
    assert evidence.complete is True
    assert evidence.bill_count == 1
    assert evidence.external_flows_quote == "0"
    with pytest.raises(BillReconciliationError, match="external cash flow"):
        from nebula.bill_reconciliation import VerifiedBillReconciliation
        VerifiedBillReconciliation(
            account_fingerprint="demo-account-hash", begin_ms=ts, end_ms=ts+1000,
            bill_count=1, last_bill_id="1001", external_flows_quote="50",
            complete=True,
        )
    with pytest.raises(BillReconciliationError, match="validator"):
        VerifiedBillReconciliation(
            account_fingerprint="demo-account-hash", begin_ms=ts, end_ms=ts+1000,
            bill_count=0, last_bill_id=None, external_flows_quote="0",
            complete=True,
        )
    with pytest.raises(BillReconciliationError, match="unknown"):
        reconcile_spot_bills([{**valid,"subType":"999"}],
            account_fingerprint="demo-account-hash", begin_ms=ts, end_ms=ts+1000)
    with pytest.raises(BillReconciliationError, match="linkage"):
        reconcile_spot_bills([{**valid,"tradeId":""}],
            account_fingerprint="demo-account-hash", begin_ms=ts, end_ms=ts+1000)
    with pytest.raises(BillReconciliationError, match="outside"):
        reconcile_spot_bills([valid], account_fingerprint="demo-account-hash",
                             begin_ms=ts+1, end_ms=ts+1000)


def test_bill_reconciliation_rejects_duplicate_ids_and_non_spot(tmp_path):
    ts = 1795132800000
    valid = {"billId":"1001","ccy":"USDT","balChg":"-0.1","bal":"999.9",
             "type":"2","subType":"1","instType":"SPOT","instId":"BTC-USDT",
             "ordId":"2001","tradeId":"3001","ts":str(ts)}
    with pytest.raises(BillReconciliationError, match="duplicate"):
        reconcile_spot_bills([valid,valid], account_fingerprint="demo-account-hash",
                             begin_ms=ts, end_ms=ts+1000)
    with pytest.raises(BillReconciliationError, match="non-Spot"):
        reconcile_spot_bills([{**valid,"instType":"MARGIN"}],
            account_fingerprint="demo-account-hash", begin_ms=ts, end_ms=ts+1000)
