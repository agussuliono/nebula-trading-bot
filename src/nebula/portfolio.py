"""Fail-closed, read-only Spot portfolio valuation and mark-to-market risk."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class PortfolioError(ValueError):
    pass


def _d(value, field: str, *, allow_zero=True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PortfolioError(f"Invalid {field}") from exc
    if not result.is_finite() or (result < 0 if allow_zero else result <= 0):
        raise PortfolioError(f"Invalid {field}")
    return result


def _signed_d(value, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PortfolioError(f"Invalid {field}") from exc
    if not result.is_finite():
        raise PortfolioError(f"Invalid {field}")
    return result


@dataclass(frozen=True)
class PortfolioPolicy:
    max_snapshot_age_ms: int = 30_000
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("10")
    max_daily_loss_quote: Decimal = Decimal("0")
    max_position_risk_quote: Decimal = Decimal("0")
    max_position_notional_quote: Decimal = Decimal("0")
    max_spread_pct: Decimal = Decimal("0.25")
    balance_tolerance: Decimal = Decimal("0.00000001")

    def __post_init__(self):
        if isinstance(self.max_snapshot_age_ms, bool) or not isinstance(self.max_snapshot_age_ms, int) or self.max_snapshot_age_ms <= 0:
            raise ValueError("max_snapshot_age_ms must be a positive integer")
        for name in ("fee_bps", "slippage_bps", "max_daily_loss_quote", "max_position_risk_quote", "max_position_notional_quote", "max_spread_pct", "balance_tolerance"):
            object.__setattr__(self, name, _d(getattr(self, name), name))
        if self.max_spread_pct <= 0:
            raise ValueError("max_spread_pct must be positive")
        if self.fee_bps + self.slippage_bps >= Decimal("10000"):
            raise ValueError("combined fee and slippage assumptions must be below 10000 bps")
        if (self.max_daily_loss_quote > 0 and self.max_position_risk_quote > 0
                and self.max_position_risk_quote > self.max_daily_loss_quote):
            raise ValueError("per-position risk cannot exceed the daily loss limit")


def calculate_inventory_basis(fills: list[dict], instrument: str) -> dict:
    """Calculate remaining average-cost inventory, including base/quote fees."""
    try:
        base_ccy, quote_ccy = instrument.split("-", 1)
    except ValueError as exc:
        raise PortfolioError("Invalid position instrument") from exc
    if not base_ccy or quote_ccy != "USDT":
        raise PortfolioError("V1 position accounting requires a USDT-quoted Spot instrument")
    quantity = Decimal("0")
    cost = Decimal("0")
    buy_count = sell_count = 0
    ordered_fills = sorted(fills, key=lambda row: (str(row.get("fill_time") or ""), int(str(row.get("trade_id") or "0"))))
    for fill in ordered_fills:
        if not fill.get("fill_time"):
            raise PortfolioError("Fill timestamp is missing; inventory ordering is uncertain")
        size = _d(fill.get("fill_size"), "fill size", allow_zero=False)
        price = _d(fill.get("fill_price"), "fill price", allow_zero=False)
        fee = _signed_d(fill.get("fee"), "fill fee")
        side, fee_ccy = fill.get("side"), fill.get("fee_currency")
        gross = size * price
        if fee_ccy == base_ccy:
            base_fee, quote_fee = fee, Decimal("0")
        elif fee_ccy == quote_ccy:
            base_fee, quote_fee = Decimal("0"), fee
        else:
            raise PortfolioError(f"Unsupported fee currency: {fee_ccy}")
        if side == "buy":
            quantity += size + base_fee
            cost += gross - quote_fee
            buy_count += 1
        elif side == "sell":
            consumed = size - base_fee
            if consumed <= 0 or consumed > quantity:
                raise PortfolioError("Sell fill exceeds locally tracked inventory")
            if quantity <= 0:
                raise PortfolioError("Sell fill has no matching buy inventory")
            average_cost = cost / quantity
            cost -= average_cost * consumed
            quantity -= consumed
            sell_count += 1
        else:
            raise PortfolioError("Unsupported Spot fill side")
        if quantity < 0 or cost < 0:
            raise PortfolioError("Position ledger became negative")
    if quantity <= 0 or cost <= 0:
        raise PortfolioError("Active position has no positive remaining cost basis")
    return {
        "base_currency": base_ccy,
        "quote_currency": quote_ccy,
        "quantity": quantity,
        "remaining_cost_quote": cost,
        "average_entry_price": cost / quantity,
        "buy_fill_count": buy_count,
        "sell_fill_count": sell_count,
    }


class PortfolioRiskEngine:
    """Evaluate a single authenticated Demo snapshot; never submits orders."""
    def __init__(self, policy: PortfolioPolicy):
        self.policy = policy

    def evaluate(self, *, account_config: list[dict], balance: list[dict], tickers: list[dict],
                 position_lock: dict | None = None, position_fills: list[dict] | None = None,
                 position_context: dict | None = None, protection_intent: dict | None = None,
                 open_regular_orders: list[dict] | None = None,
                 open_algo_orders: list[dict] | None = None,
                 local_unresolved_intents: list[dict] | None = None,
                 planned_entry: dict | None = None,
                 position_fill_sync_error: str | None = None,
                 now_ms: int, daily_realized_pnl_quote="0",
                 daily_unrealized_pnl_quote=None, daily_account_pnl_quote=None,
                 require_daily_account_pnl=False) -> dict:
        blockers = []
        if position_fill_sync_error:
            blockers.append("position_fill_sync_failed:" + str(position_fill_sync_error))
        open_regular_orders = open_regular_orders or []
        open_algo_orders = open_algo_orders or []
        local_unresolved_intents = local_unresolved_intents or []
        if local_unresolved_intents:
            blockers.append("local_unresolved_order_intents_present")
        if open_regular_orders:
            blockers.append("exchange_open_regular_orders_present")
        if (self.policy.max_daily_loss_quote <= 0 or self.policy.max_position_risk_quote <= 0
                or self.policy.max_position_notional_quote <= 0):
            blockers.append("risk_limits_not_configured")
        if not isinstance(account_config, list) or len(account_config) != 1:
            raise PortfolioError("Account configuration snapshot is missing or ambiguous")
        config = account_config[0]
        if str(config.get("acctLv")) != "1":
            blockers.append("account_not_spot_mode")
        if "autoLoan" not in config:
            blockers.append("account_auto_loan_status_unknown")
        elif str(config.get("autoLoan")).lower() == "true":
            blockers.append("auto_borrow_enabled")
        elif str(config.get("autoLoan")).lower() != "false":
            blockers.append("account_auto_loan_status_unknown")
        if "enableSpotBorrow" not in config:
            blockers.append("spot_borrow_permission_status_unknown")
        elif str(config.get("enableSpotBorrow")).lower() == "true":
            blockers.append("spot_borrow_enabled")
        elif str(config.get("enableSpotBorrow")).lower() != "false":
            blockers.append("spot_borrow_permission_status_unknown")
        permissions = {part.strip().lower() for part in str(config.get("perm", "")).split(",") if part.strip()}
        if "read_only" not in permissions:
            blockers.append("api_key_read_permission_missing")
        if "trade" not in permissions:
            blockers.append("api_key_trade_permission_missing")
        # Execution keys are least-privilege: exactly Read + Trade.
        # An allowlist also rejects new or unknown OKX permission labels by default.
        for permission in sorted(permissions - {"read_only", "trade"}):
            if permission == "withdraw":
                blockers.append("api_key_withdraw_permission_forbidden")
            elif permission in {"transfer", "loan", "earn", "onchain"}:
                blockers.append("api_key_" + permission + "_permission_forbidden")
            else:
                blockers.append("api_key_unrecognized_or_unapproved_permission")
        if not isinstance(balance, list) or len(balance) != 1:
            raise PortfolioError("Balance snapshot is missing or ambiguous")
        account = balance[0]
        balance_ts = _d(account.get("uTime"), "balance timestamp", allow_zero=False)
        if now_ms < int(balance_ts) or now_ms - int(balance_ts) > self.policy.max_snapshot_age_ms:
            blockers.append("stale_balance_snapshot")
        details = account.get("details")
        if not isinstance(details, list):
            raise PortfolioError("Balance details are missing")
        if not isinstance(tickers, list):
            raise PortfolioError("Ticker snapshot is invalid")
        ticker_map = {}
        for row in tickers:
            if not isinstance(row, dict) or not row.get("instId"):
                continue
            inst_id = row["instId"]
            if inst_id in ticker_map:
                raise PortfolioError(f"Duplicate ticker row for {inst_id}")
            ticker_map[inst_id] = row
        holdings = []
        equity = Decimal("0")
        unpriced = []
        nonzero = {}
        available_by_ccy = {}
        seen_currencies = set()
        for item in details:
            ccy = str(item.get("ccy") or "")
            if ccy in seen_currencies:
                raise PortfolioError(f"Duplicate balance row for {ccy}")
            seen_currencies.add(ccy)
            if not ccy:
                raise PortfolioError("Balance row has no currency")
            amount = _d(item.get("eq"), f"{ccy} equity")
            available = _d(item.get("availBal"), f"{ccy} available balance")
            frozen = _d(item.get("frozenBal"), f"{ccy} frozen balance")
            if (available > amount + self.policy.balance_tolerance
                    or frozen > amount + self.policy.balance_tolerance
                    or available + frozen > amount + self.policy.balance_tolerance):
                raise PortfolioError(f"Inconsistent {ccy} available/frozen balance")
            available_by_ccy[ccy] = available
            if amount == 0:
                continue
            nonzero[ccy] = amount
            if ccy == "USDT":
                px = Decimal("1")
            else:
                ticker = ticker_map.get(f"{ccy}-USDT")
                if not ticker:
                    unpriced.append(ccy)
                    continue
                bid = _d(ticker.get("bidPx"), f"{ccy} bid", allow_zero=False)
                ask = _d(ticker.get("askPx"), f"{ccy} ask", allow_zero=False)
                bid_size = _d(ticker.get("bidSz"), f"{ccy} bid size", allow_zero=False)
                ts = int(_d(ticker.get("ts"), f"{ccy} ticker timestamp", allow_zero=False))
                if ask < bid or now_ms < ts or now_ms - ts > self.policy.max_snapshot_age_ms:
                    blockers.append(f"invalid_or_stale_ticker:{ccy}")
                    continue
                if bid_size < amount:
                    blockers.append(f"insufficient_top_bid_depth:{ccy}")
                    continue
                px = bid  # conservative liquidation mark, not last trade
            value = amount * px
            equity += value
            holdings.append({"currency": ccy, "quantity": str(amount), "mark_usdt": str(px),
                             "value_usdt": str(value), "available": str(available),
                             "frozen": str(frozen)})
        if unpriced:
            blockers.append("unpriced_assets:" + ",".join(sorted(unpriced)))

        realized = _signed_d(daily_realized_pnl_quote, "daily realized P&L")
        daily_unrealized = (
            _signed_d(daily_unrealized_pnl_quote, "daily unrealized P&L")
            if daily_unrealized_pnl_quote is not None else None
        )
        daily_account = (
            _signed_d(daily_account_pnl_quote, "daily account P&L")
            if daily_account_pnl_quote is not None else None
        )
        if require_daily_account_pnl and daily_account is None:
            blockers.append("daily_account_pnl_baseline_missing")
        if position_lock and daily_unrealized is None and daily_account is None:
            # Lifetime inventory P&L cannot be used as the current UTC day's P&L.
            # Until a durable daily opening mark exists, report incomplete and
            # block entry authorization rather than mixing incompatible periods.
            blockers.append("daily_unrealized_pnl_baseline_missing")
        position = None
        if position_lock:
            blockers.append("position_already_active")
            expected_algo_client_id = (protection_intent or {}).get("algo_client_order_id")
            expected_algo_id = (protection_intent or {}).get("exchange_algo_id")
            matches = [row for row in open_algo_orders
                       if row.get("instId") == position_lock.get("instrument") and row.get("ordType") == "oco"
                       and ((expected_algo_id and str(row.get("algoId")) == str(expected_algo_id))
                            or (expected_algo_client_id and row.get("algoClOrdId") == expected_algo_client_id))]
            if len(matches) != 1 or len(open_algo_orders) != 1:
                blockers.append("exchange_oco_pending_state_mismatch")
        elif open_algo_orders:
            blockers.append("exchange_open_algo_orders_present")
        unrealized = Decimal("0")
        risk_to_stop = Decimal("0")
        planned_risk = Decimal("0")
        planned_report = None
        if position_lock:
            instrument = str(position_lock.get("instrument") or "")
            if not position_context:
                blockers.append("position_restart_context_missing")
            else:
                if position_context.get("instrument") != instrument:
                    blockers.append("position_context_instrument_mismatch")
                if position_context.get("entry_status") not in {"filled", "canceled", "mmp_canceled"}:
                    blockers.append("entry_order_not_terminal")
                if position_context.get("entry_exchange_state") not in {"filled", "canceled", "mmp_canceled"}:
                    blockers.append("entry_exchange_snapshot_not_terminal")
                if not position_context.get("entry_order_id") or position_context.get("entry_snapshot_filled_size") in (None, "", "0"):
                    blockers.append("entry_fill_snapshot_missing")
                if int(position_context.get("unresolved_exit_intents", 0)) > 0:
                    blockers.append("unresolved_exit_intents")
                if int(position_context.get("unresolved_child_orders", 0)) > 0:
                    blockers.append("unresolved_oco_child_orders")
            basis = calculate_inventory_basis(position_fills or [], instrument)
            if position_context and position_context.get("entry_order_id"):
                entry_order_id = str(position_context["entry_order_id"])
                ledger_entry_size = sum((_d(row.get("fill_size"), "entry fill size", allow_zero=False)
                                         for row in (position_fills or [])
                                         if str(row.get("exchange_order_id")) == entry_order_id and row.get("side") == "buy"), Decimal("0"))
                snapshot_entry_size = _d(position_context.get("entry_snapshot_filled_size"),
                                         "entry snapshot filled size")
                if ledger_entry_size != snapshot_entry_size:
                    blockers.append("entry_fill_ledger_snapshot_mismatch")
            base_ccy = basis["base_currency"]
            exchange_qty = nonzero.get(base_ccy, Decimal("0"))
            tolerance = self.policy.balance_tolerance
            if abs(exchange_qty - basis["quantity"]) > max(tolerance, basis["quantity"] * tolerance):
                blockers.append("exchange_balance_ledger_mismatch")
            ticker = ticker_map.get(instrument)
            if not ticker:
                blockers.append("position_ticker_missing")
            else:
                bid = _d(ticker.get("bidPx"), "position bid", allow_zero=False)
                ask = _d(ticker.get("askPx"), "position ask", allow_zero=False)
                bid_size = _d(ticker.get("bidSz"), "position bid size", allow_zero=False)
                ts = int(_d(ticker.get("ts"), "position ticker timestamp", allow_zero=False))
                if ask < bid or now_ms < ts or now_ms - ts > self.policy.max_snapshot_age_ms:
                    blockers.append("position_ticker_stale_or_invalid")
                elif bid_size < basis["quantity"]:
                    blockers.append("insufficient_position_bid_depth")
                else:
                    market_value = basis["quantity"] * bid
                    exit_cost_rate = (self.policy.fee_bps + self.policy.slippage_bps) / Decimal("10000")
                    estimated_exit_cost = market_value * exit_cost_rate
                    unrealized = market_value - basis["remaining_cost_quote"] - estimated_exit_cost
                    if self.policy.max_position_notional_quote > 0 and market_value > self.policy.max_position_notional_quote:
                        blockers.append("position_notional_exceeds_limit")
                    if not protection_intent:
                        blockers.append("protective_stop_missing")
                        stop = None
                    else:
                        if protection_intent.get("status") != "active":
                            blockers.append("protective_order_not_active")
                        if protection_intent.get("instrument") not in (None, instrument):
                            blockers.append("protective_order_instrument_mismatch")
                        if protection_intent.get("entry_client_order_id") not in (None, position_lock.get("owner_client_order_id")):
                            blockers.append("protective_order_entry_mismatch")
                        stop = _d(protection_intent.get("stop_loss"), "stop loss", allow_zero=False)
                        stop_value = basis["quantity"] * stop
                        risk_to_stop = max(Decimal("0"), basis["remaining_cost_quote"] - stop_value) + stop_value * exit_cost_rate
                        if stop >= basis["average_entry_price"]:
                            blockers.append("stop_loss_not_below_average_entry")
                        if bid <= stop:
                            blockers.append("position_stop_trigger_may_be_breached")
                    position = {
                        "instrument": instrument, "quantity": str(basis["quantity"]),
                        "average_entry_price": str(basis["average_entry_price"]),
                        "remaining_cost_quote": str(basis["remaining_cost_quote"]),
                        "bid_mark": str(bid), "market_value_quote": str(market_value),
                        "unrealized_pnl_gross_quote": str(market_value - basis["remaining_cost_quote"]),
                        "estimated_exit_fee_and_slippage_quote": str(estimated_exit_cost),
                        "unrealized_pnl_net_quote": str(unrealized),
                        "stop_loss": str(stop) if stop is not None else None,
                        "risk_to_stop_quote": str(risk_to_stop),
                    }
            extra_assets = sorted(ccy for ccy, qty in nonzero.items() if ccy not in {"USDT", base_ccy} and qty > 0)
            if extra_assets:
                blockers.append("untracked_non_usdt_assets:" + ",".join(extra_assets))
        else:
            extra_assets = sorted(ccy for ccy, qty in nonzero.items() if ccy != "USDT" and qty > 0)
            if extra_assets:
                blockers.append("untracked_non_usdt_assets:" + ",".join(extra_assets))

        if planned_entry is None:
            blockers.append("planned_entry_not_provided")
        else:
            try:
                planned_inst = str(planned_entry.get("instrument") or "")
                planned_base, planned_quote = planned_inst.split("-", 1)
                entry_px = _d(planned_entry.get("entry_price"), "planned entry price", allow_zero=False)
                stop_px = _d(planned_entry.get("stop_loss"), "planned stop loss", allow_zero=False)
                take_px = _d(planned_entry.get("take_profit"), "planned take profit", allow_zero=False)
                planned_qty = _d(planned_entry.get("size"), "planned entry size", allow_zero=False)
                if not planned_base or planned_quote != "USDT" or not (stop_px < entry_px < take_px):
                    raise PortfolioError("Planned entry must be USDT Spot with stop < entry < target")
                planned_ticker = ticker_map.get(planned_inst)
                if not planned_ticker:
                    raise PortfolioError("Planned entry ticker is missing")
                ask_px = _d(planned_ticker.get("askPx"), "planned entry ask", allow_zero=False)
                bid_px = _d(planned_ticker.get("bidPx"), "planned entry bid", allow_zero=False)
                ask_size = _d(planned_ticker.get("askSz"), "planned entry ask size", allow_zero=False)
                ticker_ts = int(_d(planned_ticker.get("ts"), "planned entry ticker timestamp", allow_zero=False))
                if ask_px < bid_px or now_ms < ticker_ts or now_ms - ticker_ts > self.policy.max_snapshot_age_ms:
                    raise PortfolioError("Planned entry ticker is stale or invalid")
                spread_pct = (ask_px - bid_px) / bid_px * Decimal("100")
                if spread_pct > self.policy.max_spread_pct:
                    raise PortfolioError("Planned entry spread exceeds the current portfolio limit")
                # The candidate price was built from an earlier public ticker.
                # Bound its drift against the latest ask so an old limit price
                # cannot authorize an entry after the market has moved materially.
                max_acceptable_buy_price = ask_px * (
                    Decimal("1") + self.policy.slippage_bps / Decimal("10000")
                )
                if entry_px < ask_px or entry_px > max_acceptable_buy_price:
                    raise PortfolioError("Planned entry price is outside the current ask/slippage band")
                if ask_size < planned_qty:
                    raise PortfolioError("Planned entry exceeds top-of-book ask depth")
                entry_value = planned_qty * entry_px
                if self.policy.max_position_notional_quote > 0 and entry_value > self.policy.max_position_notional_quote:
                    blockers.append("planned_position_notional_exceeds_limit")
                stop_value = planned_qty * stop_px
                entry_cost = entry_value * (self.policy.fee_bps + self.policy.slippage_bps) / Decimal("10000")
                if entry_value + entry_cost > available_by_ccy.get("USDT", Decimal("0")):
                    blockers.append("insufficient_available_quote_balance")
                exit_cost = stop_value * (self.policy.fee_bps + self.policy.slippage_bps) / Decimal("10000")
                planned_risk = max(Decimal("0"), entry_value - stop_value) + entry_cost + exit_cost
                planned_report = {"instrument": planned_inst, "size": str(planned_qty),
                                  "entry_price": str(entry_px), "stop_loss": str(stop_px),
                                  "take_profit": str(take_px), "estimated_entry_fee_and_slippage_quote": str(entry_cost),
                                  "estimated_exit_fee_and_slippage_quote": str(exit_cost),
                                  "risk_to_stop_quote": str(planned_risk)}
            except (AttributeError, ValueError, InvalidOperation) as exc:
                blockers.append("planned_entry_invalid:" + type(exc).__name__)
        # Account equity delta already includes realized trades, fees, and marked inventory.
        # It is authoritative when present; never add the trade ledger a second time.
        combined_daily = (daily_account if daily_account is not None else
                          realized + (daily_unrealized if daily_unrealized is not None else Decimal("0")))
        if self.policy.max_daily_loss_quote > 0 and combined_daily <= -self.policy.max_daily_loss_quote:
            blockers.append("daily_loss_including_unrealized_limit_reached")
        projected_daily = combined_daily - planned_risk
        if self.policy.max_daily_loss_quote > 0 and projected_daily <= -self.policy.max_daily_loss_quote:
            blockers.append("planned_stop_risk_exceeds_remaining_daily_loss_budget")
        total_risk = risk_to_stop + planned_risk
        if self.policy.max_position_risk_quote > 0 and total_risk > self.policy.max_position_risk_quote:
            blockers.append("portfolio_risk_exceeds_limit")
        # Valuation completeness is distinct from entry eligibility. Policy
        # blockers can forbid a new entry without making account valuation incomplete.
        valuation_incomplete_prefixes = (
            "unpriced_assets", "invalid_or_stale_ticker", "insufficient_top_bid_depth",
            "insufficient_position_bid_depth", "position_ticker", "stale_balance",
            "exchange_balance", "position_restart", "position_context",
            "entry_order", "entry_exchange", "entry_fill", "unresolved_exit",
            "unresolved_oco", "local_unresolved_order_intents",
            "exchange_open_regular_orders", "exchange_oco_pending",
            "exchange_open_algo_orders", "untracked_non_usdt",
            "position_fill_sync_failed",
        )
        complete = not any(
            blocker.startswith(valuation_incomplete_prefixes) for blocker in blockers
        )
        return {
            "mode": "read_only_demo_portfolio_analysis", "orders_enabled": False,
            "order_submission_attempts": 0, "valuation_currency": "USDT",
            "account_equity_usdt": str(equity),
            "available_usdt": str(available_by_ccy.get("USDT", Decimal("0"))),
            "holdings": holdings,
            "position": position, "planned_entry": planned_report,
            "daily_realized_pnl_quote": str(realized),
            "daily_unrealized_pnl_quote": str(daily_unrealized) if daily_unrealized is not None else None,
            "daily_account_pnl_quote": str(daily_account) if daily_account is not None else None,
            "daily_unrealized_pnl_baseline_available": daily_unrealized is not None,
            "daily_combined_pnl_quote": str(combined_daily),
            "projected_daily_pnl_after_planned_stop_quote": str(projected_daily),
            "portfolio_risk_to_stop_quote": str(total_risk),
            "assumptions": {"price_mark": "best_bid_for_conservative_exit_valuation",
                            "fee_bps": str(self.policy.fee_bps),
                            "slippage_bps": str(self.policy.slippage_bps),
                            "combined_exit_cost_bps": str(self.policy.fee_bps + self.policy.slippage_bps)},
            "valuation_complete": complete, "entry_allowed": complete and not blockers,
            "blockers": sorted(set(blockers)),
        }
