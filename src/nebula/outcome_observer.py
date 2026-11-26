"""Fase 0C outcome observer: reconstruct position lifecycle outcomes.

Reads only reconciled trading-state facts and records allowlisted outcome
events. It never changes order lifecycle, risk authorization, reconciliation,
or position state, and export failures never alter trading behaviour.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any

from .accounting import AccountingError, round_trip_accounting
from .state import StateStore

TERMINAL_ENTRY_STATUSES = {"filled", "canceled", "rejected", "mmp_canceled"}


def _event_id(trade_episode_id: str, event_type: str, discriminator: str) -> str:
    raw = f"{trade_episode_id}|{event_type}|{discriminator}".encode()
    return hashlib.sha256(raw).hexdigest()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _payload(episode: dict[str, Any], event_type: str, discriminator: str,
             **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "outcome_event_id": _event_id(episode["trade_episode_id"], event_type, discriminator),
        "trade_episode_id": episode["trade_episode_id"],
        "instrument": episode["instrument"],
        "event_type": event_type,
    }
    if episode.get("signal_key"):
        payload["signal_key"] = episode["signal_key"]
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    return payload


def _fill_fields(fill: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_time_utc": fill.get("fill_time") or None,
        "side": fill.get("side"),
        "fill_price": str(fill.get("fill_price")),
        "fill_size": str(fill.get("fill_size")),
        "fee": str(fill.get("fee")),
    }


def observe_episode(state: StateStore, episode: dict[str, Any],
                    exporter: Any | None = None) -> list[dict[str, Any]]:
    """Record and optionally export outcome events for one trade episode."""
    entry_id = episode["entry_client_order_id"]
    intent = state.get_intent(entry_id)
    if intent is None:
        return []
    events: list[dict[str, Any]] = []

    def emit(payload: dict[str, Any]) -> None:
        events.append(payload)

    emit(_payload(episode, "entry_intent_created", entry_id,
                  event_time_utc=episode.get("created_at"), side="buy"))

    try:
        fills = state.get_position_fills(entry_id)
        fills_error = False
    except ValueError:
        fills, fills_error = [], True
    buy_fills = [f for f in fills if f.get("side") == "buy"]
    sell_fills = [f for f in fills if f.get("side") == "sell"]
    buy_total = sum((_decimal(f["fill_size"]) for f in buy_fills), Decimal("0"))

    risk_plan = None
    try:
        risk_plan = state.get_entry_risk_plan(entry_id)
    except Exception:
        risk_plan = None
    plan_price = _decimal(risk_plan["entry_price"]) if risk_plan else Decimal("0")

    for fill in buy_fills:
        fields = _fill_fields(fill)
        if plan_price > 0:
            slippage = ((_decimal(fill["fill_price"]) - plan_price) / plan_price * 10000)
            fields["observed_slippage_bps"] = str(slippage.quantize(Decimal("0.0001")))
        emit(_payload(episode, "entry_filled",
                      f"{fill['exchange_order_id']}|{fill['trade_id']}", **fields))

    entry_status = str(intent.get("status") or "")
    snapshot = state.get_order_snapshot(entry_id)
    requested = _decimal(snapshot["requested_size"]) if snapshot else Decimal("0")
    if entry_status in {"canceled", "mmp_canceled"} and buy_total > 0 and requested > buy_total:
        emit(_payload(episode, "entry_remainder_cancelled", entry_status, side="buy"))
    if entry_status in {"canceled", "rejected", "mmp_canceled"} and buy_total == 0:
        emit(_payload(episode, "entry_not_filled", entry_status, side="buy"))

    protection = state.get_protection_intent(entry_id)
    if protection and protection.get("status") in {"active", "triggered"}:
        emit(_payload(episode, "protection_activated", str(protection["status"]), side="sell"))

    cumulative_sell = Decimal("0")
    for fill in sell_fills:
        cumulative_sell += _decimal(fill["fill_size"])
        event_type = "exit_filled" if buy_total > 0 and cumulative_sell >= buy_total else "exit_partial_filled"
        emit(_payload(episode, event_type,
                      f"{fill['exchange_order_id']}|{fill['trade_id']}", **_fill_fields(fill)))

    accounting: dict[str, Any] | None = None
    try:
        accounting = round_trip_accounting(fills, instrument=episode["instrument"])
    except AccountingError:
        accounting = None
    lock = state.get_position_lock()
    lock_released = lock is None or lock.get("owner_client_order_id") != entry_id
    flat = bool(accounting and accounting["is_flat_by_fill_ledger"])
    entry_not_filled = entry_status in {"canceled", "rejected", "mmp_canceled"} and buy_total == 0

    incomplete = fills_error or accounting is None or bool(
        accounting and accounting["unsupported_fee_currencies"]
    )
    if not incomplete and sell_fills and not flat and entry_status in TERMINAL_ENTRY_STATUSES:
        incomplete = True
    if incomplete:
        emit(_payload(episode, "reconciliation_incomplete", "accounting", side="sell"))

    closed = lock_released and (flat or entry_not_filled) and not fills_error
    if closed:
        emit(_payload(episode, "position_closed", "flat", side="sell"))
        if accounting and accounting["accounting_complete"]:
            emit(_payload(episode, "accounting_finalized", "final", side="sell",
                          net_pnl_quote=accounting["realized_pnl_quote"]))

    recorded: list[dict[str, Any]] = []
    for payload in events:
        if state.record_outcome_event(payload["outcome_event_id"],
                                      episode["trade_episode_id"],
                                      payload["event_type"], payload):
            recorded.append(payload)
    if closed:
        if accounting and accounting["accounting_complete"]:
            state.update_trade_episode(episode["trade_episode_id"],
                                       status="accounting_finalized",
                                       net_pnl_quote=accounting["realized_pnl_quote"])
        else:
            state.update_trade_episode(episode["trade_episode_id"], status="closed")

    if exporter is not None:
        exported_ids = []
        for event in state.list_outcome_events(episode["trade_episode_id"], unexported_only=True):
            try:
                if exporter.export(event["payload"]):
                    exported_ids.append(event["outcome_event_id"])
            except Exception:
                continue
        state.mark_outcome_exported(exported_ids)
    return recorded


def observe_all(state: StateStore, exporter: Any | None = None) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    for episode in state.list_trade_episodes():
        recorded.extend(observe_episode(state, episode, exporter))
    return recorded
