"""Read-only orchestration from a market candidate to portfolio risk preflight."""

from .entry_plan import EntryPlanPolicy
from .risk_authorizer import CandidateRiskPreflight, PortfolioRiskAuthorizer
from .runtime import ReadOnlyRuntime, RuntimePolicy


def run_read_only_preflight(client, state_store, settings, *,
                            bar: str = "1H",
                            entry_plan_policy: EntryPlanPolicy,
                            clock_ms=None) -> dict:
    """Run one market scan and preflight only the highest-ranked buy candidate.

    This orchestration has no order manager and never invokes exchange writes.
    If no valid candidate exists, private account endpoints are not called.
    """
    if not isinstance(entry_plan_policy, EntryPlanPolicy):
        raise ValueError("an explicit EntryPlanPolicy is required for preflight")

    runtime = ReadOnlyRuntime(
        client,
        state_store,
        policy=RuntimePolicy(bar=bar, entry_plan_policy=entry_plan_policy),
    )
    analysis = runtime.run_once()
    candidate_signal = next(
        (
            signal for signal in analysis.get("signals", [])
            if signal.get("signal") == "buy"
            and isinstance(signal.get("candidate_entry_plan"), dict)
        ),
        None,
    )
    if candidate_signal is None:
        return {
            "mode": "read_only_candidate_risk_preflight",
            "status": "no_candidate",
            "reason": "no_valid_buy_candidate_from_current_market_scan",
            "orders_enabled": False,
            "order_submission_attempts": 0,
            "market_analysis": analysis,
            "risk_preflight": None,
        }

    # The provider is intentionally unused by preflight_candidate: the exact
    # immutable candidate object is passed directly to the read-only evaluator.
    bar_ms = {"1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}.get(bar)
    if bar_ms is None:
        raise ValueError("preflight supports only 1H, 4H, or 1D candles")

    authorizer = PortfolioRiskAuthorizer(
        client,
        state_store,
        settings,
        plan_provider=lambda **kwargs: None,
        bar_ms=bar_ms,
        clock_ms=clock_ms,
    )
    decision = CandidateRiskPreflight(authorizer, state_store).evaluate(
        candidate_signal["candidate_entry_plan"]
    )
    return {
        "mode": "read_only_candidate_risk_preflight",
        "status": decision["status"],
        "orders_enabled": False,
        "order_submission_attempts": 0,
        "selected_instrument": candidate_signal.get("instrument"),
        "market_analysis": analysis,
        "risk_preflight": decision,
    }
