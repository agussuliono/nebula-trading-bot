"""Persistent, fail-closed daily loss circuit breaker and emergency stop."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .config import SafetyError


class CircuitBreaker:
    """UTC-day realized-loss guard backed by the project's SQLite state DB.

    Closed-trade events are idempotent by position ID. Manual stop survives date
    rollover and process restarts; clearing it requires an explicit caller flag.
    """
    def __init__(self, state_store, max_daily_loss_quote):
        self.state = state_store
        try:
            self.limit = Decimal(str(max_daily_loss_quote))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("max_daily_loss_quote must be a finite positive decimal") from exc
        if not self.limit.is_finite() or self.limit <= 0:
            raise ValueError("max_daily_loss_quote must be a finite positive decimal")
        with self.state._connect() as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"daily_pnl_events", "emergency_stop"}.issubset(tables):
                raise ValueError("Circuit breaker safety state is missing; manual recovery required")
            row = db.execute("SELECT engaged FROM emergency_stop WHERE singleton=1").fetchone()
            if row is None:
                raise ValueError("Emergency stop state is missing; manual recovery required")

    @staticmethod
    def utc_day(now=None):
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return value.astimezone(timezone.utc).date().isoformat()

    @staticmethod
    def _amount(value):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("realized P&L must be a finite decimal") from exc
        if not result.is_finite():
            raise ValueError("realized P&L must be finite")
        return result

    def record_closed_trade(self, position_id: str, realized_pnl_quote, *, now=None) -> dict:
        if not position_id or len(position_id) > 128:
            raise ValueError("position_id is required and must be <=128 characters")
        amount = self._amount(realized_pnl_quote)
        day = self.utc_day(now)
        with self.state._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT trading_day,realized_pnl_quote FROM daily_pnl_events WHERE position_id=?",
                                  (position_id,)).fetchone()
            if existing:
                if Decimal(existing["realized_pnl_quote"]) != amount:
                    raise ValueError("Conflicting duplicate closed-trade P&L event")
                # A retry after UTC midnight belongs to the original durable event day.
                day = existing["trading_day"]
                inserted = False
            else:
                db.execute("INSERT INTO daily_pnl_events(position_id,trading_day,realized_pnl_quote) VALUES(?,?,?)",
                           (position_id, day, str(amount)))
                inserted = True
            # Sum persisted decimal strings in Python; never use SQLite REAL for money.
            rows = db.execute("SELECT realized_pnl_quote FROM daily_pnl_events WHERE trading_day=?", (day,)).fetchall()
            total = sum((Decimal(row[0]) for row in rows), Decimal("0"))
            tripped = total <= -self.limit
            db.execute("INSERT INTO audit_events(event_type,client_order_id,details_json) VALUES(?,?,?)",
                       ("daily_pnl_recorded", position_id,
                        __import__("json").dumps({"day": day, "realized_pnl_quote": str(amount),
                                                   "daily_realized_pnl_quote": str(total),
                                                   "limit": str(self.limit), "circuit_breaker_tripped": tripped}, sort_keys=True)))
        return {"trading_day": day, "daily_realized_pnl_quote": str(total),
                "max_daily_loss_quote": str(self.limit), "tripped": tripped, "inserted": inserted}

    def engage_emergency_stop(self, reason="manual emergency stop"):
        safe_reason = str(reason).strip()[:200] or "manual emergency stop"
        with self.state._connect() as db:
            db.execute("UPDATE emergency_stop SET engaged=1,reason=?,updated_at=CURRENT_TIMESTAMP WHERE singleton=1",
                       (safe_reason,))
            db.execute("INSERT INTO audit_events(event_type,details_json) VALUES(?,?)",
                       ("emergency_stop_engaged", __import__("json").dumps({"reason": safe_reason})))
        return self.status()

    def clear_emergency_stop(self, *, explicit_confirmation=False):
        if explicit_confirmation is not True:
            raise SafetyError("Clearing emergency stop requires explicit confirmation")
        with self.state._connect() as db:
            db.execute("UPDATE emergency_stop SET engaged=0,reason='',updated_at=CURRENT_TIMESTAMP WHERE singleton=1")
            db.execute("INSERT INTO audit_events(event_type,details_json) VALUES(?,?)",
                       ("emergency_stop_cleared", "{}"))
        return self.status()

    def status(self, *, now=None):
        day = self.utc_day(now)
        with self.state._connect() as db:
            stop = db.execute("SELECT engaged,reason FROM emergency_stop WHERE singleton=1").fetchone()
            rows = db.execute("SELECT realized_pnl_quote FROM daily_pnl_events WHERE trading_day=?", (day,)).fetchall()
        total = sum((Decimal(row[0]) for row in rows), Decimal("0"))
        daily_tripped = total <= -self.limit
        manual = bool(stop["engaged"])
        return {"trading_day": day, "daily_realized_pnl_quote": str(total),
                "max_daily_loss_quote": str(self.limit), "daily_loss_tripped": daily_tripped,
                "manual_stop": manual, "manual_stop_reason": stop["reason"],
                "halted": daily_tripped or manual}

    def check_order_allowed(self, side: str, *, now=None):
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        state = self.status(now=now)
        # Manual emergency stop blocks all new regular orders. The exchange-level
        # kill switch independently blocks every write endpoint, including OCO.
        if state["manual_stop"]:
            raise SafetyError("Order submission blocked by emergency stop")
        # Daily loss lockout blocks new risk-taking entries, not risk-reducing sells.
        if side == "buy" and state["daily_loss_tripped"]:
            raise SafetyError("New entries blocked by daily loss circuit breaker")
        return state

    def check_entry_allowed(self, *, now=None):
        return self.check_order_allowed("buy", now=now)
