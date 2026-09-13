"""Tests for :mod:`app.config` URL validation."""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.config import Settings


def _set_env(key: str, value: str | None) -> str | None:
    """Set ``key`` to ``value`` and return the previous value."""

    previous = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    return previous


def _make_settings(**env: str) -> Settings:
    """Build a Settings instance with the given env overrides applied."""

    previous: dict[str, str | None] = {}
    for key, value in env.items():
        previous[key] = _set_env(key, value)
    try:
        # Pydantic reads the values from os.environ at runtime; the
        # constructor signature is satisfied by those environment reads.
        return Settings()  # type: ignore[call-arg]
    finally:
        for key, previous_value in previous.items():
            _set_env(key, previous_value)


@pytest.fixture
def base_env(monkeypatch: Any) -> None:
    """Common valid env values for config tests."""

    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv(
        "BCU_API_URL",
        "https://cotizaciones.bcu.gub.uy/wscotizaciones/servlet/awsbcucotizaciones",
    )


def test_https_source_a_url_is_accepted(base_env, monkeypatch: Any) -> None:
    monkeypatch.setenv(
        "SOURCE_A_BASE_URL",
        "https://comprasestatales.gub.uy/comprasenlinea/jboss/generarReporte",
    )
    settings = _make_settings()
    assert settings.source_a_base_url.startswith("https://")


def test_http_source_a_url_is_rejected_by_default(base_env, monkeypatch: Any) -> None:
    monkeypatch.setenv(
        "SOURCE_A_BASE_URL",
        "http://comprasestatales.gub.uy/comprasenlinea/jboss/generarReporte",
    )
    monkeypatch.delenv("ALLOW_HTTP_SOURCE_URL", raising=False)
    with pytest.raises(ValueError, match="URL must use"):
        _make_settings()


def test_http_source_a_url_allowed_when_flag_set(base_env, monkeypatch: Any) -> None:
    monkeypatch.setenv(
        "SOURCE_A_BASE_URL",
        "http://comprasestatales.gub.uy/comprasenlinea/jboss/generarReporte",
    )
    monkeypatch.setenv("ALLOW_HTTP_SOURCE_URL", "true")
    settings = _make_settings()
    assert settings.source_a_base_url.startswith("http://")


def test_bcu_url_must_be_https(base_env, monkeypatch: Any) -> None:
    monkeypatch.setenv(
        "BCU_API_URL",
        "http://cotizaciones.bcu.gub.uy/wscotizaciones/servlet/awsbcucotizaciones",
    )
    with pytest.raises(ValueError, match="URL must use"):
        _make_settings()


def test_disallowed_source_host_is_rejected(base_env, monkeypatch: Any) -> None:
    monkeypatch.setenv(
        "SOURCE_A_BASE_URL",
        "https://evil.example.com/comprasenlinea/jboss/generarReporte",
    )
    with pytest.raises(ValueError, match="is not allowed"):
        _make_settings()


@pytest.mark.parametrize(
    "ttl,valid",
    [("0", True), ("300", True), ("900", True), ("299", False), ("901", False)],
)
def test_cache_ttl_accepts_only_disabled_or_five_to_fifteen_minutes(
    base_env, monkeypatch: Any, ttl: str, valid: bool
) -> None:
    monkeypatch.setenv("CACHE_TTL_SECONDS", ttl)

    if valid:
        assert _make_settings().cache_ttl_seconds == int(ttl)
    else:
        with pytest.raises(ValueError):
            _make_settings()


def test_cache_max_entries_requires_at_least_one(base_env, monkeypatch: Any) -> None:
    monkeypatch.setenv("CACHE_MAX_ENTRIES", "0")
    with pytest.raises(ValueError):
        _make_settings()


# ── IndexNow key ──────────────────────────────────────────────────────────


def test_indexnow_key_defaults_to_none(base_env, monkeypatch: Any) -> None:
    """The feature is off unless a deployment opts in."""

    monkeypatch.delenv("INDEXNOW_KEY", raising=False)

    assert _make_settings().indexnow_key is None


def test_indexnow_key_blank_means_unset(base_env, monkeypatch: Any) -> None:
    """A declared-but-empty variable disables the feature instead of half-enabling it.

    Keeping the variable visible in a compose file or an env template while
    leaving it empty is common; that must not serve an empty key file that could
    never verify ownership.
    """

    for blank in ("", "   "):
        monkeypatch.setenv("INDEXNOW_KEY", blank)

        assert _make_settings().indexnow_key is None


def test_indexnow_key_is_stripped(base_env, monkeypatch: Any) -> None:
    """Surrounding whitespace does not reach the served file or the filename."""

    monkeypatch.setenv("INDEXNOW_KEY", "  0123456789abcdef  ")

    assert _make_settings().indexnow_key == "0123456789abcdef"


def test_indexnow_key_rejects_values_the_route_could_not_serve(
    base_env, monkeypatch: Any
) -> None:
    """The key doubles as a filename, so its shape is validated at startup.

    Anything outside [A-Za-z0-9-] would produce a path the key route cannot serve,
    and a key shorter than 8 characters is below what IndexNow accepts.
    """

    for invalid in ("short", "has spaces here", "bad!chars", "x" * 129):
        monkeypatch.setenv("INDEXNOW_KEY", invalid)

        with pytest.raises(ValueError):
            _make_settings()
