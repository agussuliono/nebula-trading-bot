"""Durable account-bill and UTC equity-baseline ledger.

This module stores evidence; it deliberately does not guess bill classifications
or calculate P&L when external cash flows have not been reconciled.
"""
import json
from collections.abc import Sequence
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
            db.execute("""CREATE TABLE IF NOT EXISTS daily_equity_mark_chain (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                trading_day TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                equity_quote TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                mark_hash TEXT NOT NULL
            )""")
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

    def get_daily_baseline(self, timestamp_ms):
        day = self.utc_day(timestamp_ms)
        with self.state._connect() as db:
            row = db.execute(
                "SELECT * FROM daily_equity_baselines WHERE trading_day=?", (day,)
            ).fetchone()
        return dict(row) if row else None

    def save_bills(self, bills):
        if not isinstance(bills, Sequence) or isinstance(bills, (str, bytes)):
            raise AccountLedgerError("bills must be a sequence of bill records")
        inserted = 0
        for bill in bills:
            if not hasattr(bill, "items"):
                raise AccountLedgerError("bill row must be an object")
            bill = dict(bill)
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
            economic = (str(bill["ccy"]), str(change), str(balance), str(bill["type"]),
                        str(bill["subType"]), ts)
            with self.state._connect() as db:
                old = db.execute("SELECT raw_json FROM account_bills WHERE bill_id=?",
                                 (bill_id,)).fetchone()
                if old:
                    # The bill's classification and linkage fields (instType,
                    # instId, ordId, tradeId, subtype, etc.) are part of the
                    # reconciliation evidence, not just its balance arithmetic.
                    # Compare the canonical full payload so metadata drift or
                    # conflicting duplicate IDs cannot be silently accepted.
                    if old["raw_json"] != raw:
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
            self._append_mark_chain(db, day, ts, equity)
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
            import hashlib as _hashlib
            chained = db.execute(
                "SELECT COUNT(*) FROM daily_equity_mark_chain WHERE trading_day=? AND timestamp_ms=?",
                (day, ts)).fetchone()[0]
            if not chained:
                self._append_mark_chain(db, day, ts, equity)
            return {"trading_day": day, "equity_quote": str(equity), "timestamp_ms": ts}

    def daily_equity_delta(self, *, timestamp_ms, reconciliation,
                           account_fingerprint=None):
        """Calculate daily equity P&L only from interval-bound bill evidence."""
        from .bill_reconciliation import VerifiedBillReconciliation

        ts = int(timestamp_ms)
        day = self.utc_day(ts)
        with self.state._connect() as db:
            baseline = db.execute("SELECT * FROM daily_equity_baselines WHERE trading_day=?", (day,)).fetchone()
            latest = db.execute("SELECT * FROM daily_equity_marks WHERE trading_day=? ORDER BY timestamp_ms DESC LIMIT 1", (day,)).fetchone()
        if not baseline or not latest:
            raise AccountLedgerError("Daily equity baseline or current mark is missing")
        if account_fingerprint != baseline["account_fingerprint"]:
            raise AccountLedgerError("Account fingerprint missing or changed")
        if not isinstance(reconciliation, VerifiedBillReconciliation):
            raise AccountLedgerError("Verified bill reconciliation evidence is required")
        if (reconciliation.complete is not True
                or reconciliation.account_fingerprint != baseline["account_fingerprint"]
                or reconciliation.begin_ms != int(baseline["opening_timestamp_ms"])
                or reconciliation.end_ms != ts
                or reconciliation.source != "okx_account_bills"):
            raise AccountLedgerError("Bill reconciliation evidence does not cover this account and interval")
        if int(latest["timestamp_ms"]) != ts:
            raise AccountLedgerError("A fresh equity mark for the requested timestamp is required")
        flows = _decimal(reconciliation.external_flows_quote, "external_flows_quote")
        pnl = Decimal(latest["equity_quote"]) - Decimal(baseline["opening_equity_quote"]) - flows
        return {"trading_day": day, "daily_pnl_quote": str(pnl),
                "opening_equity_quote": baseline["opening_equity_quote"],
                "current_equity_quote": latest["equity_quote"],
                "external_flows_quote": str(flows),
                "reconciled_bill_count": reconciliation.bill_count, "complete": True}

    @staticmethod
    def _append_mark_chain(db, day, ts, equity):
        import hashlib as _hashlib
        prev = db.execute(
            "SELECT mark_hash FROM daily_equity_mark_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev[0] if prev else "GENESIS"
        mark_hash = _hashlib.sha256(
            f"{prev_hash}|{day}|{ts}|{equity}".encode()).hexdigest()
        db.execute(
            "INSERT INTO daily_equity_mark_chain(trading_day,timestamp_ms,equity_quote,prev_hash,mark_hash) VALUES(?,?,?,?,?)",
            (day, ts, str(equity), prev_hash, mark_hash))

    def verify_mark_chain(self) -> bool:
        """Recompute the append-only mark hash chain and cross-check marks."""
        import hashlib as _hashlib
        with self.state._connect() as db:
            chain = db.execute(
                "SELECT trading_day,timestamp_ms,equity_quote,prev_hash,mark_hash "
                "FROM daily_equity_mark_chain ORDER BY seq").fetchall()
            marks = db.execute(
                "SELECT trading_day,timestamp_ms,equity_quote FROM daily_equity_marks").fetchall()
        if len(chain) != len(marks):
            return False
        mark_set = {(m["trading_day"], int(m["timestamp_ms"]), str(Decimal(m["equity_quote"])))
                    for m in marks}
        prev_hash = "GENESIS"
        for entry in chain:
            expected = _hashlib.sha256(
                f"{prev_hash}|{entry['trading_day']}|{int(entry['timestamp_ms'])}|{Decimal(entry['equity_quote'])}".encode()
            ).hexdigest()
            if entry["prev_hash"] != prev_hash or entry["mark_hash"] != expected:
                return False
            if (entry["trading_day"], int(entry["timestamp_ms"]),
                    str(Decimal(entry["equity_quote"]))) not in mark_set:
                return False
            prev_hash = entry["mark_hash"]
        return True

    def has_complete_history(self, account_fingerprint) -> bool:
        """True when prior-day baselines exist and every one has its opening mark."""
        if not self.verify_mark_chain():
            return False
        with self.state._connect() as db:
            baselines = db.execute(
                "SELECT trading_day,opening_timestamp_ms,account_fingerprint "
                "FROM daily_equity_baselines").fetchall()
            if not baselines:
                return False
            for baseline in baselines:
                if baseline["account_fingerprint"] != str(account_fingerprint):
                    return False
                mark = db.execute(
                    "SELECT COUNT(*) FROM daily_equity_marks WHERE trading_day=? AND timestamp_ms=?",
                    (baseline["trading_day"], baseline["opening_timestamp_ms"])).fetchone()[0]
                if not mark:
                    return False
        return True

    def cumulative_drawdown(self, *, timestamp_ms, account_fingerprint,
                            max_drawdown_pct):
        """Peak-to-current drawdown across every reconciled equity mark.

        Fails closed when evidence is missing, stale, or bound to another
        account. Equity marks only exist on the reconciled daily path, so a
        missing mark can never read as zero drawdown.
        """
        try:
            limit = Decimal(str(max_drawdown_pct))
        except Exception as exc:
            raise AccountLedgerError("max_drawdown_pct must be a finite fraction in (0, 1]") from exc
        if not limit.is_finite() or limit <= 0 or limit > 1:
            raise AccountLedgerError("max_drawdown_pct must be a finite fraction in (0, 1]")
        if not self.verify_mark_chain():
            raise AccountLedgerError("Equity mark history failed integrity verification")
        ts = int(timestamp_ms)
        with self.state._connect() as db:
            fingerprints = db.execute(
                "SELECT DISTINCT account_fingerprint FROM daily_equity_baselines").fetchall()
            marks = db.execute(
                "SELECT timestamp_ms,equity_quote FROM daily_equity_marks WHERE timestamp_ms<=? "
                "ORDER BY timestamp_ms", (ts,)).fetchall()
        if not fingerprints or any(row[0] != str(account_fingerprint) for row in fingerprints):
            raise AccountLedgerError("Equity history is missing or belongs to a different account")
        if not marks or int(marks[-1]["timestamp_ms"]) != ts:
            raise AccountLedgerError("A fresh equity mark for the requested timestamp is required")
        equities = [Decimal(row["equity_quote"]) for row in marks]
        if any(not value.is_finite() or value <= 0 for value in equities):
            raise AccountLedgerError("Equity history contains an invalid mark")
        peak = max(equities)
        current = equities[-1]
        drawdown = (peak - current) / peak
        return {"peak_equity_quote": str(peak), "current_equity_quote": str(current),
                "drawdown_pct": str(drawdown), "max_drawdown_pct": str(limit),
                "tripped": drawdown >= limit, "mark_count": len(equities)}
