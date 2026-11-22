"""Tests de lecture de la configuration."""

import settings


def test_positive_int_falls_back_to_default_for_zero(monkeypatch):
    """0 signifiait "illimite" dans les anciennes configs: jamais un plafond a 1."""
    monkeypatch.setenv("OEIL_TEST_LIMIT", "0")
    assert settings._get_positive_int("OEIL_TEST_LIMIT", 100_000) == 100_000


def test_positive_int_rejects_negative_and_invalid(monkeypatch):
    monkeypatch.setenv("OEIL_TEST_LIMIT", "-5")
    assert settings._get_positive_int("OEIL_TEST_LIMIT", 42) == 42
    monkeypatch.setenv("OEIL_TEST_LIMIT", "abc")
    assert settings._get_positive_int("OEIL_TEST_LIMIT", 42) == 42


def test_positive_int_keeps_explicit_value(monkeypatch):
    monkeypatch.setenv("OEIL_TEST_LIMIT", "250")
    assert settings._get_positive_int("OEIL_TEST_LIMIT", 42) == 250


def test_analyzer_limits_are_never_one_by_default():
    assert settings.ANALYZER_MAX_EMAIL_ENTRIES > 1
    assert settings.ANALYZER_MAX_PHONE_ENTRIES > 1
    assert settings.ANALYZER_MAX_DOMAIN_ENTRIES > 1
