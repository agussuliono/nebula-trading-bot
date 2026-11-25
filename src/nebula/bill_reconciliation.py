"""Evidence-bound reconciliation of complete OKX Spot account-bills snapshots."""
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType


class BillReconciliationError(ValueError):
    pass


_SNAPSHOT_SEAL = object()
_VERIFICATION_SEAL = object()


@dataclass(frozen=True)
class CompleteAccountBillsSnapshot(Sequence):
    """Immutable result emitted only after the OKX bills pager sees its terminal page."""
    rows: tuple
    begin_ms: int
    end_ms: int
    page_count: int
    terminal_page_seen: bool
    source: str = "okx_account_bills"
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._seal is not _SNAPSHOT_SEAL:
            raise BillReconciliationError("Account bills snapshot was not produced by the OKX pager")
        if (self.source != "okx_account_bills" or self.terminal_page_seen is not True
                or isinstance(self.begin_ms, bool) or isinstance(self.end_ms, bool)
                or self.begin_ms <= 0 or self.end_ms < self.begin_ms
                or isinstance(self.page_count, bool) or self.page_count < 1):
            raise BillReconciliationError("Account bills snapshot is incomplete or malformed")
        if not isinstance(self.rows, tuple) or any(not isinstance(row, MappingProxyType) for row in self.rows):
            raise BillReconciliationError("Account bills snapshot rows must be immutable mappings")

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        return self.rows[index]

    def __iter__(self):
        return iter(self.rows)


def _create_complete_account_bills_snapshot(rows, *, begin_ms, end_ms, page_count,
                                            terminal_page_seen):
    """Private bridge used by the bounded exchange pager after terminal-page validation."""
    frozen_rows = tuple(MappingProxyType(dict(row)) for row in rows)
    return CompleteAccountBillsSnapshot(
        rows=frozen_rows, begin_ms=int(begin_ms), end_ms=int(end_ms),
        page_count=page_count, terminal_page_seen=terminal_page_seen,
        _seal=_SNAPSHOT_SEAL,
    )


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
    _seal: object = field(default=None, repr=False, compare=False)

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
        if self._seal is not _VERIFICATION_SEAL:
            raise BillReconciliationError("Bill reconciliation evidence was not produced by the validator")


def reconcile_spot_bills(snapshot, *, account_fingerprint, begin_ms, end_ms):
    """Accept only a complete immutable snapshot from the bounded OKX bills pager."""
    if not isinstance(snapshot, CompleteAccountBillsSnapshot):
        raise BillReconciliationError("A complete OKX account-bills snapshot is required; raw lists are untrusted")
    if (snapshot.terminal_page_seen is not True
            or snapshot.begin_ms != int(begin_ms) or snapshot.end_ms != int(end_ms)):
        raise BillReconciliationError("OKX bills snapshot does not cover the exact requested interval")
    if not account_fingerprint or len(str(account_fingerprint)) > 128:
        raise BillReconciliationError("Invalid account fingerprint")
    if (isinstance(begin_ms, bool) or isinstance(end_ms, bool)
            or int(begin_ms) <= 0 or int(end_ms) < int(begin_ms)):
        raise BillReconciliationError("Invalid bill coverage interval")
    seen = set()
    for bill in snapshot:
        if not isinstance(bill, MappingProxyType):
            raise BillReconciliationError("Malformed account bill")
        if (str(bill.get("type")) != "2"
                or str(bill.get("instType") or "").upper() != "SPOT"):
            raise BillReconciliationError("Transfer or non-Spot bill classification")
        required = ("billId", "ccy", "balChg", "bal", "type", "subType",
                    "instType", "instId", "ordId", "tradeId", "ts")
        if any(bill.get(key) in (None, "") for key in required):
            raise BillReconciliationError("Bill is missing classification or linkage fields")
        bill_id = str(bill["billId"])
        if not bill_id.isascii() or not bill_id.isdigit() or bill_id in seen:
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
        if (len(instrument) != 2 or not instrument[0] or instrument[1] != "USDT"
                or str(bill["subType"]) not in {"1", "2"}):
            raise BillReconciliationError("Transfer, non-Spot, non-USDT, or unknown bill classification")
        if not str(bill["ccy"]).strip():
            raise BillReconciliationError("Bill currency is missing")
    return VerifiedBillReconciliation(
        account_fingerprint=str(account_fingerprint), begin_ms=int(begin_ms),
        end_ms=int(end_ms), bill_count=len(snapshot),
        last_bill_id=str(snapshot[-1]["billId"]) if snapshot else None,
        external_flows_quote="0", complete=True, _seal=_VERIFICATION_SEAL,
    )
