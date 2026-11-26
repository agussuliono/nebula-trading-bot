"""Durable local state for idempotent Demo order submission and audit."""
import hashlib
import json
import os
import uuid
import sqlite3
import stat
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: str = "nebula.sqlite3"):
        self.path = str(path)
        self._secure_storage_files(create_database=True)
        self._init_db()
        self._secure_storage_files()

    def _secure_regular_file(self, path: str, *, create: bool = False) -> None:
        """Force a database file/sidecar to owner-only mode without following links."""
        candidate = Path(path)
        flags = os.O_RDWR
        if create:
            flags |= os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        elif candidate.is_symlink():
            raise RuntimeError(f"Refusing symlinked NEBULA state file: {candidate.name}")

        try:
            fd = os.open(str(candidate), flags, 0o600)
        except FileNotFoundError:
            if not create:
                return
            raise
        except OSError as exc:
            raise RuntimeError(
                f"Cannot securely open NEBULA state file: {candidate.name}"
            ) from exc

        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(
                    f"NEBULA state path is not a regular file: {candidate.name}"
                )
            if not hasattr(os, "fchmod"):
                raise RuntimeError(
                    "This platform cannot enforce private NEBULA state-file permissions"
                )
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    def _secure_storage_files(self, *, create_database: bool = False) -> None:
        # In-memory SQLite has no filesystem state or sidecars.
        if self.path == ":memory:":
            return
        self._secure_regular_file(self.path, create=create_database)
        # Harden pre-existing sidecars before SQLite opens them, and newly-created
        # ones after WAL mode is enabled. WAL/SHM can contain sensitive account data.
        for suffix in ("-wal", "-shm", "-journal"):
            self._secure_regular_file(self.path + suffix)

    def _connect(self):
        if self.path != ":memory:":
            self._secure_storage_files()
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            if self.path != ":memory:":
                self._secure_storage_files()
            return conn
        except Exception:
            conn.close()
            raise

    def _init_db(self):
        with self._connect() as db:
            existing_tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            known_nebula_tables = {
                "intents", "order_snapshots", "kv", "exit_child_orders", "fills",
                "protection_intents", "position_lock", "audit_events",
                "trade_episodes", "outcome_events",
                "daily_pnl_events", "emergency_stop", "nebula_schema_meta",
                "account_bills", "daily_equity_baselines", "daily_equity_marks",
                "daily_equity_mark_chain",
            }
            # Any trace of the NEBULA schema marks this as a pre-existing
            # database; a damaged one must never be re-initialized as fresh.
            pre_existing = bool(existing_tables & known_nebula_tables)
            db.execute("""CREATE TABLE IF NOT EXISTS intents (
                client_order_id TEXT PRIMARY KEY,
                instrument TEXT NOT NULL,
                candle_ts INTEGER NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                exchange_order_id TEXT,
                position_client_order_id TEXT,
                last_update TEXT DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            # Safe additive migration for databases created by v0.2.0.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(intents)")}
            if "exchange_order_id" not in columns:
                db.execute("ALTER TABLE intents ADD COLUMN exchange_order_id TEXT")
            if "last_update" not in columns:
                db.execute("ALTER TABLE intents ADD COLUMN last_update TEXT")
            if "position_client_order_id" not in columns:
                db.execute("ALTER TABLE intents ADD COLUMN position_client_order_id TEXT")
            if "risk_plan_json" not in columns:
                db.execute("ALTER TABLE intents ADD COLUMN risk_plan_json TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS idx_intents_status ON intents(status)")
            db.execute("""CREATE TABLE IF NOT EXISTS order_snapshots (
                client_order_id TEXT PRIMARY KEY REFERENCES intents(client_order_id),
                exchange_order_id TEXT,
                exchange_state TEXT NOT NULL,
                requested_size TEXT NOT NULL,
                cumulative_fill_size TEXT NOT NULL,
                average_fill_price TEXT NOT NULL,
                fee TEXT NOT NULL,
                fee_currency TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            # Triggered OCOs spawn regular sell orders; retain every child ID across restarts.
            db.execute("""CREATE TABLE IF NOT EXISTS exit_child_orders (
                entry_client_order_id TEXT NOT NULL REFERENCES protection_intents(entry_client_order_id),
                exchange_order_id TEXT NOT NULL,
                instrument TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'unknown',
                requested_size TEXT NOT NULL DEFAULT '0',
                cumulative_fill_size TEXT NOT NULL DEFAULT '0',
                average_fill_price TEXT NOT NULL DEFAULT '0',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(entry_client_order_id, exchange_order_id)
            )""")
            # Trade-level execution ledger. Fees keep OKX's sign: negative=fee, positive=rebate.
            db.execute("""CREATE TABLE IF NOT EXISTS fills (
                instrument TEXT NOT NULL,
                exchange_order_id TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL,
                fill_size TEXT NOT NULL,
                fill_price TEXT NOT NULL,
                fee TEXT NOT NULL,
                fee_currency TEXT NOT NULL,
                fill_time TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(instrument, exchange_order_id, trade_id)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(instrument,exchange_order_id)")
            # One durable protective OCO intent per entry position.
            db.execute("""CREATE TABLE IF NOT EXISTS protection_intents (
                entry_client_order_id TEXT PRIMARY KEY REFERENCES intents(client_order_id),
                instrument TEXT NOT NULL,
                algo_client_order_id TEXT NOT NULL UNIQUE,
                filled_size TEXT NOT NULL,
                take_profit TEXT NOT NULL,
                stop_loss TEXT NOT NULL,
                status TEXT NOT NULL,
                exchange_algo_id TEXT,
                last_error_type TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            # A single durable slot enforces V1's one-active-position invariant.
            db.execute("""CREATE TABLE IF NOT EXISTS position_lock (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                owner_client_order_id TEXT NOT NULL,
                instrument TEXT NOT NULL,
                acquired_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            # Fase 0C: one opaque episode identity per reserved entry. The
            # mapping to raw order IDs stays in trading state, never exported.
            db.execute("""CREATE TABLE IF NOT EXISTS trade_episodes (
                trade_episode_id TEXT PRIMARY KEY,
                entry_client_order_id TEXT NOT NULL UNIQUE REFERENCES intents(client_order_id),
                instrument TEXT NOT NULL,
                signal_key TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                net_pnl_quote TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT,
                accounting_finalized_at TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS outcome_events (
                outcome_event_id TEXT PRIMARY KEY,
                trade_episode_id TEXT NOT NULL REFERENCES trade_episodes(trade_episode_id),
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                exported_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                client_order_id TEXT,
                details_json TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            if not pre_existing:
                # Safety state is created only for a fresh database. A
                # pre-existing database missing these tables is damaged
                # state and must fail closed, never silently re-initialize
                # a latched emergency stop to disengaged.
                db.execute("""CREATE TABLE IF NOT EXISTS daily_pnl_events (
                    position_id TEXT PRIMARY KEY,
                    trading_day TEXT NOT NULL,
                    realized_pnl_quote TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS idx_daily_pnl_day ON daily_pnl_events(trading_day)")
                db.execute("""CREATE TABLE IF NOT EXISTS emergency_stop (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    engaged INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )""")
                db.execute("INSERT OR IGNORE INTO emergency_stop(singleton,engaged,reason) VALUES(1,0,'')")
                db.execute("""CREATE TABLE IF NOT EXISTS nebula_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )""")
                db.execute("INSERT OR IGNORE INTO nebula_schema_meta(key,value) VALUES('initialized','1')")
            else:
                # Legacy migration: a pre-existing database without the marker
                # gets one only after its safety state proves complete; a
                # damaged legacy database stays unmarked and fails closed.
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "nebula_schema_meta" not in tables:
                    safety_complete = (
                        {"daily_pnl_events", "emergency_stop"}.issubset(tables)
                        and db.execute(
                            "SELECT COUNT(*) FROM emergency_stop WHERE singleton=1"
                        ).fetchone()[0] == 1
                    )
                    if safety_complete:
                        db.execute("""CREATE TABLE IF NOT EXISTS nebula_schema_meta (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        )""")
                        db.execute("INSERT OR IGNORE INTO nebula_schema_meta(key,value) VALUES('initialized','1')")

    @staticmethod
    def make_client_order_id(strategy: str, instrument: str, candle_ts: int, side: str) -> str:
        raw = f"{strategy}|{instrument}|{candle_ts}|{side}".encode()
        return "NB" + hashlib.sha256(raw).hexdigest()[:28]

    def reserve_intent(self, client_order_id: str, instrument: str, candle_ts: int, side: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO intents(client_order_id,instrument,candle_ts,side,status) VALUES(?,?,?,?, 'reserved')",
                (client_order_id, instrument, candle_ts, side),
            )
            return cursor.rowcount == 1

    def update_intent(self, client_order_id: str, status: str, exchange_order_id: str | None = None) -> None:
        """Apply only forward, evidence-compatible order-intent transitions."""
        allowed = {
            "reserved": {"submitting", "rejected"},
            "submitting": {"acknowledged", "submission_unknown", "rejected"},
            "submission_unknown": {
                "submission_unknown", "acknowledged", "live", "partially_filled",
                "filled", "canceled", "mmp_canceled", "rejected",
            },
            "acknowledged": {
                "submission_unknown", "live", "partially_filled",
                "filled", "canceled", "mmp_canceled",
            },
            "live": {
                "submission_unknown", "live", "partially_filled",
                "filled", "canceled", "mmp_canceled",
            },
            "partially_filled": {
                "submission_unknown", "partially_filled",
                "filled", "canceled", "mmp_canceled",
            },
            "filled": {"filled"},
            "canceled": {"canceled"},
            "rejected": {"rejected"},
            "mmp_canceled": {"mmp_canceled"},
        }
        if status not in allowed:
            raise ValueError("Unsupported order intent state")
        if exchange_order_id is not None:
            value = str(exchange_order_id)
            if not value.isascii() or not value.isdigit():
                raise ValueError("Exchange order ID must be numeric")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,exchange_order_id FROM intents WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if not row:
                raise ValueError("Cannot update an unknown order intent")
            current = str(row["status"])
            if status not in allowed.get(current, set()):
                raise ValueError(f"Invalid order intent transition: {current} -> {status}")
            previous_id = str(row["exchange_order_id"] or "")
            next_id = str(exchange_order_id) if exchange_order_id is not None else ""
            if previous_id and next_id and previous_id != next_id:
                raise ValueError("Exchange order ID cannot change for an existing intent")
            db.execute(
                "UPDATE intents SET status=?, exchange_order_id=COALESCE(?, exchange_order_id), last_update=CURRENT_TIMESTAMP WHERE client_order_id=?",
                (status, exchange_order_id, client_order_id),
            )

    def get_intent(self, client_order_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM intents WHERE client_order_id=?", (client_order_id,)).fetchone()
            return dict(row) if row else None

    def list_unresolved_intents(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM intents WHERE status IN ('reserved','submitting','submission_unknown','acknowledged','live','partially_filled') ORDER BY created_at"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_entry_risk_plan(self, client_order_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT risk_plan_json FROM intents WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if not row or not row["risk_plan_json"]:
                return None
            try:
                value = json.loads(row["risk_plan_json"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Persisted entry risk plan is corrupt") from exc
            if not isinstance(value, dict):
                raise ValueError("Persisted entry risk plan is not an object")
            return value

    def reserve_entry_intent(self, client_order_id: str, instrument: str, candle_ts: int,
                             side: str, risk_plan: dict[str, Any] | None = None,
                             signal_key: str | None = None) -> bool:
        """Atomically reserve the sole position slot and the entry intent."""
        if side != "buy":
            raise ValueError("entry intents must be Spot buy orders")
        encoded_plan = None
        if risk_plan is not None:
            from decimal import Decimal, InvalidOperation
            try:
                if not isinstance(risk_plan, dict) or risk_plan.get("instrument") != instrument:
                    raise ValueError("Risk plan instrument does not match entry intent")
                values = {
                    field: Decimal(str(risk_plan[field]))
                    for field in ("size", "entry_price", "stop_loss", "take_profit", "risk_to_stop_quote")
                }
                if not all(value.is_finite() and value > 0 for value in values.values()):
                    raise ValueError("Risk plan values must be finite and positive")
                if not values["stop_loss"] < values["entry_price"] < values["take_profit"]:
                    raise ValueError("Risk plan stop/entry/target ordering is invalid")
            except (KeyError, TypeError, InvalidOperation) as exc:
                raise ValueError("Risk plan is incomplete or invalid") from exc
            encoded_plan = json.dumps({
                "instrument": instrument,
                **{key: str(value) for key, value in values.items()},
            }, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM intents WHERE client_order_id=?", (client_order_id,)).fetchone()
            if existing:
                return False
            lock = db.execute("SELECT owner_client_order_id,instrument FROM position_lock WHERE singleton=1").fetchone()
            if lock:
                raise RuntimeError(f"Single-position slot is occupied by {lock['owner_client_order_id']} ({lock['instrument']})")
            db.execute("INSERT INTO position_lock(singleton,owner_client_order_id,instrument) VALUES(1,?,?)",
                       (client_order_id, instrument))
            db.execute("INSERT INTO intents(client_order_id,instrument,candle_ts,side,status,risk_plan_json) VALUES(?,?,?,?,'reserved',?)",
                       (client_order_id, instrument, candle_ts, side, encoded_plan))
            db.execute(
                "INSERT INTO trade_episodes(trade_episode_id,entry_client_order_id,instrument,signal_key) VALUES(?,?,?,?)",
                (str(uuid.uuid4()), client_order_id, instrument, signal_key),
            )
            return True

    def reserve_exit_intent(self, client_order_id: str, instrument: str, candle_ts: int,
                            side: str, position_client_order_id: str) -> bool:
        """Reserve an exit only for the currently locked position."""
        if side != "sell":
            raise ValueError("exit intents must be Spot sell orders")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM intents WHERE client_order_id=?", (client_order_id,)).fetchone()
            if existing:
                return False
            lock = db.execute("SELECT owner_client_order_id,instrument FROM position_lock WHERE singleton=1").fetchone()
            if not lock or lock["owner_client_order_id"] != position_client_order_id or lock["instrument"] != instrument:
                raise RuntimeError("Exit order does not match the active position lock")
            existing_exit = db.execute(
                """SELECT client_order_id,status FROM intents
                   WHERE position_client_order_id=? AND side='sell'
                     AND status NOT IN ('filled','canceled','rejected','mmp_canceled')
                   LIMIT 1""",
                (position_client_order_id,),
            ).fetchone()
            if existing_exit:
                raise RuntimeError(
                    f"Position already has an unresolved exit order {existing_exit['client_order_id']}"
                )
            protection = db.execute(
                "SELECT status FROM protection_intents WHERE entry_client_order_id=?",
                (position_client_order_id,),
            ).fetchone()
            if protection:
                raise RuntimeError(
                    "Independent sell order is blocked while a durable OCO protection intent exists"
                )
            db.execute("INSERT INTO intents(client_order_id,instrument,candle_ts,side,status,position_client_order_id) VALUES(?,?,?,?,'reserved',?)",
                       (client_order_id, instrument, candle_ts, side, position_client_order_id))
            return True

    def get_position_lock(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM position_lock WHERE singleton=1").fetchone()
            return dict(row) if row else None

    def get_trade_episode(self, entry_client_order_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM trade_episodes WHERE entry_client_order_id=?",
                (entry_client_order_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_trade_episodes(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM trade_episodes ORDER BY created_at,trade_episode_id").fetchall()
            return [dict(row) for row in rows]

    def update_trade_episode(self, trade_episode_id: str, *, status: str,
                             net_pnl_quote: str | None = None) -> None:
        with self._connect() as db:
            if status == "closed":
                db.execute(
                    "UPDATE trade_episodes SET status=?,closed_at=CURRENT_TIMESTAMP WHERE trade_episode_id=?",
                    (status, trade_episode_id),
                )
            elif status == "accounting_finalized":
                db.execute(
                    """UPDATE trade_episodes SET status=?,net_pnl_quote=?,
                       closed_at=COALESCE(closed_at,CURRENT_TIMESTAMP),
                       accounting_finalized_at=CURRENT_TIMESTAMP WHERE trade_episode_id=?""",
                    (status, net_pnl_quote, trade_episode_id),
                )
            else:
                db.execute(
                    "UPDATE trade_episodes SET status=? WHERE trade_episode_id=?",
                    (status, trade_episode_id),
                )

    OUTCOME_PAYLOAD_FIELDS = {
        "outcome_event_id", "trade_episode_id", "signal_key", "instrument",
        "event_type", "event_time_utc", "side", "fill_price", "fill_size",
        "fee", "fee_currency", "protection_status", "exit_reason",
        "observed_slippage_bps", "net_pnl_quote",
    }
    OUTCOME_EVENT_TYPES = {
        "entry_intent_created", "entry_filled", "entry_remainder_cancelled",
        "entry_not_filled", "protection_activated", "exit_partial_filled",
        "exit_filled", "reconciliation_incomplete", "position_closed",
        "accounting_finalized",
    }

    @classmethod
    def validate_outcome_payload(cls, payload: Any) -> dict[str, Any]:
        """Reject any outcome payload outside the Fase 0C export allowlist."""
        if not isinstance(payload, dict):
            raise ValueError("Outcome payload must be an object")
        unknown = set(payload) - cls.OUTCOME_PAYLOAD_FIELDS
        if unknown:
            raise ValueError(f"Outcome payload has non-allowlisted fields: {sorted(unknown)}")
        for field in ("outcome_event_id", "trade_episode_id", "instrument", "event_type"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise ValueError(f"Outcome payload field {field} must be a non-empty string")
        if payload["event_type"] not in cls.OUTCOME_EVENT_TYPES:
            raise ValueError("Outcome payload event_type is not recognized")
        if any(not isinstance(value, str) for value in payload.values()):
            raise ValueError("Outcome payload values must all be strings")
        if "net_pnl_quote" in payload and payload["event_type"] != "accounting_finalized":
            raise ValueError("net_pnl_quote is only allowed on accounting_finalized")
        return payload

    def list_position_intents(self, entry_client_order_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM intents WHERE client_order_id=? OR position_client_order_id=? ORDER BY created_at,client_order_id",
                (entry_client_order_id, entry_client_order_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_entry_intents_without_episode(self) -> list[dict[str, Any]]:
        """Buy intents with no trade episode (legacy or bypass-created)."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT i.* FROM intents i
                   LEFT JOIN trade_episodes e ON e.entry_client_order_id=i.client_order_id
                   WHERE i.side='buy' AND e.trade_episode_id IS NULL
                   ORDER BY i.created_at,i.client_order_id"""
            ).fetchall()
            return [dict(row) for row in rows]

    def record_outcome_event(self, outcome_event_id: str, trade_episode_id: str,
                             event_type: str, payload: dict[str, Any]) -> bool:
        """Idempotently persist one outcome event; True when newly recorded."""
        self.validate_outcome_payload(payload)
        if (payload["outcome_event_id"] != outcome_event_id
                or payload["trade_episode_id"] != trade_episode_id
                or payload["event_type"] != event_type):
            raise ValueError("Outcome payload identity does not match its record keys")
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO outcome_events(outcome_event_id,trade_episode_id,event_type,payload_json) VALUES(?,?,?,?)",
                (outcome_event_id, trade_episode_id, event_type,
                 json.dumps(payload, sort_keys=True, separators=(",", ":"))),
            )
            return cursor.rowcount == 1

    def list_outcome_events(self, trade_episode_id: str | None = None,
                            unexported_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM outcome_events"
        clauses, params = [], []
        if trade_episode_id is not None:
            clauses.append("trade_episode_id=?")
            params.append(trade_episode_id)
        if unexported_only:
            clauses.append("exported_at IS NULL")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at,outcome_event_id"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
            events = []
            for row in rows:
                event = dict(row)
                event["payload"] = json.loads(event.pop("payload_json"))
                events.append(event)
            return events

    def mark_outcome_exported(self, outcome_event_ids: list[str]) -> None:
        if not outcome_event_ids:
            return
        with self._connect() as db:
            db.executemany(
                "UPDATE outcome_events SET exported_at=CURRENT_TIMESTAMP WHERE outcome_event_id=?",
                [(event_id,) for event_id in outcome_event_ids],
            )

    def release_terminal_zero_fill_entry_lock(self, client_order_id: str) -> bool:
        """Release an entry slot only after a persisted terminal zero-fill result."""
        from decimal import Decimal, InvalidOperation
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            intent = db.execute("SELECT status,side FROM intents WHERE client_order_id=?", (client_order_id,)).fetchone()
            snapshot = db.execute("SELECT exchange_state,cumulative_fill_size FROM order_snapshots WHERE client_order_id=?", (client_order_id,)).fetchone()
            if not intent or intent["side"] != "buy" or not snapshot:
                return False
            if intent["status"] not in {"canceled", "rejected", "mmp_canceled"}:
                return False
            try:
                cumulative = Decimal(snapshot["cumulative_fill_size"])
            except (InvalidOperation, TypeError, ValueError):
                return False
            if not cumulative.is_finite() or cumulative != 0:
                return False
            if snapshot["exchange_state"] not in {"canceled", "mmp_canceled"}:
                return False
            cursor = db.execute("DELETE FROM position_lock WHERE singleton=1 AND owner_client_order_id=?", (client_order_id,))
            if cursor.rowcount:
                db.execute("INSERT INTO audit_events(event_type,client_order_id,details_json) VALUES(?,?,?)",
                           ("zero_fill_entry_lock_released", client_order_id,
                            json.dumps({"exchange_state": snapshot["exchange_state"],
                                        "cumulative_fill_size": "0"}, sort_keys=True)))
            return cursor.rowcount == 1

    def release_rejected_entry_lock(self, client_order_id: str) -> bool:
        """Release only when the exchange/local adapter definitively rejected the entry."""
        from decimal import Decimal, InvalidOperation
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            intent = db.execute("SELECT status,side,exchange_order_id FROM intents WHERE client_order_id=?", (client_order_id,)).fetchone()
            snapshot = db.execute("SELECT exchange_state,cumulative_fill_size FROM order_snapshots WHERE client_order_id=?", (client_order_id,)).fetchone()
            if not intent or intent["side"] != "buy" or intent["status"] != "rejected":
                return False
            if not snapshot and intent["exchange_order_id"]:
                # An exchange order ID without a snapshot does not prove
                # the order was never filled.
                return False
            if snapshot and snapshot["exchange_state"] not in {"canceled", "mmp_canceled"}:
                # A non-terminal snapshot (e.g. live) contradicts rejection
                # even when its cumulative fill is still zero.
                return False
            if snapshot:
                try:
                    cumulative = Decimal(str(snapshot["cumulative_fill_size"]))
                except (InvalidOperation, TypeError, ValueError):
                    return False
                # Exact Decimal comparison: never release a position lock because
                # float conversion rounded a tiny partial fill down to zero.
                if not cumulative.is_finite() or cumulative != 0:
                    return False
            cursor = db.execute("DELETE FROM position_lock WHERE singleton=1 AND owner_client_order_id=?", (client_order_id,))
            if cursor.rowcount:
                db.execute("INSERT INTO audit_events(event_type,client_order_id,details_json) VALUES(?,?,?)",
                           ("rejected_entry_lock_released", client_order_id,
                            json.dumps({"intent_status": intent["status"],
                                        "exchange_state": snapshot["exchange_state"] if snapshot else None,
                                        "cumulative_fill_size": "0",
                                        "exchange_order_id_present": bool(intent["exchange_order_id"])},
                                       sort_keys=True)))
            return cursor.rowcount == 1

    LOCK_RELEASE_EVENT_TYPES = {
        "position_lock_released",
        "zero_fill_entry_lock_released",
        "rejected_entry_lock_released",
    }

    def has_lock_release_evidence(self, entry_client_order_id: str) -> bool:
        """True only when a sanctioned release path recorded valid evidence."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT event_type,details_json FROM audit_events
                   WHERE client_order_id=?
                   AND event_type IN ('position_lock_released',
                                      'zero_fill_entry_lock_released',
                                      'rejected_entry_lock_released')""",
                (entry_client_order_id,),
            ).fetchall()
            for row in rows:
                try:
                    details = json.loads(row["details_json"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(details, dict):
                    continue
                if row["event_type"] == "position_lock_released":
                    try:
                        base = Decimal(str(details["base_balance"]))
                        dust = Decimal(str(details["dust_limit"]))
                    except (KeyError, InvalidOperation):
                        continue
                    if details.get("open_orders_checked") is True and 0 <= base <= dust:
                        return True
                elif row["event_type"] == "zero_fill_entry_lock_released":
                    if (details.get("exchange_state") in {"canceled", "mmp_canceled"}
                            and details.get("cumulative_fill_size") == "0"):
                        return True
                elif row["event_type"] == "rejected_entry_lock_released":
                    if (details.get("intent_status") == "rejected"
                            and details.get("cumulative_fill_size") == "0"
                            and details.get("exchange_state") in {None, "canceled", "mmp_canceled"}):
                        return True
            return False

    def release_position_lock_after_flat_check(self, *, position_client_order_id: str,
                                               base_balance: str, dust_limit: str,
                                               open_orders_checked: bool) -> bool:
        """Release only after caller has freshly checked balance and open orders.

        The caller must source these values from authenticated Demo account reads.
        This method is deliberately fail-closed and records the reconciliation evidence.
        """
        from decimal import Decimal, InvalidOperation
        if open_orders_checked is not True:
            raise ValueError("A fresh open-order check is required before releasing the position lock")
        try:
            balance, dust = Decimal(str(base_balance)), Decimal(str(dust_limit))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Balance and dust limit must be valid decimals") from exc
        if not balance.is_finite() or not dust.is_finite() or balance < 0 or dust < 0 or balance > dust:
            raise ValueError("Position is not confirmed flat")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lock = db.execute("SELECT instrument FROM position_lock WHERE singleton=1 AND owner_client_order_id=?",
                              (position_client_order_id,)).fetchone()
            if not lock:
                return False
            unresolved = db.execute("""SELECT COUNT(*) AS n FROM intents
                WHERE instrument=? AND side='sell'
                AND status IN ('reserved','submitting','submission_unknown','acknowledged','live','partially_filled')""",
                (lock["instrument"],)).fetchone()["n"]
            if unresolved:
                raise ValueError("Cannot release position lock while exit orders remain unresolved")
            unresolved_children = db.execute("""SELECT COUNT(*) AS n FROM exit_child_orders
                WHERE entry_client_order_id=? AND state IN ('unknown','live','partially_filled')""",
                (position_client_order_id,)).fetchone()["n"]
            if unresolved_children:
                raise ValueError("Cannot release position lock while OCO child orders remain unresolved")
            db.execute("DELETE FROM position_lock WHERE singleton=1 AND owner_client_order_id=?", (position_client_order_id,))
            db.execute("INSERT INTO audit_events(event_type,client_order_id,details_json) VALUES(?,?,?)",
                       ("position_lock_released", position_client_order_id,
                        json.dumps({"base_balance": str(balance), "dust_limit": str(dust),
                                    "open_orders_checked": True}, sort_keys=True)))
            return True

    def record_event(self, event_type: str, client_order_id: str | None = None, details: dict | None = None) -> None:
        if event_type in self.LOCK_RELEASE_EVENT_TYPES:
            raise ValueError(
                "Lock-release evidence may only be recorded by a sanctioned release path"
            )
        # Callers must never put credentials, signatures, or full request headers here.
        payload = json.dumps(details or {}, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            db.execute(
                "INSERT INTO audit_events(event_type,client_order_id,details_json) VALUES(?,?,?)",
                (event_type, client_order_id, payload),
            )

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as db:
            rows = db.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["details"] = json.loads(item.pop("details_json"))
                result.append(item)
            return result

    def save_order_snapshot(self, client_order_id: str, order: dict) -> dict[str, Any]:
        """Persist cumulative execution facts; reject identity, state and fill regressions."""
        if not isinstance(order, dict):
            raise ValueError("Exchange order snapshot must be an object")
        from decimal import Decimal, InvalidOperation
        def dec(value, name):
            try:
                number = Decimal(str(value or "0"))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid {name} in exchange order snapshot") from exc
            if not number.is_finite() or number < 0:
                raise ValueError(f"Invalid {name} in exchange order snapshot")
            return number
        intent = self.get_intent(client_order_id)
        if not intent:
            raise ValueError("Cannot snapshot an unknown order intent")
        cumulative = dec(order.get("accFillSz", "0"), "accFillSz")
        requested = dec(order.get("sz", "0"), "sz")
        average = dec(order.get("avgPx", "0"), "avgPx")
        try:
            fee_value = Decimal(str(order.get("fee", "0") or "0"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Invalid fee in exchange order snapshot") from exc
        if not fee_value.is_finite():
            raise ValueError("Invalid fee in exchange order snapshot")
        # Preserve OKX sign: negative is a fee, positive is a rebate.
        fee = fee_value
        if requested <= 0:
            raise ValueError("Exchange order snapshot has no valid requested size")
        if cumulative > requested:
            raise ValueError("Cumulative fill exceeds requested order size")
        if cumulative > 0 and average <= 0:
            raise ValueError("Filled order has no valid average price")
        state = str(order.get("state", "unknown"))
        order_id = str(order.get("ordId") or "")
        if order_id and (not order_id.isascii() or not order_id.isdigit()):
            raise ValueError("Exchange order snapshot has an invalid order ID")
        expected_order_id = str(intent.get("exchange_order_id") or "")
        if expected_order_id and order_id and expected_order_id != order_id:
            raise ValueError("Exchange order ID does not match the persisted intent")
        # When the exchange returns identity fields, bind them to the durable intent.
        # These fields are optional here only for legacy/offline fixtures; production
        # OKX order-detail responses include them and are checked when present.
        identity = {
            "instId": intent["instrument"],
            "side": intent["side"],
            "instType": "SPOT",
            "tdMode": "cash",
            "clOrdId": client_order_id,
        }
        for field, expected in identity.items():
            if field in order and str(order[field]) != expected:
                raise ValueError(f"Exchange order snapshot {field} does not match the persisted intent")
        with self._connect() as db:
            previous = db.execute("SELECT * FROM order_snapshots WHERE client_order_id=?", (client_order_id,)).fetchone()
            if previous:
                if cumulative < Decimal(previous["cumulative_fill_size"]):
                    raise ValueError("Exchange cumulative fill regressed; refusing to overwrite")
                old_order_id = str(previous["exchange_order_id"] or "")
                if old_order_id and order_id and old_order_id != order_id:
                    raise ValueError("Exchange order ID changed for an existing intent")
                if Decimal(previous["requested_size"]) != requested:
                    raise ValueError("Exchange requested order size changed for an existing intent")
                old_state = str(previous["exchange_state"])
                terminal = {"filled", "canceled", "mmp_canceled"}
                if old_state in terminal and state != old_state:
                    raise ValueError("Exchange order state regressed after a terminal state")
                allowed_transitions = {
                    "live": {"live", "partially_filled", "filled", "canceled", "mmp_canceled"},
                    "partially_filled": {"partially_filled", "filled", "canceled", "mmp_canceled"},
                }
                if old_state in allowed_transitions and state not in allowed_transitions[old_state]:
                    raise ValueError("Exchange order state made an invalid lifecycle transition")
            db.execute("""INSERT INTO order_snapshots(
                client_order_id,exchange_order_id,exchange_state,requested_size,cumulative_fill_size,
                average_fill_price,fee,fee_currency,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(client_order_id) DO UPDATE SET
                exchange_order_id=excluded.exchange_order_id, exchange_state=excluded.exchange_state,
                requested_size=excluded.requested_size,cumulative_fill_size=excluded.cumulative_fill_size,
                average_fill_price=excluded.average_fill_price,fee=excluded.fee,
                fee_currency=excluded.fee_currency,updated_at=CURRENT_TIMESTAMP""",
                (client_order_id, str(order.get("ordId") or ""), state, str(requested), str(cumulative),
                 str(average), str(fee), str(order.get("feeCcy") or "")))
            row = db.execute("SELECT * FROM order_snapshots WHERE client_order_id=?", (client_order_id,)).fetchone()
            return dict(row)

    def get_order_snapshot(self, client_order_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM order_snapshots WHERE client_order_id=?", (client_order_id,)).fetchone()
            return dict(row) if row else None

    def reserve_protection_intent(self, *, entry_client_order_id: str, instrument: str,
                                  algo_client_order_id: str, filled_size: str,
                                  take_profit: str, stop_loss: str) -> dict[str, Any]:
        """Persist one immutable OCO intent; same entry can never create a second one."""
        from decimal import Decimal, InvalidOperation
        try:
            qty, tp, sl = map(lambda x: Decimal(str(x)), (filled_size, take_profit, stop_loss))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Protection quantities and prices must be decimals") from exc
        if not all(x.is_finite() for x in (qty, tp, sl)) or qty <= 0 or sl <= 0 or tp <= sl:
            raise ValueError("Invalid immutable OCO quantity or trigger prices")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            entry = db.execute("SELECT instrument,side FROM intents WHERE client_order_id=?",
                               (entry_client_order_id,)).fetchone()
            if not entry or entry["side"] != "buy" or entry["instrument"] != instrument:
                raise ValueError("Protection must belong to the matching Spot buy entry")
            existing = db.execute("SELECT * FROM protection_intents WHERE entry_client_order_id=?",
                                  (entry_client_order_id,)).fetchone()
            if existing:
                same = (existing["filled_size"] == str(qty) and existing["take_profit"] == str(tp)
                        and existing["stop_loss"] == str(sl) and existing["algo_client_order_id"] == algo_client_order_id)
                if not same:
                    raise ValueError("Protection intent already exists with different parameters; reconcile before changing")
                return dict(existing)
            db.execute("""INSERT INTO protection_intents(
                entry_client_order_id,instrument,algo_client_order_id,filled_size,take_profit,stop_loss,status
            ) VALUES(?,?,?,?,?,?, 'reserved')""",
                (entry_client_order_id,instrument,algo_client_order_id,str(qty),str(tp),str(sl)))
            db.execute("INSERT INTO audit_events(event_type,client_order_id,details_json) VALUES(?,?,?)",
                       ("protection_intent_reserved", entry_client_order_id,
                        json.dumps({"algo_client_order_id":algo_client_order_id,"filled_size":str(qty)},sort_keys=True)))
            return dict(db.execute("SELECT * FROM protection_intents WHERE entry_client_order_id=?",
                                   (entry_client_order_id,)).fetchone())

    def get_protection_intent(self, entry_client_order_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row=db.execute("SELECT * FROM protection_intents WHERE entry_client_order_id=?",
                           (entry_client_order_id,)).fetchone()
            return dict(row) if row else None

    def update_protection_intent(self, entry_client_order_id: str, status: str,
                                 exchange_algo_id: str | None = None,
                                 error_type: str | None = None) -> dict[str, Any]:
        allowed={"submitting","submission_unknown","active","triggered","partially_failed","canceled","failed"}
        if status not in allowed:
            raise ValueError("Unsupported protection state")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM protection_intents WHERE entry_client_order_id=?",
                           (entry_client_order_id,)).fetchone()
            if not row:
                raise ValueError("Unknown protection intent")
            # OCO lifecycle is monotonic once active/terminal; stale reads cannot
            # move a triggered/canceled/failed intent back into an earlier state.
            if row["status"] in {"active", "triggered", "partially_failed", "canceled", "failed"} and status in {"submitting", "submission_unknown"}:
                raise ValueError("Cannot resubmit a terminal or active OCO intent")
            if row["status"] in {"triggered", "partially_failed", "canceled", "failed"} and status != row["status"]:
                raise ValueError("Cannot regress or overwrite a terminal OCO state")
            db.execute("""UPDATE protection_intents SET status=?,
                exchange_algo_id=COALESCE(?,exchange_algo_id),last_error_type=?,updated_at=CURRENT_TIMESTAMP
                WHERE entry_client_order_id=?""",
                (status,exchange_algo_id,error_type,entry_client_order_id))
            db.execute("INSERT INTO audit_events(event_type,client_order_id,details_json) VALUES(?,?,?)",
                       ("protection_state_updated",entry_client_order_id,
                        json.dumps({"status":status,"exchange_algo_id":exchange_algo_id,"error_type":error_type},sort_keys=True)))
            return dict(db.execute("SELECT * FROM protection_intents WHERE entry_client_order_id=?",
                                   (entry_client_order_id,)).fetchone())


    def defer_protection_intent(self, entry_client_order_id: str,
                                error_type: str | None = None) -> dict[str, Any]:
        """Return a definitely-not-submitted OCO intent to reserved after local rejection.

        This transition is allowed only from submitting. It must never be used
        after a network attempt whose outcome could be unknown.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM protection_intents WHERE entry_client_order_id=?",
                             (entry_client_order_id,)).fetchone()
            if not row:
                raise ValueError("Unknown protection intent")
            if row["status"] != "submitting":
                raise ValueError("Only a locally blocked OCO submission can be deferred")
            db.execute("""UPDATE protection_intents SET status='reserved',last_error_type=?,
                updated_at=CURRENT_TIMESTAMP WHERE entry_client_order_id=?""",
                (error_type, entry_client_order_id))
            db.execute("INSERT INTO audit_events(event_type,client_order_id,details_json) VALUES(?,?,?)",
                       ("protection_submission_deferred", entry_client_order_id,
                        json.dumps({"status": "reserved", "error_type": error_type}, sort_keys=True)))
            return dict(db.execute("SELECT * FROM protection_intents WHERE entry_client_order_id=?",
                                   (entry_client_order_id,)).fetchone())


    def save_exit_child_orders(self, entry_client_order_id: str, instrument: str,
                               exchange_order_ids: list[str]) -> list[dict[str, Any]]:
        """Persist the regular order IDs spawned by a triggered OCO, without dropping prior IDs."""
        if not isinstance(exchange_order_ids, list):
            raise ValueError("exchange_order_ids must be a list")
        clean = []
        for value in exchange_order_ids:
            order_id = str(value)
            if not order_id.isdigit():
                raise ValueError("OCO child order IDs must be numeric")
            if order_id not in clean:
                clean.append(order_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            protection = db.execute("SELECT instrument FROM protection_intents WHERE entry_client_order_id=?",
                                    (entry_client_order_id,)).fetchone()
            if not protection or protection["instrument"] != instrument:
                raise ValueError("OCO child does not match a persisted protection intent")
            for order_id in clean:
                db.execute("""INSERT OR IGNORE INTO exit_child_orders(
                    entry_client_order_id,exchange_order_id,instrument) VALUES(?,?,?)""",
                    (entry_client_order_id, order_id, instrument))
            rows = db.execute("SELECT * FROM exit_child_orders WHERE entry_client_order_id=? ORDER BY exchange_order_id",
                              (entry_client_order_id,)).fetchall()
            return [dict(row) for row in rows]

    def update_exit_child_order(self, entry_client_order_id: str, exchange_order_id: str,
                                order: dict[str, Any]) -> dict[str, Any]:
        from decimal import Decimal, InvalidOperation
        def decimal_field(value, field):
            try:
                number = Decimal(str(value or "0"))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid {field} in OCO child order") from exc
            if not number.is_finite() or number < 0:
                raise ValueError(f"Invalid {field} in OCO child order")
            return number
        requested = decimal_field(order.get("sz"), "sz")
        filled = decimal_field(order.get("accFillSz"), "accFillSz")
        average = decimal_field(order.get("avgPx"), "avgPx")
        if requested <= 0 or filled > requested or (filled > 0 and average <= 0):
            raise ValueError("Inconsistent OCO child order quantities")
        state = str(order.get("state", "unknown"))
        if state not in {"live", "partially_filled", "filled", "canceled", "mmp_canceled"}:
            raise ValueError("Unrecognized OCO child order state")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT cumulative_fill_size FROM exit_child_orders WHERE entry_client_order_id=? AND exchange_order_id=?",
                             (entry_client_order_id, str(exchange_order_id))).fetchone()
            if not old:
                raise ValueError("Unknown OCO child order")
            if filled < Decimal(old["cumulative_fill_size"]):
                raise ValueError("OCO child cumulative fill regressed")
            db.execute("""UPDATE exit_child_orders SET state=?,requested_size=?,cumulative_fill_size=?,
                average_fill_price=?,updated_at=CURRENT_TIMESTAMP
                WHERE entry_client_order_id=? AND exchange_order_id=?""",
                (state,str(requested),str(filled),str(average),entry_client_order_id,str(exchange_order_id)))
            row = db.execute("SELECT * FROM exit_child_orders WHERE entry_client_order_id=? AND exchange_order_id=?",
                             (entry_client_order_id,str(exchange_order_id))).fetchone()
            return dict(row)

    def save_fill(self, fill: dict[str, Any]) -> bool:
        """Idempotently ingest one OKX Spot execution; duplicate trade IDs never double count."""
        from decimal import Decimal, InvalidOperation
        required = ("instId", "ordId", "tradeId", "side", "fillSz", "fillPx", "fee", "feeCcy")
        if any(fill.get(key) in (None, "") for key in required):
            raise ValueError("Fill is missing a required OKX execution field")
        if fill["side"] not in {"buy", "sell"}:
            raise ValueError("Only Spot buy/sell fills are supported")
        def parse(value, field, *, signed=False):
            try:
                number = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid {field} in OKX fill") from exc
            if not number.is_finite() or (not signed and number <= 0):
                raise ValueError(f"Invalid {field} in OKX fill")
            return number
        size = parse(fill["fillSz"], "fillSz")
        price = parse(fill["fillPx"], "fillPx")
        fee = parse(fill["fee"], "fee", signed=True)
        instrument, order_id, trade_id = str(fill["instId"]), str(fill["ordId"]), str(fill["tradeId"])
        if not order_id.isdigit() or not trade_id.lstrip("-").isdigit():
            raise ValueError("Invalid OKX order/trade ID")
        import json
        raw = json.dumps(fill, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            existing = db.execute("SELECT raw_json FROM fills WHERE instrument=? AND exchange_order_id=? AND trade_id=?",
                                  (instrument,order_id,trade_id)).fetchone()
            if existing:
                if existing["raw_json"] != raw:
                    # Fields like ts may vary in representation; compare economic fields instead.
                    old = db.execute("SELECT side,fill_size,fill_price,fee,fee_currency FROM fills WHERE instrument=? AND exchange_order_id=? AND trade_id=?",
                                     (instrument,order_id,trade_id)).fetchone()
                    new_values = (fill["side"],str(size),str(price),str(fee),str(fill["feeCcy"]))
                    if tuple(old) != new_values:
                        raise ValueError("Conflicting duplicate OKX trade ID; ledger integrity check failed")
                return False
            db.execute("""INSERT INTO fills(instrument,exchange_order_id,trade_id,client_order_id,side,
                fill_size,fill_price,fee,fee_currency,fill_time,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (instrument,order_id,trade_id,str(fill.get("clOrdId") or ""),fill["side"],str(size),str(price),
                 str(fee),str(fill["feeCcy"]),str(fill.get("fillTime") or fill.get("ts") or ""),raw))
            return True

    def get_position_context(self, position_client_order_id: str) -> dict[str, Any]:
        """Return restart-reconciliation facts for the active position only."""
        unresolved = ("reserved", "submitting", "submission_unknown", "acknowledged", "live", "partially_filled")
        with self._connect() as db:
            entry = db.execute("""SELECT i.instrument,i.status,i.exchange_order_id,
                s.exchange_state,s.cumulative_fill_size
                FROM intents i LEFT JOIN order_snapshots s ON s.client_order_id=i.client_order_id
                WHERE i.client_order_id=? AND i.side='buy'""", (position_client_order_id,)).fetchone()
            if not entry:
                raise ValueError("Active position entry intent is missing")
            exits = db.execute("""SELECT COUNT(*) FROM intents
                WHERE position_client_order_id=? AND side='sell' AND status IN (?,?,?,?,?,?)""",
                (position_client_order_id, *unresolved)).fetchone()[0]
            legacy_exits = db.execute("""SELECT COUNT(*) FROM intents
                WHERE instrument=? AND side='sell' AND position_client_order_id IS NULL
                AND status IN (?,?,?,?,?,?)""", (entry["instrument"], *unresolved)).fetchone()[0]
            exits += legacy_exits
            children = db.execute("""SELECT COUNT(*) FROM exit_child_orders
                WHERE entry_client_order_id=? AND state IN ('unknown','live','partially_filled')""",
                (position_client_order_id,)).fetchone()[0]
            return {
                "instrument": entry["instrument"],
                "entry_status": entry["status"],
                "entry_order_id": entry["exchange_order_id"],
                "entry_exchange_state": entry["exchange_state"],
                "entry_snapshot_filled_size": entry["cumulative_fill_size"],
                "unresolved_exit_intents": int(exits),
                "unresolved_child_orders": int(children),
            }

    def get_position_fills(self, position_client_order_id: str) -> list[dict[str, Any]]:
        """Return fills linked to one active entry and its explicit exit orders."""
        with self._connect() as db:
            entry = db.execute("SELECT instrument,exchange_order_id FROM intents WHERE client_order_id=? AND side='buy'",
                               (position_client_order_id,)).fetchone()
            if not entry:
                raise ValueError("Active position entry intent is missing")
            instrument = entry["instrument"]
            order_ids = {str(entry["exchange_order_id"])} if entry["exchange_order_id"] else set()
            rows = db.execute("SELECT exchange_order_id FROM intents WHERE position_client_order_id=? AND instrument=? AND side='sell'",
                              (position_client_order_id, instrument)).fetchall()
            order_ids.update(str(row[0]) for row in rows if row[0])
            rows = db.execute("SELECT exchange_order_id FROM exit_child_orders WHERE entry_client_order_id=? AND instrument=?",
                              (position_client_order_id, instrument)).fetchall()
            order_ids.update(str(row[0]) for row in rows if row[0])
            legacy = db.execute("""SELECT i.exchange_order_id FROM intents i
                JOIN fills f ON f.instrument=i.instrument AND f.exchange_order_id=i.exchange_order_id
                WHERE i.instrument=? AND i.side='sell' AND i.position_client_order_id IS NULL
                AND i.exchange_order_id IS NOT NULL""", (instrument,)).fetchall()
            if legacy:
                raise ValueError("Unlinked legacy sell fills exist; position attribution requires manual reconciliation")
            if not order_ids:
                return []
            placeholders = ",".join("?" for _ in order_ids)
            fills = db.execute(f"SELECT * FROM fills WHERE instrument=? AND exchange_order_id IN ({placeholders}) ORDER BY fill_time,CAST(trade_id AS INTEGER)",
                               [instrument, *sorted(order_ids)]).fetchall()
            return [dict(row) for row in fills]

    def list_fills(self, instrument: str, order_ids: list[str] | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if order_ids is not None:
                ids = [str(x) for x in order_ids]
                if not ids:
                    return []
                placeholders = ",".join("?" for _ in ids)
                rows = db.execute(f"SELECT * FROM fills WHERE instrument=? AND exchange_order_id IN ({placeholders}) ORDER BY fill_time,trade_id",
                                  [instrument,*ids]).fetchall()
            else:
                rows = db.execute("SELECT * FROM fills WHERE instrument=? ORDER BY fill_time,trade_id", (instrument,)).fetchall()
            return [dict(row) for row in rows]

    def list_exit_child_orders(self, entry_client_order_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM exit_child_orders WHERE entry_client_order_id=? ORDER BY exchange_order_id",
                              (entry_client_order_id,)).fetchall()
            return [dict(row) for row in rows]
