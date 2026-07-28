"""Tests for SEO-related Settings fields: ``site_url`` and ``debug``.

These fields are introduced by the SEO optimization change to support
canonical URL generation and conditional HSTS headers.
"""

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
    monkeypatch.setenv(
        "SOURCE_A_BASE_URL",
        "https://comprasestatales.gub.uy/comprasenlinea/jboss/generarReporte",
    )


# ---------------------------------------------------------------------------
# site_url
# ---------------------------------------------------------------------------


def test_site_url_defaults_to_localhost(base_env: Any, monkeypatch: Any) -> None:
    """site_url defaults to http://localhost:8000 when SITE_URL is unset."""

    monkeypatch.delenv("SITE_URL", raising=False)
    settings = _make_settings()
    assert settings.site_url == "http://localhost:8000"


def test_site_url_reads_from_env(base_env: Any, monkeypatch: Any) -> None:
    """SITE_URL env var overrides the default."""

    monkeypatch.setenv("SITE_URL", "https://adjudica.digitales.gub.uy")
    settings = _make_settings()
    assert settings.site_url == "https://adjudica.digitales.gub.uy"


# ---------------------------------------------------------------------------
# debug
# ---------------------------------------------------------------------------


def test_debug_defaults_to_false(base_env: Any) -> None:
    """debug defaults to False when DEBUG is unset."""

    _set_env("DEBUG", None)
    try:
        settings = _make_settings()
    finally:
        pass
    assert settings.debug is False


def test_debug_reads_true_from_env(base_env: Any, monkeypatch: Any) -> None:
    """DEBUG=true env var sets debug to True."""

    monkeypatch.setenv("DEBUG", "true")
    settings = _make_settings()
    assert settings.debug is True


def test_debug_reads_false_from_env(base_env: Any, monkeypatch: Any) -> None:
    """DEBUG=false env var sets debug to False (explicit)."""

    monkeypatch.setenv("DEBUG", "false")
    settings = _make_settings()
    assert settings.debug is False
