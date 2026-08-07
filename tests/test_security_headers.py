"""Tests for Phase 3: Security headers middleware.

Verifies that the middleware adds X-Content-Type-Options, X-Frame-Options
on every response, and conditionally adds HSTS based on debug mode.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
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


def test_html_responses_include_exact_nonce_csp_and_inline_coverage(
    client: TestClient,
) -> None:
    paths = ["/", "/about", "/organism/SECURITY-ORG", "/company/RUT/42"]
    executable_blocks: set[tuple[str, str]] = set()

    for path in paths:
        response = client.get(path)
        policy = response.headers["content-security-policy"]
        nonce = policy.split("'nonce-", 1)[1].split("'", 1)[0]
        expected = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            f"style-src 'self' 'nonce-{nonce}'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; upgrade-insecure-requests"
        )
        assert policy == expected

        soup = BeautifulSoup(response.text, "html.parser")
        for style in soup.find_all("style"):
            assert style.get("nonce") == nonce
            executable_blocks.add(("style", style.get_text()))
        for script in soup.find_all("script"):
            if script.get("type") == "application/ld+json":
                assert script.get("nonce") is None
                continue
            if script.get("src"):
                continue
            assert script.get("nonce") == nonce
            executable_blocks.add(("script", script.get_text()))

    # The index and organism chart blocks intentionally share identical source
    # today, so the six template blocks produce five unique rendered bodies.
    assert len(executable_blocks) == 5


def test_csp_nonce_varies_and_exists_on_error_html(client: TestClient) -> None:
    first = client.get("/")
    second = client.get("/")
    not_found = client.get("/missing-security-page")
    invalid = client.get("/?date_from=not-a-date")

    def nonce(response: Any) -> str:
        policy = response.headers["content-security-policy"]
        return str(policy.split("'nonce-", 1)[1].split("'", 1)[0])

    assert nonce(first) != nonce(second)
    assert not_found.status_code == 404
    assert invalid.status_code == 422
    assert nonce(not_found)
    assert nonce(invalid)


@pytest.mark.parametrize(
    "path",
    [
        "/adjudications",
        "/organism/SECURITY-ORG/partial",
        "/company/RUT/42/partial",
    ],
)
def test_rendered_partials_have_no_inline_scripts(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    soup = BeautifulSoup(response.text, "html.parser")

    inline_scripts = [
        script
        for script in soup.find_all("script")
        if not script.get("src") and script.get("type") != "application/ld+json"
    ]
    assert inline_scripts == []
