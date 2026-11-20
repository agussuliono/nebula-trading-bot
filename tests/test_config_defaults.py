from nebula.config import Settings


def test_demo_order_gate_defaults_off():
    settings = Settings()
    assert settings.enable_demo_orders is False


def test_emergency_stop_defaults_on_in_settings():
    assert Settings().kill_switch is True


def test_emergency_stop_defaults_on_when_environment_variable_is_missing(monkeypatch):
    monkeypatch.delenv("NEBULA_KILL_SWITCH", raising=False)
    assert Settings.from_env().kill_switch is True


def test_emergency_stop_can_only_be_cleared_by_explicit_false(monkeypatch):
    monkeypatch.setenv("NEBULA_KILL_SWITCH", "false")
    assert Settings.from_env().kill_switch is False



def test_demo_account_exclusivity_defaults_false():
    assert Settings().demo_account_exclusive is False


def test_demo_account_exclusivity_requires_explicit_environment_confirmation(monkeypatch):
    monkeypatch.delenv("NEBULA_DEMO_ACCOUNT_EXCLUSIVE", raising=False)
    assert Settings.from_env().demo_account_exclusive is False

    monkeypatch.setenv("NEBULA_DEMO_ACCOUNT_EXCLUSIVE", "true")
    assert Settings.from_env().demo_account_exclusive is True
