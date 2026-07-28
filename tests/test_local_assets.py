"""Tests for local asset references in HTML responses.

After self-hosting fonts and vendor JS, the index page must reference
local paths (/static/...) and must NOT reference external CDNs.
"""

from __future__ import annotations

from typing import Any


def test_index_references_local_htmx(client: Any) -> None:
    """GET / contains a reference to the local htmx vendor file."""

    response = client.get("/")
    assert response.status_code == 200
    assert "/static/vendor/htmx.min.js" in response.text


def test_index_references_local_chartjs(client: Any) -> None:
    """GET / contains a reference to the local Chart.js vendor file."""

    response = client.get("/")
    assert response.status_code == 200
    assert "/static/vendor/chart.umd.js" in response.text


def test_index_does_not_reference_google_fonts(client: Any) -> None:
    """GET / does NOT contain fonts.googleapis.com links."""

    response = client.get("/")
    assert "fonts.googleapis.com" not in response.text


def test_index_does_not_reference_jsdelivr(client: Any) -> None:
    """GET / does NOT contain cdn.jsdelivr.net links."""

    response = client.get("/")
    assert "cdn.jsdelivr.net" not in response.text


def test_index_references_local_fonts_css(client: Any) -> None:
    """GET / references the local fonts.css before style.css."""

    response = client.get("/")
    text = response.text
    fonts_pos = text.find("/static/css/fonts.css")
    style_pos = text.find("/static/css/style.css")
    assert fonts_pos != -1, "fonts.css not found in HTML"
    assert style_pos != -1, "style.css not found in HTML"
    assert fonts_pos < style_pos, "fonts.css must appear before style.css"
