"""Durable account-bill and UTC equity-baseline ledger.

This module stores evidence; it deliberately does not guess bill classifications
or calculate P&L when external cash flows have not been reconciled.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


class AccountLedgerError(ValueError):
    pass


def _decimal(value, field, *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AccountLedgerError(f"Invalid {field}") from exc
    if not result.is_finite() or (result <= 0 if positive else False):
        raise AccountLedgerError(f"Invalid {field}")
    return result


class AccountLedger:
    """SQLite-backed, idempotent account bills and immutable daily baselines."""
    def __init__(self, state_store):
        self.state = state_store
        with self.state._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS account_bills (
                bill_id TEXT PRIMARY KEY,
                currency TEXT NOT NULL,
                balance_change TEXT NOT NULL,
                balance_after TEXT NOT NULL,
                bill_type TEXT NOT NULL,
                bill_subtype TEXT NOT NULL,
                instrument TEXT NOT NULL,
                order_id TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_account_bills_ts ON account_bills(timestamp_ms)")
            db.execute("""CREATE TABLE IF NOT EXISTS daily_equity_baselines (
                trading_day TEXT PRIMARY KEY,
                opening_equity_quote TEXT NOT NULL,
                opening_timestamp_ms INTEGER NOT NULL,
                valuation_currency TEXT NOT NULL,
                account_fingerprint TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS daily_equity_marks (
                trading_day TEXT NOT NULL REFERENCES daily_equity_baselines(trading_day),
                timestamp_ms INTEGER NOT NULL,
                equity_quote TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(trading_day,timestamp_ms)
            )""")

    @staticmethod
    def utc_day(timestamp_ms):
        if isinstance(timestamp_ms, bool) or int(timestamp_ms) <= 0:
            raise AccountLedgerError("timestamp_ms must be a positive millisecond timestamp")
        return datetime.fromtimestamp(int(timestamp_ms) / 1000, timezone.utc).date().isoformat()

    def save_bills(self, bills):
        if not isinstance(bills, list):
            raise AccountLedgerError("bills must be a list")
        inserted = 0
        for bill in bills:
            if not isinstance(bill, dict):
                raise AccountLedgerError("bill row must be an object")
            required = ("billId", "ccy", "balChg", "bal", "type", "subType", "ts")
            if any(bill.get(key) in (None, "") for key in required):
                raise AccountLedgerError("bill is missing required OKX fields")
            bill_id = str(bill["billId"])
            if not bill_id.isdigit():
                raise AccountLedgerError("billId must be numeric")
            change = _decimal(bill["balChg"], "balChg")
            balance = _decimal(bill["bal"], "bal")
            ts = int(_decimal(bill["ts"], "ts", positive=True))
            raw = json.dumps(bill, sort_keys=True, separators=(",", ":"))
            with self.state._connect() as db:
                old = db.execute("SELECT currency,balance_change,balance_after,bill_type,bill_subtype,timestamp_ms FROM account_bills WHERE bill_id=?",
                                 (bill_id,)).fetchone()
                economic = (str(bill["ccy"]), str(change), str(balance), str(bill["type"]),
                            str(bill["subType"]), ts)
                if old:
                    if tuple(old) != economic:
                        raise AccountLedgerError("Conflicting duplicate account billId")
                    continue
                db.execute("""INSERT INTO account_bills(
                    bill_id,currency,balance_change,balance_after,bill_type,bill_subtype,
                    instrument,order_id,trade_id,timestamp_ms,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (bill_id, *economic[:5], str(bill.get("instId") or ""),
                     str(bill.get("ordId") or ""), str(bill.get("tradeId") or ""), ts, raw))
                inserted += 1
        return {"received": len(bills), "inserted": inserted}

    def list_bills(self, *, begin_ms=None, end_ms=None):
        clauses, params = [], []
        if begin_ms is not None:
            clauses.append("timestamp_ms>=?")
            params.append(int(begin_ms))
        if end_ms is not None:
            clauses.append("timestamp_ms<=?")
            params.append(int(end_ms))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.state._connect() as db:
            rows = db.execute("SELECT * FROM account_bills" + where + " ORDER BY timestamp_ms,bill_id", params).fetchall()
            return [dict(row) for row in rows]

    def establish_daily_baseline(self, *, equity_quote, timestamp_ms,
                                 account_fingerprint, source="okx_demo_balance"):
        equity = _decimal(equity_quote, "equity_quote", positive=True)
        ts = int(timestamp_ms)
        day = self.utc_day(ts)
        if not account_fingerprint or len(str(account_fingerprint)) > 128:
            raise AccountLedgerError("A stable account fingerprint is required")
        if source != "okx_demo_balance":
            raise AccountLedgerError("Unsupported equity baseline source")
        with self.state._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM daily_equity_baselines WHERE trading_day=?", (day,)).fetchone()
            if existing:
                if (Decimal(existing["opening_equity_quote"]) != equity
                        or existing["account_fingerprint"] != str(account_fingerprint)):
                    raise AccountLedgerError("Daily baseline already exists and is immutable")
                return dict(existing)
            previous = db.execute("SELECT trading_day,opening_timestamp_ms FROM daily_equity_baselines ORDER BY trading_day DESC LIMIT 1").fetchone()
            if previous and ts <= int(previous["opening_timestamp_ms"]):
                raise AccountLedgerError("Baseline timestamp must advance monotonically")
            db.execute("""INSERT INTO daily_equity_baselines(
                trading_day,opening_equity_quote,opening_timestamp_ms,valuation_currency,
                account_fingerprint,source) VALUES(?,?,?,?,?,?)""",
                (day, str(equity), ts, "USDT", str(account_fingerprint), source))
            db.execute("INSERT INTO daily_equity_marks(trading_day,timestamp_ms,equity_quote,source) VALUES(?,?,?,?)",
                       (day, ts, str(equity), source))
            return dict(db.execute("SELECT * FROM daily_equity_baselines WHERE trading_day=?", (day,)).fetchone())

    def record_equity_mark(self, *, equity_quote, timestamp_ms, account_fingerprint,
                           source="okx_demo_balance"):
        equity = _decimal(equity_quote, "equity_quote", positive=True)
        ts = int(timestamp_ms)
        day = self.utc_day(ts)
        if source != "okx_demo_balance":
            raise AccountLedgerError("Unsupported equity mark source")
        with self.state._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            baseline = db.execute("SELECT account_fingerprint,opening_timestamp_ms FROM daily_equity_baselines WHERE trading_day=?",
                                  (day,)).fetchone()
            if not baseline:
                raise AccountLedgerError("Daily equity baseline is missing")
            if baseline["account_fingerprint"] != str(account_fingerprint):
                raise AccountLedgerError("Account fingerprint changed; reconciliation required")
            if ts < int(baseline["opening_timestamp_ms"]):
                raise AccountLedgerError("Equity mark predates daily baseline")
            latest = db.execute("SELECT MAX(timestamp_ms) FROM daily_equity_marks WHERE trading_day=?", (day,)).fetchone()[0]
            if latest is not None and ts < int(latest):
                raise AccountLedgerError("Equity mark timestamp regressed")
            db.execute("INSERT OR IGNORE INTO daily_equity_marks(trading_day,timestamp_ms,equity_quote,source) VALUES(?,?,?,?)",
                       (day, ts, str(equity), source))
            row = db.execute("SELECT equity_quote FROM daily_equity_marks WHERE trading_day=? AND timestamp_ms=?",
                             (day, ts)).fetchone()
            if not row or Decimal(row["equity_quote"]) != equity:
                raise AccountLedgerError("Conflicting equity mark at the same timestamp")
            return {"trading_day": day, "equity_quote": str(equity), "timestamp_ms": ts}

    def daily_equity_delta(self, *, timestamp_ms, external_flows_quote=None,
                           account_fingerprint=None):
        ts = int(timestamp_ms)
        day = self.utc_day(ts)
        with self.state._connect() as db:
            baseline = db.execute("SELECT * FROM daily_equity_baselines WHERE trading_day=?", (day,)).fetchone()
            latest = db.execute("SELECT * FROM daily_equity_marks WHERE trading_day=? ORDER BY timestamp_ms DESC LIMIT 1", (day,)).fetchone()
        if not baseline or not latest:
            raise AccountLedgerError("Daily equity baseline or current mark is missing")
        if account_fingerprint != baseline["account_fingerprint"]:
            raise AccountLedgerError("Account fingerprint missing or changed")
        if external_flows_quote is None:
            raise AccountLedgerError("External cash flows have not been fully reconciled")
        flows = _decimal(external_flows_quote, "external_flows_quote")
        pnl = Decimal(latest["equity_quote"]) - Decimal(baseline["opening_equity_quote"]) - flows
        return {"trading_day": day, "daily_pnl_quote": str(pnl),
                "opening_equity_quote": baseline["opening_equity_quote"],
                "current_equity_quote": latest["equity_quote"],
                "external_flows_quote": str(flows), "complete": True}
