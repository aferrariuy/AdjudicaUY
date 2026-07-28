"""Tests for Cache-Control middleware on static assets.

Static files under /static/ should have aggressive cache headers
(public, max-age=31536000, immutable) since they are content-hashed.
Non-static routes (/, /healthz) must NOT carry these headers.
"""

from __future__ import annotations

from typing import Any


EXPECTED_CACHE_HEADER = "public, max-age=31536000, immutable"


def test_static_css_has_cache_control(client: Any) -> None:
    """GET /static/css/style.css returns Cache-Control with immutable."""

    response = client.get("/static/css/style.css")
    assert response.status_code == 200
    cache_control = response.headers.get("Cache-Control", "")
    assert "public" in cache_control
    assert "max-age=31536000" in cache_control
    assert "immutable" in cache_control


def test_index_does_not_have_static_cache_control(client: Any) -> None:
    """GET / does NOT get the static asset Cache-Control header."""

    response = client.get("/")
    cache_control = response.headers.get("Cache-Control", "")
    assert "immutable" not in cache_control
    assert "max-age=31536000" not in cache_control


def test_healthz_does_not_have_static_cache_control(client: Any) -> None:
    """GET /healthz does NOT get the static asset Cache-Control header."""

    response = client.get("/healthz")
    cache_control = response.headers.get("Cache-Control", "")
    assert "immutable" not in cache_control
    assert "max-age=31536000" not in cache_control
