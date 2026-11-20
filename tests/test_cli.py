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
