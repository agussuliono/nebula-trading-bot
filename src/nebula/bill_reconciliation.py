"""Evidence-bound reconciliation of OKX Spot account bills."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class BillReconciliationError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedBillReconciliation:
    """Validated bill coverage for one account and one exact time interval."""
    account_fingerprint: str
    begin_ms: int
    end_ms: int
    bill_count: int
    last_bill_id: str | None
    external_flows_quote: str
    complete: bool
    source: str = "okx_account_bills"

    def __post_init__(self):
        if not self.account_fingerprint or len(self.account_fingerprint) > 128:
            raise BillReconciliationError("Invalid account fingerprint")
        if (isinstance(self.begin_ms, bool) or isinstance(self.end_ms, bool)
                or self.begin_ms <= 0 or self.end_ms < self.begin_ms):
            raise BillReconciliationError("Invalid bill coverage interval")
        if isinstance(self.bill_count, bool) or self.bill_count < 0:
            raise BillReconciliationError("Invalid bill count")
        if self.source != "okx_account_bills" or self.complete is not True:
            raise BillReconciliationError("Bill coverage is not verified")
        try:
            flows = Decimal(str(self.external_flows_quote))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise BillReconciliationError("Invalid external cash flow") from exc
        if not flows.is_finite() or flows != 0:
            raise BillReconciliationError("Unsupported external cash flow for Spot-trade-only reconciliation")


def reconcile_spot_bills(bills, *, account_fingerprint, begin_ms, end_ms):
    """Accept only fully classified USDT-quoted Spot trade bills.

    The caller must pass the complete result of the bounded OKX bills pager.
    Any unknown bill, currency, subtype, missing linkage, duplicate ID, or
    out-of-window record fails closed. Such trades are internal account activity;
    external cash flow is therefore zero, while equity marks capture their value.
    """
    if not isinstance(bills, list):
        raise BillReconciliationError("Bills must be a complete list")
    if not account_fingerprint or len(str(account_fingerprint)) > 128:
        raise BillReconciliationError("Invalid account fingerprint")
    if (isinstance(begin_ms, bool) or isinstance(end_ms, bool)
            or int(begin_ms) <= 0 or int(end_ms) < int(begin_ms)):
        raise BillReconciliationError("Invalid bill coverage interval")
    seen = set()
    for bill in bills:
        if not isinstance(bill, dict):
            raise BillReconciliationError("Malformed account bill")
        if (str(bill.get("type")) != "2"
                or str(bill.get("instType") or "").upper() != "SPOT"):
            raise BillReconciliationError("Transfer or non-Spot bill classification")
        required = ("billId", "ccy", "balChg", "bal", "type", "subType",
                    "instType", "instId", "ordId", "tradeId", "ts")
        if any(bill.get(key) in (None, "") for key in required):
            raise BillReconciliationError("Bill is missing classification or linkage fields")
        bill_id = str(bill["billId"])
        if not bill_id.isdigit() or bill_id in seen:
            raise BillReconciliationError("Missing or duplicate billId")
        seen.add(bill_id)
        try:
            ts = int(str(bill["ts"]))
            change = Decimal(str(bill["balChg"]))
            balance = Decimal(str(bill["bal"]))
        except (ValueError, InvalidOperation, TypeError) as exc:
            raise BillReconciliationError("Malformed bill timestamp or balance") from exc
        if ts < int(begin_ms) or ts > int(end_ms):
            raise BillReconciliationError("Bill falls outside requested coverage")
        if not change.is_finite() or not balance.is_finite():
            raise BillReconciliationError("Non-finite bill amount")
        if not str(bill["ordId"]).isdigit() or not str(bill["tradeId"]).isdigit():
            raise BillReconciliationError("Spot trade bill has invalid order/trade linkage")
        instrument = str(bill["instId"]).split("-", 1)
        if (str(bill["type"]) != "2" or str(bill["instType"]).upper() != "SPOT"
                or len(instrument) != 2 or not instrument[0] or instrument[1] != "USDT"
                or str(bill["subType"]) not in {"1", "2"}):
            raise BillReconciliationError("Transfer, non-Spot, non-USDT, or unknown bill classification")
        if not str(bill["ccy"]).strip():
            raise BillReconciliationError("Bill currency is missing")
    return VerifiedBillReconciliation(
        account_fingerprint=str(account_fingerprint), begin_ms=int(begin_ms),
        end_ms=int(end_ms), bill_count=len(bills),
        last_bill_id=str(bills[-1]["billId"]) if bills else None,
        external_flows_quote="0", complete=True,
    )
