"""Fase 0C outcome observer: reconstruct position lifecycle outcomes.

Reads only reconciled trading-state facts and records allowlisted outcome
events. It never changes order lifecycle, risk authorization, reconciliation,
or position state, and export failures never alter trading behaviour.
Final outcomes require a fill ledger proven complete against order
snapshots; an empty or incomplete ledger is never treated as flat.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any

from .accounting import AccountingError, round_trip_accounting
from .state import StateStore

TERMINAL_ENTRY_STATUSES = {"filled", "canceled", "rejected", "mmp_canceled"}
TERMINAL_ORDER_STATES = {"filled", "canceled", "mmp_canceled"}


def _event_id(trade_episode_id: str, event_type: str, discriminator: str) -> str:
    raw = f"{trade_episode_id}|{event_type}|{discriminator}".encode()
    return hashlib.sha256(raw).hexdigest()


def _strict_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


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
        "fee_currency": str(fill.get("fee_currency")),
    }


def _ledger_completeness(state: StateStore, entry_id: str,
                         fills: list[dict[str, Any]]) -> tuple[bool, bool]:
    """Return (ledger_complete, fills_valid) against order snapshots.

    Every order's ledger fill total must equal its snapshot cumulative fill.
    A terminal order without a snapshot, or any mismatch, is incomplete.
    """
    fills_valid = True
    ledger_by_order: dict[str, Decimal] = {}
    for fill in fills:
        size = _strict_decimal(fill.get("fill_size"))
        price = _strict_decimal(fill.get("fill_price"))
        fee = _strict_decimal(fill.get("fee"))
        if size is None or price is None or fee is None or size <= 0 or price <= 0:
            fills_valid = False
            continue
        order_id = str(fill.get("exchange_order_id") or "")
        ledger_by_order[order_id] = ledger_by_order.get(order_id, Decimal("0")) + size
    if not fills_valid:
        return False, False

    complete = True
    known_orders: set[str] = set()
    for intent in state.list_position_intents(entry_id):
        snapshot = state.get_order_snapshot(intent["client_order_id"])
        order_id = str((snapshot or {}).get("exchange_order_id")
                       or intent.get("exchange_order_id") or "")
        if order_id:
            known_orders.add(order_id)
        if snapshot is None:
            status = str(intent.get("status") or "")
            if status in TERMINAL_ENTRY_STATUSES and status != "rejected":
                complete = False
            continue
        cumulative = _strict_decimal(snapshot.get("cumulative_fill_size"))
        if cumulative is None:
            complete = False
            continue
        if ledger_by_order.get(order_id, Decimal("0")) != cumulative:
            complete = False
    for child in state.list_exit_child_orders(entry_id):
        order_id = str(child.get("exchange_order_id") or "")
        known_orders.add(order_id)
        cumulative = _strict_decimal(child.get("cumulative_fill_size"))
        if cumulative is None or ledger_by_order.get(order_id, Decimal("0")) != cumulative:
            complete = False
    if set(ledger_by_order) - known_orders:
        complete = False
    return complete, True


def _proven_zero_fill(intent: dict[str, Any], snapshot: dict[str, Any] | None,
                      buy_total: Decimal) -> bool:
    if buy_total != 0:
        return False
    status = str(intent.get("status") or "")
    if status == "rejected":
        if snapshot is None:
            return True
        cumulative = _strict_decimal(snapshot.get("cumulative_fill_size"))
        return cumulative == 0
    if status not in {"canceled", "mmp_canceled"} or snapshot is None:
        return False
    cumulative = _strict_decimal(snapshot.get("cumulative_fill_size"))
    return (cumulative == 0
            and str(snapshot.get("exchange_state") or "") in {"canceled", "mmp_canceled"})


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
    buy_total = sum((_strict_decimal(f["fill_size"]) or Decimal("0") for f in buy_fills),
                    Decimal("0"))

    ledger_complete, fills_valid = (False, False) if fills_error else \
        _ledger_completeness(state, entry_id, fills)

    risk_plan = None
    try:
        risk_plan = state.get_entry_risk_plan(entry_id)
    except Exception:
        risk_plan = None
    plan_price = _strict_decimal(risk_plan["entry_price"]) if risk_plan else None

    for fill in buy_fills:
        fields = _fill_fields(fill)
        price = _strict_decimal(fill["fill_price"])
        if plan_price and plan_price > 0 and price is not None:
            slippage = (price - plan_price) / plan_price * 10000
            fields["observed_slippage_bps"] = str(slippage.quantize(Decimal("0.0001")))
        emit(_payload(episode, "entry_filled",
                      f"{fill['exchange_order_id']}|{fill['trade_id']}", **fields))

    entry_status = str(intent.get("status") or "")
    snapshot = state.get_order_snapshot(entry_id)
    requested = _strict_decimal(snapshot["requested_size"]) if snapshot else None
    cumulative = _strict_decimal(snapshot["cumulative_fill_size"]) if snapshot else None
    zero_fill = _proven_zero_fill(intent, snapshot, buy_total)
    if (entry_status in {"canceled", "mmp_canceled"} and buy_total > 0
            and requested is not None and cumulative == buy_total
            and requested > buy_total):
        emit(_payload(episode, "entry_remainder_cancelled", entry_status, side="buy"))
    if zero_fill:
        emit(_payload(episode, "entry_not_filled", entry_status, side="buy"))

    protection = state.get_protection_intent(entry_id)
    if protection and protection.get("status") in {"active", "triggered"}:
        emit(_payload(episode, "protection_activated", str(protection["status"]),
                      side="sell", protection_status=str(protection["status"])))

    base_ccy = episode["instrument"].split("-", 1)[0]
    inventory = Decimal("0")
    for fill in fills:
        size = _strict_decimal(fill["fill_size"]) or Decimal("0")
        fee = _strict_decimal(fill["fee"]) or Decimal("0")
        base_fee = fee if fill.get("fee_currency") == base_ccy else Decimal("0")
        if fill.get("side") == "buy":
            inventory += size + base_fee
        elif fill.get("side") == "sell":
            inventory -= size
            inventory += base_fee
            event_type = "exit_filled" if inventory == 0 else "exit_partial_filled"
            emit(_payload(episode, event_type,
                          f"{fill['exchange_order_id']}|{fill['trade_id']}",
                          **_fill_fields(fill)))

    accounting: dict[str, Any] | None = None
    try:
        accounting = round_trip_accounting(fills, instrument=episode["instrument"])
    except AccountingError:
        accounting = None
    lock = state.get_position_lock()
    lock_not_owned = lock is None or lock.get("owner_client_order_id") != entry_id
    lock_released = lock_not_owned and state.has_lock_release_evidence(entry_id)
    flat = bool(accounting and accounting["is_flat_by_fill_ledger"])

    incomplete = (
        fills_error or not fills_valid or not ledger_complete
        or accounting is None
        or bool(accounting and accounting["unsupported_fee_currencies"])
        or (lock_not_owned and not lock_released)
    )
    if not incomplete and sell_fills and not flat and entry_status in TERMINAL_ENTRY_STATUSES:
        incomplete = True
    if entry_status in TERMINAL_ENTRY_STATUSES and snapshot is None and not zero_fill:
        incomplete = True
    if incomplete:
        emit(_payload(episode, "reconciliation_incomplete", "accounting", side="sell"))

    closed = (lock_released and not incomplete and (flat or zero_fill)
              and accounting is not None)
    if closed:
        emit(_payload(episode, "position_closed", "flat", side="sell"))
        if accounting["accounting_complete"]:
            emit(_payload(episode, "accounting_finalized", "final", side="sell",
                          net_pnl_quote=accounting["realized_pnl_quote"]))

    recorded: list[dict[str, Any]] = []
    for payload in events:
        if state.record_outcome_event(payload["outcome_event_id"],
                                      episode["trade_episode_id"],
                                      payload["event_type"], payload):
            recorded.append(payload)
    if closed:
        if accounting["accounting_complete"]:
            state.update_trade_episode(episode["trade_episode_id"],
                                       status="accounting_finalized",
                                       net_pnl_quote=accounting["realized_pnl_quote"])
        else:
            state.update_trade_episode(episode["trade_episode_id"], status="closed")

    if exporter is not None:
        exported_ids = []
        for event in state.list_outcome_events(episode["trade_episode_id"], unexported_only=True):
            try:
                payload = StateStore.validate_outcome_payload(event["payload"])
                if (payload["outcome_event_id"] != event["outcome_event_id"]
                        or payload["trade_episode_id"] != event["trade_episode_id"]
                        or payload["event_type"] != event["event_type"]):
                    continue
                if exporter.export(payload):
                    exported_ids.append(event["outcome_event_id"])
            except Exception:
                continue
        state.mark_outcome_exported(exported_ids)
    return recorded


def observe_all(state: StateStore, exporter: Any | None = None) -> dict[str, Any]:
    """Observe every episode and always report legacy positions without one."""
    recorded: list[dict[str, Any]] = []
    for episode in state.list_trade_episodes():
        recorded.extend(observe_episode(state, episode, exporter))
    return {
        "recorded_events": recorded,
        "legacy_episode_unavailable": legacy_episode_report(state),
    }


def legacy_episode_report(state: StateStore) -> list[dict[str, Any]]:
    """Explicitly report buy intents that have no trade episode.

    Legacy positions are never given a fabricated episode; they require
    separate manual reconciliation.
    """
    lock = state.get_position_lock()
    report = []
    for intent in state.list_entry_intents_without_episode():
        report.append({
            "entry_client_order_id": intent["client_order_id"],
            "instrument": intent["instrument"],
            "status": intent["status"],
            "holds_position_lock": bool(
                lock and lock.get("owner_client_order_id") == intent["client_order_id"]
            ),
            "reason": "legacy_episode_unavailable",
        })
    return report
