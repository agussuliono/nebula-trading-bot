import pytest

from nebula.cli import main


def test_top_level_help_exits_successfully_and_formats_percent(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["nebula", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "70 percent" in output
    assert "30 percent" in output
    assert "diagnose-portfolio" in output
    assert "preflight-once" in output



def test_candidate_plan_cli_requires_both_explicit_limits(monkeypatch):
    monkeypatch.setattr("sys.argv", [
        "nebula", "analyze-once", "--plan-risk-quote", "5",
    ])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_candidate_plan_cli_options_are_limited_to_read_only_analysis(monkeypatch):
    monkeypatch.setattr("sys.argv", [
        "nebula", "check-demo-public", "--plan-risk-quote", "5",
        "--plan-max-notional-quote", "100",
    ])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2



def test_preflight_once_requires_explicit_candidate_limits(monkeypatch):
    monkeypatch.setattr("sys.argv", ["nebula", "preflight-once"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_preflight_once_accepts_paired_limits_and_invokes_read_only_orchestrator(
    monkeypatch, tmp_path
):
    from nebula.config import Settings

    calls = {}
    settings = Settings()
    monkeypatch.setattr(
        "sys.argv",
        [
            "nebula", "preflight-once",
            "--plan-risk-quote", "5",
            "--plan-max-notional-quote", "100",
            "--bar", "1H",
            "--db", str(tmp_path / "cli.sqlite3"),
        ],
    )
    monkeypatch.setattr("nebula.cli.Settings.from_env", lambda: settings)
    monkeypatch.setattr("nebula.cli.OkxDemoClient", lambda configured: object())
    monkeypatch.setattr("nebula.cli.StateStore", lambda path: "state")

    def run_preflight(client, state, configured, *, bar, entry_plan_policy):
        calls.update({
            "client": client,
            "state": state,
            "settings": configured,
            "bar": bar,
            "risk_budget": entry_plan_policy.risk_budget_quote,
            "max_notional": entry_plan_policy.max_notional_quote,
        })
        return {
            "mode": "read_only_candidate_risk_preflight",
            "status": "no_candidate",
            "orders_enabled": False,
            "order_submission_attempts": 0,
        }

    monkeypatch.setattr("nebula.preflight.run_read_only_preflight", run_preflight)
    main()

    assert calls["state"] == "state"
    assert calls["settings"] is settings
    assert calls["bar"] == "1H"
    from decimal import Decimal
    assert Decimal(str(calls["risk_budget"])) == Decimal("5")
    assert Decimal(str(calls["max_notional"])) == Decimal("100")
