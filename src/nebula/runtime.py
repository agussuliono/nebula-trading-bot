"""Read-only, one-cycle market runtime for NEBULA V1.

This module intentionally does not import or call an order manager. It fetches
public market data, evaluates confirmed candles, and may produce a candidate
entry plan. It never authorizes or submits an exchange order.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math

from .entry_plan import EntryPlanPolicy, build_entry_plan
from .instruments import SpotInstrument
from .scanner import (
    build_candidates, candle_range_volatility, rank_candidates,
    valid_traded_candle_count,
)
from .strategy import trend_signal, closed_candles


@dataclass(frozen=True)
class RuntimePolicy:
    bar: str = "1H"
    candle_limit: int = 200
    max_symbols_to_analyze: int = 30
    min_quote_volume: float = 1_000_000.0
    max_spread_pct: float = 0.25
    min_volatility_pct: float = 0.20
    max_volatility_pct: float = 5.0
    min_listing_age_days: int = 30
    min_data_points: int = 51
    fast_ema: int = 20
    slow_ema: int = 50
    max_candle_age_intervals: int = 2
    max_ticker_age_ms: int = 30_000
    entry_plan_policy: EntryPlanPolicy | None = None

    def __post_init__(self):
        if self.bar not in {"1H", "4H", "1D"}:
            raise ValueError("V1 runtime supports only 1H, 4H, or 1D candles")
        if not 2 <= self.candle_limit <= 300:
            raise ValueError("candle_limit must be in 2..300")
        if not 1 <= self.max_symbols_to_analyze <= 100:
            raise ValueError("max_symbols_to_analyze must be in 1..100")
        if self.fast_ema <= 0 or self.slow_ema <= self.fast_ema:
            raise ValueError("EMA periods must satisfy 0 < fast_ema < slow_ema")
        numeric = (self.min_quote_volume, self.max_spread_pct,
                   self.min_volatility_pct, self.max_volatility_pct)
        if not all(math.isfinite(x) for x in numeric):
            raise ValueError("runtime thresholds must be finite")
        if (self.min_quote_volume <= 0 or self.max_spread_pct <= 0 or
                self.min_volatility_pct < 0 or self.max_volatility_pct <= self.min_volatility_pct):
            raise ValueError("invalid scanner thresholds")
        if self.min_listing_age_days < 0 or self.min_data_points < self.slow_ema + 1:
            raise ValueError("invalid listing age or data point minimum")
        if (isinstance(self.max_candle_age_intervals, bool)
                or not isinstance(self.max_candle_age_intervals, int)
                or self.max_candle_age_intervals < 1
                or isinstance(self.max_ticker_age_ms, bool)
                or not isinstance(self.max_ticker_age_ms, int)
                or self.max_ticker_age_ms <= 0):
            raise ValueError("candle age intervals and ticker age must be positive")
        if self.entry_plan_policy is not None and not isinstance(self.entry_plan_policy, EntryPlanPolicy):
            raise ValueError("entry_plan_policy must be an EntryPlanPolicy or None")


class RuntimeErrorSafe(RuntimeError):
    """A fail-closed runtime analysis error."""


class ReadOnlyRuntime:
    """Runs one public-data-only scan/signal/plan cycle; never sends orders."""

    BAR_MS = {"1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}

    def __init__(self, client, state_store, *, policy: RuntimePolicy | None = None,
                 circuit_breaker=None, clock=None):
        self.client = client
        self.state = state_store
        self.policy = policy or RuntimePolicy()
        self.circuit_breaker = circuit_breaker
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self.state._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runtime_signals (
                instrument TEXT NOT NULL,
                candle_ts INTEGER NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(instrument, candle_ts, action)
            )""")

    def _now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise RuntimeErrorSafe("Runtime clock must be timezone-aware")
        return now.astimezone(timezone.utc)

    def _persist_signal(self, instrument: str, candle_ts: int, action: str, reason: str) -> bool:
        with self.state._connect() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO runtime_signals(instrument,candle_ts,action,reason) VALUES(?,?,?,?)",
                (instrument, int(candle_ts), action, reason),
            )
            inserted = cur.rowcount == 1
        if inserted:
            self.state.record_event("runtime_signal_detected", None,
                                    {"instrument": instrument, "candle_ts": int(candle_ts),
                                     "action": action, "reason": reason})
        return inserted

    def _candidate_plan(self, *, instrument_row, ticker, candles, signal, now_ms):
        if self.policy.entry_plan_policy is None:
            return None, "disabled_not_configured"
        try:
            spec = SpotInstrument.from_okx(instrument_row)
            ask = ticker.get("askPx")
            if ask in (None, ""):
                raise ValueError("fresh ticker ask price is missing")
            plan = build_entry_plan(
                candles=candles, signal=signal, entry_price=ask,
                instrument=spec, policy=self.policy.entry_plan_policy,
                now_ms=now_ms, bar_ms=self.BAR_MS[self.policy.bar],
            )
            return plan, "candidate_only_not_authorized"
        except Exception as exc:
            return None, "rejected:" + type(exc).__name__

    def run_once(self) -> dict:
        now = self._now()
        now_ms = int(now.timestamp() * 1000)
        try:
            instruments = self.client.get_instruments()
            tickers = self.client.get_tickers()
        except Exception as exc:
            self.state.record_event("runtime_market_data_error", None,
                                    {"stage": "universe", "error_type": type(exc).__name__})
            raise RuntimeErrorSafe("Public market universe unavailable; cycle aborted") from exc
        if not isinstance(instruments, list) or not isinstance(tickers, list):
            raise RuntimeErrorSafe("Invalid public market response shape")

        raw_candidates = build_candidates(instruments, tickers, now_ms=now_ms,
                                           max_ticker_age_ms=self.policy.max_ticker_age_ms)
        tick_by_id = {x.get("instId"): x for x in tickers if isinstance(x, dict) and x.get("instId")}
        instrument_by_id = {x.get("instId"): x for x in instruments
                            if isinstance(x, dict) and x.get("instId")}
        enriched = []
        candles_by_id = {}
        stale = []
        data_quality_rejected = []
        errors = []
        interval = self.BAR_MS[self.policy.bar]

        # Prioritize liquid instruments before making bounded candle requests.
        raw_candidates.sort(key=lambda x: (x.quote_volume, -x.spread_pct), reverse=True)
        for candidate in raw_candidates[:self.policy.max_symbols_to_analyze]:
            try:
                candles = self.client.get_candles(candidate.inst_id, self.policy.bar,
                                                  self.policy.candle_limit)
                if not isinstance(candles, list):
                    raise ValueError("invalid candle response")
                closed = closed_candles(candles)
                if not closed:
                    stale.append(candidate.inst_id)
                    continue
                last_ts = int(closed[-1][0])
                age_ms = now_ms - last_ts
                if age_ms < 0 or age_ms > interval * self.policy.max_candle_age_intervals:
                    stale.append(candidate.inst_id)
                    continue
                points = valid_traded_candle_count(candles)
                if points < self.policy.min_data_points:
                    data_quality_rejected.append({
                        "instrument": candidate.inst_id,
                        "valid_traded_candles": points,
                        "required": self.policy.min_data_points,
                    })
                    continue
                volatility, volatility_points = candle_range_volatility(candles, window=24)
                if volatility_points == 0:
                    data_quality_rejected.append({
                        "instrument": candidate.inst_id,
                        "valid_traded_candles": points,
                        "required": self.policy.min_data_points,
                    })
                    continue
                candles_by_id[candidate.inst_id] = candles
                enriched.append(type(candidate)(candidate.inst_id, candidate.quote_volume,
                                                candidate.spread_pct, volatility,
                                                candidate.listing_age_days, points))
            except Exception as exc:
                errors.append({"instrument": candidate.inst_id,
                               "error_type": type(exc).__name__, "stage": "candles"})

        ranked = rank_candidates(
            enriched, min_quote_volume=self.policy.min_quote_volume,
            max_spread_pct=self.policy.max_spread_pct,
            min_volatility_pct=self.policy.min_volatility_pct,
            max_volatility_pct=self.policy.max_volatility_pct,
            min_listing_age_days=self.policy.min_listing_age_days,
            min_data_points=self.policy.min_data_points,
        )
        lock = self.state.get_position_lock()
        breaker_status = self.circuit_breaker.status(now=now) if self.circuit_breaker else None
        reports = []
        for candidate in ranked:
            try:
                candles = candles_by_id[candidate.inst_id]
                ticker = tick_by_id[candidate.inst_id]
                signal = trend_signal(candles, self.policy.fast_ema, self.policy.slow_ema)
                if signal.candle_ts <= 0:
                    continue
                is_new = self._persist_signal(candidate.inst_id, signal.candle_ts,
                                              signal.action, signal.reason)
                blocked_reason = None
                if signal.action == "buy" and lock:
                    blocked_reason = "position_already_active"
                elif signal.action == "buy" and breaker_status and breaker_status["halted"]:
                    blocked_reason = "risk_circuit_breaker_halted"

                candidate_plan = None
                plan_status = "not_applicable"
                if signal.action == "buy" and not blocked_reason:
                    candidate_plan, plan_status = self._candidate_plan(
                        instrument_row=instrument_by_id.get(candidate.inst_id),
                        ticker=ticker, candles=candles, signal=signal, now_ms=now_ms,
                    )

                reports.append({
                    "instrument": candidate.inst_id,
                    "last_price": ticker.get("last"),
                    "quote_volume_24h": candidate.quote_volume,
                    "spread_pct": candidate.spread_pct,
                    "volatility_pct": candidate.volatility_pct,
                    "listing_age_days": candidate.listing_age_days,
                    "closed_candle_count": candidate.data_points,
                    "candle_ts": signal.candle_ts,
                    "signal": signal.action,
                    "reason": signal.reason,
                    "new_signal": is_new,
                    "blocked_reason": blocked_reason,
                    "candidate_entry_plan": candidate_plan,
                    "entry_plan_status": plan_status,
                    "authorization": "not_requested_read_only_runtime",
                    "execution": "not_submitted_read_only_runtime",
                })
            except Exception as exc:
                errors.append({"instrument": candidate.inst_id,
                               "error_type": type(exc).__name__, "stage": "signal_or_plan"})

        result = {
            "mode": "read_only_analysis",
            "orders_enabled": False,
            "timestamp_utc": now.isoformat(),
            "bar": self.policy.bar,
            "universe_count": len(raw_candidates),
            "analyzed_count": min(len(raw_candidates), self.policy.max_symbols_to_analyze),
            "eligible_count": len(ranked),
            "stale_instruments": stale,
            "data_quality_rejected": data_quality_rejected,
            "errors": errors,
            "position_lock_active": bool(lock),
            "risk_controls": breaker_status,
            "signals": reports,
            "order_submission_attempts": 0,
        }
        self.state.record_event("runtime_cycle_completed", None, {
            "bar": self.policy.bar, "universe_count": result["universe_count"],
            "eligible_count": result["eligible_count"],
            "signal_count": len(reports), "errors": len(errors),
            "mode": result["mode"],
        })
        return result


def result_json(result: dict) -> str:
    return json.dumps(result, sort_keys=True, indent=2, default=str)
