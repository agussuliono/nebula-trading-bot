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


def test_public_settings_loader_does_not_read_credentials_or_order_flags(monkeypatch):
    import os

    original_getenv = os.getenv
    forbidden = {"OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE",
                 "NEBULA_ENABLE_DEMO_ORDERS", "NEBULA_KILL_SWITCH"}

    def guarded_getenv(name, default=None):
        if name in forbidden:
            raise AssertionError(f"public settings attempted to read {name}")
        return original_getenv(name, default)

    monkeypatch.setattr("nebula.config.os.getenv", guarded_getenv)
    settings = Settings.from_public_env()
    assert settings.api_key == ""
    assert settings.secret_key == ""
    assert settings.passphrase == ""
    assert settings.enable_demo_orders is False
    assert settings.kill_switch is True
