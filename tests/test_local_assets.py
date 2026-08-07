"""Tests for local asset references in HTML responses.

After self-hosting fonts and vendor JS, the index page must reference
local paths (/static/...) and must NOT reference external CDNs.

Phase 5 adds PageSpeed optimization assertions: synchronous stylesheet
loading (post-CLS-revert guard), anti-FOUC relocation, font preload
scoping, and theme:changed event wiring.
"""

from __future__ import annotations

import re
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


# ── Phase 5: PageSpeed optimization assertions ─────────────────────────
# Async CSS loading with inline critical CSS was reverted (f3f6cb0):
# two-phase rendering caused CLS of 0.928 on desktop / 0.6 on mobile.
# Synchronous loading is the intentional state — these tests guard it.


def test_stylesheets_loaded_synchronously(client: Any) -> None:
    """GET / loads fonts.css and style.css as render-blocking stylesheets.

    Regression guard for the CLS revert: reintroducing the preload+onload
    async pattern (rel="preload" as="style") must fail this test.
    """

    response = client.get("/")
    text = response.text
    assert 'rel="stylesheet" href="/static/css/fonts.css"' in text
    assert 'rel="stylesheet" href="/static/css/style.css"' in text
    # No preload+onload async CSS pattern
    assert 'rel="preload" href="/static/css/fonts.css"' not in text
    assert 'rel="preload" href="/static/css/style.css"' not in text


def test_no_critical_css_inlined(client: Any) -> None:
    """GET / does not inline critical CSS (box-sizing reset) in <head>.

    Critical CSS inlining belonged to the reverted async-loading approach
    (two-phase rendering). The only inline <style> allowed is the HTMX
    transition helper.
    """

    response = client.get("/")
    text = response.text
    head_match = re.search(r"<head>(.*?)</head>", text, re.DOTALL)
    assert head_match, "<head> not found"
    head = head_match.group(1)
    style_matches = re.findall(r"<style[^>]*>(.*?)</style>", head, re.DOTALL)
    assert style_matches, "Inline <style> block not found in <head>"
    for style_content in style_matches:
        assert "box-sizing" not in style_content, (
            "Critical CSS should not be inlined (caused CLS when async)"
        )
        assert "font-family" not in style_content, (
            "Critical CSS should not be inlined (caused CLS when async)"
        )


def test_anti_fouc_in_body(client: Any) -> None:
    """GET / has the anti-FOUC localStorage script in <body>, not <head>."""

    response = client.get("/")
    text = response.text
    head_match = re.search(r"<head>(.*?)</head>", text, re.DOTALL)
    body_match = re.search(r"<body[^>]*>(.*?)</body>", text, re.DOTALL)
    assert head_match and body_match, "Missing <head> or <body>"
    head = head_match.group(1)
    body = body_match.group(1)
    # The localStorage theme check should be in <body>, not <head>
    assert "localStorage.getItem('theme')" not in head, (
        "Anti-FOUC script should not be in <head>"
    )
    assert "localStorage.getItem('theme')" in body, (
        "Anti-FOUC script should be in <body>"
    )


def test_only_ibmplexsans_preloaded(client: Any) -> None:
    """GET / preloads only IBMPlexSans-400.woff2 as font."""

    response = client.get("/")
    text = response.text
    # Find all font preload links
    font_preloads = re.findall(r'<link[^>]*rel="preload"[^>]*as="font"[^>]*>', text)
    assert len(font_preloads) == 1, (
        f"Expected exactly 1 font preload, found {len(font_preloads)}: {font_preloads}"
    )
    assert "IBMPlexSans-400.woff2" in font_preloads[0], (
        "The single font preload should be IBMPlexSans-400.woff2"
    )


def test_bigshoulders_not_preloaded(client: Any) -> None:
    """GET / does NOT preload BigShouldersDisplay fonts."""

    response = client.get("/")
    text = response.text
    # Find all preload links
    preload_links = re.findall(r'<link[^>]*rel="preload"[^>]*>', text)
    for link in preload_links:
        assert "BigShouldersDisplay" not in link, (
            f"BigShouldersDisplay should not be preloaded: {link}"
        )


def test_scripts_block_present(client: Any) -> None:
    """GET / has a scripts block with chart wiring (theme:changed reference)."""

    response = client.get("/")
    text = response.text
    # The scripts block should contain theme:changed listener
    assert "theme:changed" in text, (
        "theme:changed event reference should be present in the page"
    )
    # And the Chart.js loader should be in the page (from child template)
    assert "window.__loadChartJS" in text, (
        "Chart.js loader (window.__loadChartJS) should be present"
    )


def test_theme_changed_event_dispatched(client: Any) -> None:
    """GET / has theme:changed dispatch in the theme toggle handler."""

    response = client.get("/")
    text = response.text
    # The toggle function should dispatch theme:changed
    assert "dispatchEvent" in text
    assert "theme:changed" in text
    # Verify it's in the toggle context (not just the listener)
    assert (
        "new Event('theme:changed')" in text or 'new Event("theme:changed")' in text
    ), "Theme toggle should dispatch a theme:changed event"
