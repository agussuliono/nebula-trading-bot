"""Live-snapshot portfolio authorization for Demo Spot entries.

This module authorizes only an already-sized order whose plan includes a closed
buy signal and precomputed stop/target. It never submits an order.
"""
from decimal import Decimal

from .portfolio import PortfolioPolicy, PortfolioRiskEngine
from .risk_controls import CircuitBreaker


class RiskAuthorizationError(RuntimeError):
    pass


class PortfolioRiskAuthorizer:
    """Fail-closed adapter between order sizing and the read-only portfolio engine."""

    def __init__(self, client, state_store, settings, *, plan_provider, clock_ms=None,
                 bar_ms=3_600_000, max_candle_age_intervals=2):
        if not callable(plan_provider):
            raise ValueError("plan_provider must provide an explicit stop/target plan")
        self.client = client
        self.state = state_store
        self.settings = settings
        if isinstance(bar_ms, bool) or not isinstance(bar_ms, int) or bar_ms <= 0:
            raise ValueError("bar_ms must be a positive integer")
        if (isinstance(max_candle_age_intervals, bool)
                or not isinstance(max_candle_age_intervals, int)
                or max_candle_age_intervals < 1):
            raise ValueError("max_candle_age_intervals must be a positive integer")
        if hasattr(client, "settings") and client.settings != settings:
            raise ValueError("authorizer settings must exactly match the exchange client settings")
        self.plan_provider = plan_provider
        self.clock_ms = clock_ms or (lambda: __import__("time").time_ns() // 1_000_000)
        self.bar_ms = bar_ms
        self.max_candle_age_intervals = max_candle_age_intervals

    def __call__(self, *, instrument: str, requested_size: str,
                 requested_price: str, candle_ts: int) -> dict:
        try:
            self.settings.validate_order_permission()
            plan = self.plan_provider(
                instrument=instrument, size=requested_size,
                entry_price=requested_price, candle_ts=candle_ts,
            )
            if not isinstance(plan, dict):
                raise RiskAuthorizationError("Entry plan provider returned no plan")
            plan_candle_ts = plan.get("candle_ts")
            if (plan.get("signal_action") != "buy"
                    or plan.get("candle_closed") is not True
                    or isinstance(plan_candle_ts, bool)
                    or not isinstance(plan_candle_ts, int)
                    or plan_candle_ts <= 0
                    or plan_candle_ts != int(candle_ts)):
                raise RiskAuthorizationError("Entry requires a matching confirmed closed-candle buy signal")
            now_ms = int(self.clock_ms())
            candle_close_ts = plan_candle_ts + self.bar_ms
            if (candle_close_ts > now_ms
                    or now_ms - candle_close_ts > self.bar_ms * self.max_candle_age_intervals):
                raise RiskAuthorizationError("Closed-candle signal is stale or its candle is not complete")
            planned = {
                "instrument": instrument,
                "size": str(requested_size),
                "entry_price": str(requested_price),
                "stop_loss": str(plan["stop_loss"]),
                "take_profit": str(plan["take_profit"]),
            }
            lock = self.state.get_position_lock()
            unresolved = self.state.list_unresolved_intents()
            account_config = self.client.get_account_config()
            balance = self.client.get_balance()
            tickers = self.client.get_tickers()
            regular = self.client.get_pending_orders("SPOT")
            oco = self.client.get_pending_algo_orders("oco")
            conditional = self.client.get_pending_algo_orders("conditional")
            breaker = CircuitBreaker(self.state, self.settings.max_daily_loss_quote)
            daily_realized = breaker.status()["daily_realized_pnl_quote"]
            policy = PortfolioPolicy(
                max_snapshot_age_ms=self.settings.market_data_max_age_ms,
                fee_bps=Decimal(str(self.settings.fee_bps)),
                slippage_bps=Decimal(str(self.settings.slippage_bps)),
                max_daily_loss_quote=Decimal(str(self.settings.max_daily_loss_quote)),
                max_position_risk_quote=Decimal(str(self.settings.max_risk_quote)),
                max_position_notional_quote=Decimal(str(self.settings.max_position_quote)),
            )
            report = PortfolioRiskEngine(policy).evaluate(
                account_config=account_config, balance=balance, tickers=tickers,
                position_lock=lock, local_unresolved_intents=unresolved,
                open_regular_orders=regular, open_algo_orders=[*oco, *conditional],
                planned_entry=planned, now_ms=now_ms,
                daily_realized_pnl_quote=daily_realized,
            )
            if report.get("valuation_complete") is not True or report.get("entry_allowed") is not True:
                raise RiskAuthorizationError(
                    "Portfolio snapshot denied entry: " + ",".join(report.get("blockers", []))
                )
            approved = report.get("planned_entry")
            if not isinstance(approved, dict):
                raise RiskAuthorizationError("Portfolio engine did not produce a priced risk plan")
            return {
                "entry_allowed": True,
                "planned_entry": {
                    "instrument": approved["instrument"],
                    "size": approved["size"],
                    "entry_price": approved["entry_price"],
                    "stop_loss": approved["stop_loss"],
                    "take_profit": approved["take_profit"],
                    "risk_to_stop_quote": approved["risk_to_stop_quote"],
                },
                "portfolio_snapshot": {
                    "account_equity_usdt": report["account_equity_usdt"],
                    "available_usdt": report["available_usdt"],
                    "projected_daily_pnl_after_planned_stop_quote":
                        report["projected_daily_pnl_after_planned_stop_quote"],
                },
            }
        except RiskAuthorizationError:
            raise
        except Exception as exc:
            raise RiskAuthorizationError("Portfolio risk authorization failed closed") from exc
