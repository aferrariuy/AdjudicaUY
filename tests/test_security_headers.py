"""Tests for Phase 3: Security headers middleware.

Verifies that the middleware adds X-Content-Type-Options, X-Frame-Options
on every response, and conditionally adds HSTS based on debug mode.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _make_client_with_debug(debug: bool, db_session: Any) -> TestClient:
    """Create a TestClient with settings.debug overridden to ``debug``."""

    from app.database import get_db
    from app.main import create_app

    app = create_app()

    # Override get_db to use the test session.
    def _override_get_db():  # type: ignore[no-untyped-def]
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    # Patch get_settings to return a Settings with the desired debug value.
    import os

    from app.config import Settings

    old_debug = os.environ.get("DEBUG")
    os.environ["DEBUG"] = "true" if debug else "false"
    try:
        fresh_settings = Settings()  # type: ignore[call-arg]
    finally:
        if old_debug is None:
            os.environ.pop("DEBUG", None)
        else:
            os.environ["DEBUG"] = old_debug

    with patch("app.main.get_settings", return_value=fresh_settings):
        # Re-create app so middleware picks up the patched settings.
        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        return TestClient(app)


# ---------------------------------------------------------------------------
# X-Content-Type-Options
# ---------------------------------------------------------------------------


def test_x_content_type_options_present(db_session: Any) -> None:
    """Every response includes X-Content-Type-Options: nosniff."""

    client = _make_client_with_debug(debug=False, db_session=db_session)
    response = client.get("/healthz")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    client.close()


def test_x_content_type_options_on_index(db_session: Any) -> None:
    """X-Content-Type-Options is present on HTML responses too."""

    client = _make_client_with_debug(debug=False, db_session=db_session)
    response = client.get("/")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    client.close()


# ---------------------------------------------------------------------------
# X-Frame-Options
# ---------------------------------------------------------------------------


def test_x_frame_options_present(db_session: Any) -> None:
    """Every response includes X-Frame-Options: DENY."""

    client = _make_client_with_debug(debug=False, db_session=db_session)
    response = client.get("/healthz")
    assert response.headers.get("X-Frame-Options") == "DENY"
    client.close()


# ---------------------------------------------------------------------------
# HSTS — production mode (debug=False)
# ---------------------------------------------------------------------------


def test_hsts_present_in_production(db_session: Any) -> None:
    """HSTS header is present when debug=False."""

    client = _make_client_with_debug(debug=False, db_session=db_session)
    response = client.get("/healthz")
    hsts = response.headers.get("Strict-Transport-Security")
    assert hsts is not None, "HSTS header must be present when debug=False"
    assert "max-age=" in hsts
    assert "includeSubDomains" in hsts
    client.close()


def test_hsts_max_age_at_least_one_year(db_session: Any) -> None:
    """HSTS max-age is at least 31536000 (one year)."""

    client = _make_client_with_debug(debug=False, db_session=db_session)
    response = client.get("/healthz")
    hsts = response.headers.get("Strict-Transport-Security", "")
    # Extract max-age value.
    for part in hsts.split(";"):
        part = part.strip()
        if part.startswith("max-age="):
            max_age = int(part.split("=")[1])
            assert max_age >= 31536000
            break
    else:
        pytest.fail("max-age directive not found in HSTS header")
    client.close()


# ---------------------------------------------------------------------------
# HSTS — debug mode (debug=True)
# ---------------------------------------------------------------------------


def test_hsts_absent_in_debug_mode(db_session: Any) -> None:
    """HSTS header is NOT present when debug=True."""

    client = _make_client_with_debug(debug=True, db_session=db_session)
    response = client.get("/healthz")
    hsts = response.headers.get("Strict-Transport-Security")
    assert hsts is None, "HSTS header must be absent when debug=True"
    client.close()


def test_security_headers_still_present_in_debug(db_session: Any) -> None:
    """Non-HSTS security headers are present even in debug mode."""

    client = _make_client_with_debug(debug=True, db_session=db_session)
    response = client.get("/healthz")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    client.close()
