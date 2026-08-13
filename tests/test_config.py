"""Tests for SEO-related Settings fields: ``site_url`` and ``debug``.

These fields are introduced by the SEO optimization change to support
canonical URL generation and conditional HSTS headers.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.config import Settings, trusted_host_allowlist


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


# ---------------------------------------------------------------------------
# trusted_host_allowlist
# ---------------------------------------------------------------------------


def test_trusted_host_allowlist_includes_canonical_hostname(
    base_env: Any, monkeypatch: Any
) -> None:
    """The canonical SITE_URL hostname is always allowlisted."""

    monkeypatch.setenv("SITE_URL", "https://adjudica.digitales.gub.uy")
    settings = _make_settings()
    allowed = trusted_host_allowlist(settings)
    assert "adjudica.digitales.gub.uy" in allowed


def test_trusted_host_allowlist_always_includes_local_aliases(
    base_env: Any, monkeypatch: Any
) -> None:
    """localhost and the loopback aliases are always present."""

    monkeypatch.setenv("SITE_URL", "https://adjudica.digitales.gub.uy")
    settings = _make_settings()
    allowed = trusted_host_allowlist(settings)
    assert {"localhost", "127.0.0.1", "::1"} <= set(allowed)


def test_trusted_host_allowlist_excludes_test_hosts_outside_pytest(
    base_env: Any, monkeypatch: Any
) -> None:
    """example.test/testserver are NOT allowlisted outside a pytest run."""

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("SITE_URL", "https://adjudica.digitales.gub.uy")
    settings = _make_settings()
    allowed = trusted_host_allowlist(settings)
    assert "example.test" not in allowed
    assert "testserver" not in allowed


def test_trusted_host_allowlist_includes_test_hosts_under_pytest(
    base_env: Any, monkeypatch: Any
) -> None:
    """An active pytest context allowlists example.test and testserver."""

    monkeypatch.setenv("SITE_URL", "https://adjudica.digitales.gub.uy")
    settings = _make_settings()
    allowed = trusted_host_allowlist(settings)
    assert "example.test" in allowed
    assert "testserver" in allowed


def test_trusted_host_allowlist_is_sorted(base_env: Any, monkeypatch: Any) -> None:
    """The allowlist is returned sorted for deterministic middleware config."""

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("SITE_URL", "https://zzz.example")
    settings = _make_settings()
    allowed = trusted_host_allowlist(settings)
    assert allowed == sorted(allowed)
    # With a site hostname that sorts last, the loopback alias comes first.
    assert allowed[0] == "127.0.0.1"


def test_trusted_host_allowlist_rejects_hostless_site_url(
    base_env: Any, monkeypatch: Any
) -> None:
    """A SITE_URL without a hostname raises ValueError."""

    monkeypatch.setenv("SITE_URL", "https:///path-only")
    settings = _make_settings()
    with pytest.raises(ValueError, match="SITE_URL must include a hostname"):
        trusted_host_allowlist(settings)
