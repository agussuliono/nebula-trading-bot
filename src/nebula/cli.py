import argparse
import json
import os

from .config import Settings
from .exchange import OkxDemoClient
from .runtime import ReadOnlyRuntime, RuntimePolicy, result_json
from .state import StateStore
from .output import emit_result
from .portfolio import PortfolioPolicy, PortfolioRiskEngine


_EXECUTION_PAYLOAD_KEYS = {
    "candidate", "entry", "scan", "reconciliation",
    "entry_reconciliation", "protection",
}
_SENSITIVE_OUTPUT_KEYS = {
    "size", "sz", "price", "px", "notional", "order_id", "ord_id",
    "client_order_id", "cl_ord_id", "balance", "equity", "pnl",
    "realized_pnl", "fee", "amount", "qty", "quantity",
}


def _redact_execution_report(value):
    """Strip order-plan and account detail before a report reaches logs."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key in _EXECUTION_PAYLOAD_KEYS:
                summary = {"present": item is not None}
                if isinstance(item, dict) and isinstance(item.get("status"), str):
                    summary["status"] = item["status"]
                redacted[key] = summary
            elif key in _SENSITIVE_OUTPUT_KEYS:
                continue
            else:
                redacted[key] = _redact_execution_report(item)
        return redacted
    if isinstance(value, list):
        return [_redact_execution_report(item) for item in value]
    return value


def _outcome_observation_summary(state):
    from .outcome_observer import observe_all

    result = observe_all(state)
    recorded = result.get("recorded_events") or []
    finalized = sum(
        1 for event in recorded
        if isinstance(event, dict) and event.get("event_type") == "accounting_finalized"
    )
    return {
        "recorded_events": len(recorded),
        "accounting_finalized": finalized,
        "legacy_episode_unavailable": len(result.get("legacy_episode_unavailable") or []),
    }


def _operator_breaker(state):
    from .risk_controls import CircuitBreaker

    raw_limit = os.getenv("NEBULA_MAX_DAILY_LOSS_QUOTE", "0")
    try:
        limit = float(raw_limit)
    except (TypeError, ValueError):
        limit = 0.0
    # The daily-loss limit is unused by engage/status/clear; a positive
    # placeholder keeps the fail-closed state validation in CircuitBreaker.
    return CircuitBreaker(state, limit if limit > 0 else "1")


def _cycle_gap_report(state):
    """List hourly candle slots with no completed runtime cycle."""
    processed = []
    for event in state.list_events(limit=1000):
        if event.get("event_type") != "runtime_cycle_completed":
            continue
        candle_ts = (event.get("details") or {}).get("latest_candle_ts")
        if isinstance(candle_ts, int) and candle_ts > 0:
            processed.append(candle_ts)
    processed = sorted(set(processed))
    if not processed:
        return {"status": "no_processed_cycles", "processed_cycles": 0, "gaps": []}
    step_ms = 3_600_000
    present = set(processed)
    gaps = [
        slot for slot in range(processed[0], processed[-1] + step_ms, step_ms)
        if slot not in present
    ]
    return {
        "status": "cycle_gaps_detected" if gaps else "no_cycle_gaps",
        "processed_cycles": len(processed),
        "first_candle_ts": processed[0],
        "last_candle_ts": processed[-1],
        "gap_count": len(gaps),
        "gaps": gaps,
    }


def _emergency_status(state):
    with state._connect() as db:
        row = db.execute(
            "SELECT engaged,reason,updated_at FROM emergency_stop WHERE singleton=1"
        ).fetchone()
    if row is None:
        raise ValueError("Emergency stop state is missing; manual recovery required")
    return {
        "status": "emergency_stop_engaged" if row[0] else "emergency_stop_clear",
        "engaged": bool(row[0]),
        "reason": row[1],
        "updated_at": row[2],
    }


def main():
    parser = argparse.ArgumentParser(prog="nebula")
    parser.add_argument("command", choices=["check-demo-public", "verify-demo-once", "analyze-once", "preflight-once", "portfolio-once", "recover-once", "orchestrate-once", "emergency-stop", "emergency-status", "emergency-clear", "observe-outcomes", "backup-state", "restore-state", "cycle-gaps", "backtest", "portfolio-backtest", "diagnose-portfolio", "download-history", "collect-prospective", "audit-data"])
    parser.add_argument("--reason", default="", help="Operator reason recorded with an emergency stop")
    parser.add_argument("--confirm", default="", help="Exact confirmation token for guarded operator commands")
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
    parser.add_argument("--execute-demo", action="store_true",
                        help="Explicitly request one guarded OKX Spot Demo execution cycle (never live)")
    args = parser.parse_args()
    if args.execute_demo and args.command != "orchestrate-once":
        parser.error("--execute-demo is only valid with orchestrate-once")
    if (args.plan_risk_quote is None) != (args.plan_max_notional_quote is None):
        parser.error("--plan-risk-quote and --plan-max-notional-quote must be supplied together")
    if args.command not in {"analyze-once", "preflight-once"} and (args.plan_risk_quote is not None or args.plan_max_notional_quote is not None):
        parser.error("candidate-plan sizing options are only valid with analyze-once or preflight-once")
    if args.command == "preflight-once" and args.plan_risk_quote is None:
        parser.error("preflight-once requires --plan-risk-quote and --plan-max-notional-quote")
    if args.command in {"emergency-stop", "emergency-status", "emergency-clear", "observe-outcomes", "backup-state", "restore-state", "cycle-gaps"}:
        # Operator controls act on local state only: no credentials, no network.
        state = StateStore(args.db)
        if args.command == "observe-outcomes":
            emit_result(json.dumps(_outcome_observation_summary(state), indent=2, sort_keys=True))
            return
        if args.command == "cycle-gaps":
            emit_result(json.dumps(_cycle_gap_report(state), indent=2, sort_keys=True))
            return
        if args.command == "backup-state":
            emit_result(json.dumps(state.backup_to(args.output), indent=2, sort_keys=True))
            return
        if args.command == "restore-state":
            if not args.data:
                parser.error("restore-state requires --data PATH")
            if args.confirm != "restore-state":
                parser.error("restore-state requires --confirm restore-state")
            emit_result(json.dumps(state.restore_from(args.data), indent=2, sort_keys=True))
            return
        breaker = _operator_breaker(state)
        if args.command == "emergency-stop":
            breaker.engage_emergency_stop(args.reason or "operator emergency stop")
            emit_result(json.dumps(_emergency_status(state), indent=2, sort_keys=True))
            return
        if args.command == "emergency-status":
            emit_result(json.dumps(_emergency_status(state), indent=2, sort_keys=True))
            return
        if args.confirm != "clear-emergency-stop":
            parser.error("emergency-clear requires --confirm clear-emergency-stop")
        breaker.clear_emergency_stop(explicit_confirmation=True)
        emit_result(json.dumps(_emergency_status(state), indent=2, sort_keys=True))
        return
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
    if args.command == "check-demo-public":
        # This path must not inspect API key/secret/passphrase environment variables.
        settings = Settings.from_public_env()
        client = OkxDemoClient(settings)
        instruments = client.get_instruments()
        tickers = client.get_tickers()
        emit_result(f"OK: {len(instruments)} spot instruments; {len(tickers)} tickers (public market data). No orders sent.")
        return

    # Public-only commands must not inspect API credentials. The default
    # orchestrator mode is shadow-only; load credentials only for an explicit
    # --execute-demo request.
    if args.command == "analyze-once" or (
        args.command == "orchestrate-once" and not args.execute_demo
    ):
        settings = Settings.from_public_env()
    else:
        settings = Settings.from_env()
    client = OkxDemoClient(settings)
    if args.command == "verify-demo-once":
        from .demo_verification import verify_demo_read_only
        report = verify_demo_read_only(client, settings)
        emit_result(json.dumps(report, indent=2, sort_keys=True))
        if report.get("status") != "read_only_demo_connection_verified":
            raise SystemExit(1)
        return
    if args.command == "orchestrate-once":
        from .orchestrator import ExecutionOrchestrator
        state = StateStore(args.db)
        orchestrator = ExecutionOrchestrator(
            client, state, settings, bar=args.bar,
        )
        report = orchestrator.run_once(execute=args.execute_demo)
        try:
            report["outcome_observation"] = _outcome_observation_summary(state)
        except Exception as exc:
            report["outcome_observation"] = {
                "status": "observation_failed", "error_type": type(exc).__name__,
            }
        emit_result(json.dumps(
            _redact_execution_report(report), indent=2, sort_keys=True, default=str,
        ))
        return
    if args.command == "preflight-once":
        from .entry_plan import EntryPlanPolicy
        from .preflight import run_read_only_preflight
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
        report = run_read_only_preflight(
            client, state, settings, bar=args.bar,
            entry_plan_policy=plan_policy,
        )
        emit_result(json.dumps(report, indent=2, sort_keys=True))
        if report.get("status") not in {"approved", "no_candidate"}:
            raise SystemExit(1)
        return
    if args.command == "recover-once":
        # Authenticated read-only startup recovery. This path never creates an
        # order manager with submission authority and never calls write endpoints.
        settings.validate_private_read_permission()
        from .startup import StartupRecovery
        state = StateStore(args.db)
        report = StartupRecovery(client, state).run()
        emit_result(json.dumps(report, indent=2, sort_keys=True))
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
