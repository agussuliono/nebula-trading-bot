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
