"""Tests for local asset references in HTML responses.

After self-hosting fonts and vendor JS, the index page must reference
local paths (/static/...) and must NOT reference external CDNs.

Phase 5 adds PageSpeed optimization assertions: critical CSS inlined,
async stylesheet loading, anti-FOUC relocation, font preload scoping,
and theme:changed event wiring.
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


def test_base_inlines_critical_css(client: Any) -> None:
    """GET / has an inline <style> block in <head> with critical CSS."""

    response = client.get("/")
    text = response.text
    # Extract <head> content
    head_match = re.search(r"<head>(.*?)</head>", text, re.DOTALL)
    assert head_match, "<head> not found"
    head = head_match.group(1)
    # Must contain an inline <style> block with box-sizing and font-family
    style_match = re.search(r"<style>(.*?)</style>", head, re.DOTALL)
    assert style_match, "Inline <style> block not found in <head>"
    style_content = style_match.group(1)
    assert "box-sizing" in style_content, "Critical CSS missing box-sizing reset"
    assert "font-family" in style_content, "Critical CSS missing font-family"


def test_fonts_css_async_loaded(client: Any) -> None:
    """GET / loads fonts.css via preload+onload, not render-blocking stylesheet."""

    response = client.get("/")
    text = response.text
    # fonts.css should appear as rel="preload" with as="style"
    assert 'rel="preload"' in text
    assert 'href="/static/css/fonts.css"' in text
    assert 'as="style"' in text
    # fonts.css should NOT appear as a render-blocking stylesheet
    # (the noscript fallback is fine — strip noscript blocks before checking)
    without_noscript = re.sub(
        r"<noscript>.*?</noscript>", "", text, flags=re.DOTALL
    )
    assert 'rel="stylesheet" href="/static/css/fonts.css"' not in without_noscript


def test_style_css_async_loaded(client: Any) -> None:
    """GET / loads style.css via preload+onload, not render-blocking stylesheet."""

    response = client.get("/")
    text = response.text
    assert 'href="/static/css/style.css"' in text
    assert 'as="style"' in text
    # style.css should NOT appear as a render-blocking stylesheet
    without_noscript = re.sub(
        r"<noscript>.*?</noscript>", "", text, flags=re.DOTALL
    )
    assert 'rel="stylesheet" href="/static/css/style.css"' not in without_noscript


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
    font_preloads = re.findall(
        r'<link[^>]*rel="preload"[^>]*as="font"[^>]*>', text
    )
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
    assert "new Event('theme:changed')" in text or 'new Event("theme:changed")' in text, (
        "Theme toggle should dispatch a theme:changed event"
    )
