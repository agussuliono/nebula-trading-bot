import argparse
import json
import os

from .config import Settings
from .exchange import OkxDemoClient
from .runtime import ReadOnlyRuntime, RuntimePolicy, result_json
from .state import StateStore
from .output import emit_result
from .portfolio import PortfolioPolicy, PortfolioRiskEngine


def main():
    parser = argparse.ArgumentParser(prog="nebula")
    parser.add_argument("command", choices=["check-demo-public", "analyze-once", "portfolio-once", "backtest", "portfolio-backtest", "diagnose-portfolio", "download-history", "collect-prospective", "audit-data"])
    parser.add_argument("--db", default=os.getenv("NEBULA_DB_PATH", "nebula.sqlite3"))
    parser.add_argument("--bar", default="1H")
    parser.add_argument("--data", help="Local ascending OHLCV CSV for offline backtest or dataset audit")
    parser.add_argument("--symbol", help="USDT Spot symbol for public history download, e.g. BTC-USDT")
    parser.add_argument("--candles", type=int, default=2000, help="Number of confirmed historical candles to fetch (60..100000)")
    parser.add_argument("--output", default="data/market.csv", help="Output CSV path for history download")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace an existing dataset and manifest")
    parser.add_argument("--asset", action="append", default=[], metavar="SYMBOL=CSV", help="Portfolio backtest input; repeat for each symbol")
    parser.add_argument("--walk-forward", action="store_true", help="Run chronological out-of-sample folds")
    parser.add_argument("--stress", action="store_true", help="Run transaction-cost stress scenarios")
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--sensitivity", action="store_true", help="Run exploratory EMA sensitivity on the first 70 percent only; reserve final 30 percent")
    parser.add_argument("--trades-output", help="Optional CSV export path for diagnostic trade records")
    parser.add_argument("--initial-equity", type=float, default=1000.0)
    parser.add_argument("--risk-pct", type=float, default=0.01)
    parser.add_argument("--max-position-pct", type=float, default=0.25)
    parser.add_argument("--stop-pct", type=float, default=0.02)
    parser.add_argument("--target-pct", type=float, default=0.04)
    parser.add_argument("--fee-bps", type=float, default=10.0)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument("--daily-loss-pct", type=float, default=0.03)
    parser.add_argument("--max-drawdown-halt-pct", type=float, default=0.20)
    parser.add_argument("--fast-ema", type=int, default=20)
    parser.add_argument("--slow-ema", type=int, default=50)
    parser.add_argument("--plan-risk-quote", type=float,
                        help="Explicit quote-currency risk budget for read-only candidate plans")
    parser.add_argument("--plan-max-notional-quote", type=float,
                        help="Explicit quote-currency notional cap for read-only candidate plans")
    args = parser.parse_args()
    if args.command == "download-history":
        if not args.symbol:
            parser.error("download-history requires --symbol BTC-USDT")
        from .history import fetch_history, save_dataset
        client = OkxDemoClient(Settings())
        candles, meta = fetch_history(client, args.symbol, args.bar, args.candles)
        manifest = save_dataset(args.output, args.symbol.strip().upper(), args.bar, candles, meta, overwrite=args.overwrite)
        emit_result(json.dumps({"csv": args.output, "manifest": args.output + ".manifest.json",
                          "dataset": manifest, "orders_submitted": 0}, indent=2, sort_keys=True))
        return
    if args.command == "collect-prospective":
        if not args.symbol:
            parser.error("collect-prospective requires --symbol BTC-USDT")
        if args.bar != "1H":
            parser.error("The frozen prospective research protocol currently supports 1H only")
        from .backtest import Candle, load_csv
        from .prospective import append_prospective_candles
        # Settings() has no credentials; this command uses only the public candles endpoint.
        client = OkxDemoClient(Settings())
        existing = load_csv(args.output)
        rows = client.get_candles(args.symbol.strip().upper(), bar=args.bar, limit=300)
        parsed = [Candle.from_okx(row) for row in rows]
        fresh = sorted((c for c in parsed if c.confirmed and c.ts > existing[-1].ts),
                       key=lambda c: c.ts)
        if not fresh:
            emit_result(json.dumps({"symbol": args.symbol.strip().upper(), "bar": args.bar,
                                    "appended": 0, "reason": "no_new_confirmed_candles",
                                    "orders_submitted": 0, "private_api_calls": 0}, indent=2))
            return
        report = append_prospective_candles(args.output, args.symbol, args.bar, fresh)
        emit_result(json.dumps(report, indent=2, sort_keys=True))
        return
    if args.command == "audit-data":
        if not args.data:
            parser.error("audit-data requires --data PATH")
        from .backtest import load_csv
        from .history import audit_dataset
        emit_result(json.dumps(audit_dataset(load_csv(args.data), args.bar), indent=2, sort_keys=True))
        return
    if args.command in {"backtest", "portfolio-backtest", "diagnose-portfolio"}:
        if args.command == "backtest" and not args.data:
            parser.error("backtest requires --data PATH")
        if args.command in {"portfolio-backtest", "diagnose-portfolio"} and len(args.asset) < 2:
            parser.error("portfolio command requires at least two --asset SYMBOL=CSV arguments")
        if args.walk_forward and args.stress:
            parser.error("choose either --walk-forward or --stress for one run")
        from .backtest import (BacktestConfig, Backtester, PortfolioBacktester, load_csv, walk_forward, stress_test, portfolio_walk_forward, portfolio_stress_test)
        candles = load_csv(args.data) if args.data else None
        markets = {}
        if args.command in {"portfolio-backtest", "diagnose-portfolio"}:
            for item in args.asset:
                if "=" not in item:
                    parser.error("Each --asset must use SYMBOL=CSV")
                symbol, path = item.split("=", 1)
                if not symbol.strip() or not path.strip() or symbol.strip().upper() in markets:
                    parser.error("Invalid or duplicate --asset symbol/path")
                markets[symbol.strip().upper()] = load_csv(path.strip())
        config = BacktestConfig(initial_equity=args.initial_equity,
            risk_per_trade_pct=args.risk_pct, max_position_pct=args.max_position_pct,
            stop_loss_pct=args.stop_pct, take_profit_pct=args.target_pct,
            fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
            max_daily_loss_pct=args.daily_loss_pct,
            max_drawdown_halt_pct=args.max_drawdown_halt_pct,
            fast_ema=args.fast_ema, slow_ema=args.slow_ema)
        if args.command in {"portfolio-backtest", "diagnose-portfolio"}:
            if args.command == "diagnose-portfolio" and (args.walk_forward or args.stress):
                parser.error("diagnose-portfolio cannot be combined with --walk-forward or --stress")
            if args.walk_forward and args.stress:
                parser.error("choose either --walk-forward or --stress for one run")
            if args.command == "diagnose-portfolio":
                from .diagnostics import diagnose_portfolio, export_trade_records
                base_result = PortfolioBacktester(config).run(markets)
                report = diagnose_portfolio(markets, base_result, config, sensitivity=args.sensitivity)
                if args.trades_output:
                    export_trade_records(args.trades_output, report["trade_records"])
                    report["trade_csv"] = args.trades_output
            elif args.walk_forward:
                report = portfolio_walk_forward(markets, config=config, folds=args.folds)
            elif args.stress:
                report = portfolio_stress_test(markets, config=config)
            else:
                report = PortfolioBacktester(config).run(markets)
        elif args.walk_forward:
            report = walk_forward(candles, config=config, folds=args.folds)
        elif args.stress:
            report = stress_test(candles, config=config)
        else:
            report = Backtester(config).run(candles)
        emit_result(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return
    settings = Settings.from_env()
    client = OkxDemoClient(settings)
    if args.command == "check-demo-public":
        instruments = client.get_instruments()
        tickers = client.get_tickers()
        emit_result(f"OK: {len(instruments)} spot instruments; {len(tickers)} tickers (public market data). No orders sent.")
        return
    if args.command == "portfolio-once":
        # Read-only private account endpoints plus public tickers. No order manager
        # is instantiated and no order-writing endpoint is reachable from this path.
        from decimal import Decimal
        import time
        state = StateStore(args.db)
        account_config = client.get_account_config()
        balance = client.get_balance()
        tickers = client.get_tickers()
        pending_regular = client.get_pending_orders("SPOT")
        pending_oco = client.get_pending_algo_orders("oco")
        pending_conditional = client.get_pending_algo_orders("conditional")
        lock = state.get_position_lock()
        fill_sync_error = None
        if lock:
            from .protection_manager import DemoProtectionManager
            try:
                DemoProtectionManager(client, state).sync_entry_fills(lock["owner_client_order_id"])
            except Exception as exc:
                fill_sync_error = type(exc).__name__
                state.record_event("portfolio_entry_fill_sync_failed",
                                   lock["owner_client_order_id"],
                                   {"error_type": fill_sync_error})
        local_unresolved = state.list_unresolved_intents()
        context = state.get_position_context(lock["owner_client_order_id"]) if lock else None
        fills = state.get_position_fills(lock["owner_client_order_id"]) if lock else []
        protection = state.get_protection_intent(lock["owner_client_order_id"]) if lock else None
        policy = PortfolioPolicy(
            max_snapshot_age_ms=settings.market_data_max_age_ms,
            fee_bps=Decimal(str(settings.fee_bps)),
            slippage_bps=Decimal(str(settings.slippage_bps)),
            max_daily_loss_quote=Decimal(str(settings.max_daily_loss_quote)),
            max_position_risk_quote=Decimal(str(settings.max_risk_quote)),
            max_position_notional_quote=Decimal(str(settings.max_position_quote)),
        )
        breaker = None
        if settings.max_daily_loss_quote > 0:
            from .risk_controls import CircuitBreaker
            breaker = CircuitBreaker(state, settings.max_daily_loss_quote)
        daily_realized = breaker.status()["daily_realized_pnl_quote"] if breaker else "0"
        report = PortfolioRiskEngine(policy).evaluate(
            account_config=account_config, balance=balance, tickers=tickers,
            position_lock=lock, position_fills=fills, position_context=context,
            protection_intent=protection, local_unresolved_intents=local_unresolved,
            position_fill_sync_error=fill_sync_error,
            open_regular_orders=pending_regular, open_algo_orders=[*pending_oco, *pending_conditional],
            now_ms=int(time.time() * 1000), daily_realized_pnl_quote=daily_realized,
        )
        state.record_event("portfolio_snapshot_evaluated", lock.get("owner_client_order_id") if lock else None,
                           {"valuation_complete": report["valuation_complete"],
                            "entry_allowed": report["entry_allowed"],
                            "blockers": report["blockers"],
                            "account_equity_usdt": report["account_equity_usdt"]})
        emit_result(json.dumps(report, indent=2, sort_keys=True))
        return
    # Deliberately public-data-only. This command never creates an order manager,
    # never reads private balances, and cannot submit or cancel exchange orders.
    if (args.plan_risk_quote is None) != (args.plan_max_notional_quote is None):
        parser.error("--plan-risk-quote and --plan-max-notional-quote must be supplied together")
    if args.command != "analyze-once" and (args.plan_risk_quote is not None or args.plan_max_notional_quote is not None):
        parser.error("candidate-plan sizing options are only valid with analyze-once")
    plan_policy = None
    if args.plan_risk_quote is not None:
        from .entry_plan import EntryPlanPolicy
        try:
            plan_policy = EntryPlanPolicy(
                risk_budget_quote=args.plan_risk_quote,
                max_notional_quote=args.plan_max_notional_quote,
                fee_bps=settings.fee_bps,
                slippage_bps=settings.slippage_bps,
            )
        except ValueError as exc:
            parser.error(f"invalid candidate-plan sizing policy: {exc}")
    state = StateStore(args.db)
    policy = RuntimePolicy(bar=args.bar, entry_plan_policy=plan_policy)
    runtime = ReadOnlyRuntime(client, state, policy=policy)
    emit_result(result_json(runtime.run_once()))


if __name__ == "__main__":
    main()
