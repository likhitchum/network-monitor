"""Tests for validate_config(): every production guard rule, driven by monkeypatched
module constants (they are read from the environment once at import time)."""

import pytest

import app.main as m

VALID = {
    "APP_ENV": "production",
    "SECRET": "x" * 32,
    "ADMIN_PASSWORD": "strong-admin-pass-1",
    "USER_PASSWORD": "strong-user-pass-1",
    "ALLOWED_ORIGINS": ["https://monitor.example.com"],
}


def configure(monkeypatch, **overrides):
    values = {**VALID, **overrides}
    for name, value in values.items():
        monkeypatch.setattr(m, name, value)


def test_non_production_env_skips_all_rules(monkeypatch):
    """Even the most insecure values must pass outside production."""
    configure(
        monkeypatch,
        APP_ENV="development",
        SECRET="dev-only-change-me",
        ADMIN_PASSWORD="admin123",
        USER_PASSWORD="user123",
        ALLOWED_ORIGINS=["*"],
    )
    assert m.validate_config() is None

    monkeypatch.setattr(m, "APP_ENV", "test")
    assert m.validate_config() is None


def test_production_with_valid_config_passes(monkeypatch):
    configure(monkeypatch)
    assert m.validate_config() is None


def test_production_rejects_insecure_secret_from_blacklist(monkeypatch):
    configure(monkeypatch, SECRET="dev-only-change-me")
    with pytest.raises(RuntimeError, match="APP_SECRET"):
        m.validate_config()


def test_production_rejects_short_secret(monkeypatch):
    configure(monkeypatch, SECRET="x" * 31)
    with pytest.raises(RuntimeError, match="APP_SECRET"):
        m.validate_config()


def test_production_accepts_secret_of_exactly_32_chars(monkeypatch):
    configure(monkeypatch, SECRET="x" * 32)
    m.validate_config()  # must not raise


def test_production_rejects_weak_admin_password(monkeypatch):
    configure(monkeypatch, ADMIN_PASSWORD="admin123")
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        m.validate_config()


def test_production_rejects_short_admin_password(monkeypatch):
    configure(monkeypatch, ADMIN_PASSWORD="short-pass1")  # 11 chars
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        m.validate_config()


def test_production_rejects_weak_user_password(monkeypatch):
    configure(monkeypatch, USER_PASSWORD="user123")
    with pytest.raises(RuntimeError, match="USER_PASSWORD"):
        m.validate_config()


def test_production_rejects_short_user_password(monkeypatch):
    configure(monkeypatch, USER_PASSWORD="user-pass-1")  # 11 chars
    with pytest.raises(RuntimeError, match="USER_PASSWORD"):
        m.validate_config()


def test_production_accepts_passwords_of_exactly_12_chars(monkeypatch):
    configure(monkeypatch, ADMIN_PASSWORD="a" * 12, USER_PASSWORD="u" * 12)
    m.validate_config()  # must not raise


def test_production_rejects_wildcard_cors_origin(monkeypatch):
    configure(monkeypatch, ALLOWED_ORIGINS=["https://good.example.com", "*"])
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        m.validate_config()


def test_production_rejects_empty_cors_origins(monkeypatch):
    configure(monkeypatch, ALLOWED_ORIGINS=[])
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        m.validate_config()


def test_secret_rule_is_checked_first(monkeypatch):
    """When several rules are broken, the APP_SECRET error wins (documented order)."""
    configure(
        monkeypatch,
        SECRET="short",
        ADMIN_PASSWORD="admin123",
        USER_PASSWORD="user123",
        ALLOWED_ORIGINS=["*"],
    )
    with pytest.raises(RuntimeError, match="APP_SECRET"):
        m.validate_config()
