"""Tests for local asset references in HTML responses.

After self-hosting fonts and vendor JS, the index page must reference
local paths (/static/...) and must NOT reference external CDNs.

Phase 5 adds PageSpeed optimization assertions: synchronous stylesheet
loading (post-CLS-revert guard), anti-FOUC relocation, font preload
scoping, and theme:changed event wiring.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import lxml.html


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


# The full path data for each icon, so a typo anywhere in a shape is caught and not
# just one near its start.
SUN_ICON = (
    "M12 3v1m0 16v1m8.66-13.66l-.71.71M4.05 4.05l-.71.71M21 12h-1M4"
    " 12H3m16.66 7.34l-.71-.71M4.05 19.95l-.71-.71M12 8a4 4 0 100 8"
    " 4 4 0 000-8z"
)

MOON_ICON = "M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"

# The two visibility rules the icons use, named for what each one does rather than for
# the icon that happens to carry it. Both come from Tailwind's own vocabulary, read
# through ``darkMode: 'class'``: ``dark:inline`` means "visible once ``.dark`` is set",
# and ``dark:hidden`` means the shape disappears there.
DARK_ONLY = frozenset({"hidden", "dark:inline"})
LIGHT_ONLY = frozenset({"dark:hidden"})


def _theme_document(page: str) -> lxml.html.HtmlElement:
    """Parse a rendered page.

    Cast rather than assert: lxml's html helpers carry no annotations, so mypy infers
    ``_Element`` from the ``etree.fromstring`` call inside them. What they return at
    runtime is an ``HtmlElement``, which is where ``get_element_by_id`` lives.
    """

    return cast("lxml.html.HtmlElement", lxml.html.document_fromstring(page))


def _theme_icons(document: lxml.html.HtmlElement) -> dict[frozenset[str], str]:
    """Map each theme icon's CSS classes to the path data it draws.

    Reads the page as a document, through lxml, instead of as text. The earlier version
    of this test re-derived the theme-to-icon mapping by matching the source of the
    inline script's ``if``/``else``, and three reviews each found a different defect in
    that: it pinned the script's layout, then it lost the ``else`` binding, then its
    function capture ran to the end of the document. The mapping is a property of the
    markup now, so there is no control flow left to parse.
    """

    svg = document.get_element_by_id("theme-toggle-icon")
    icons: dict[frozenset[str], str] = {}
    for node in svg.iter("path"):
        data = node.get("d")
        assert data, "a theme toggle icon carries no path data"
        icons[frozenset(node.get("class", "").split())] = data
    return icons


def test_theme_toggle_draws_the_mode_a_click_would_switch_to(client: Any) -> None:
    """GET / pairs each theme with the symbol of the other one.

    The button advertises the inverse action in two places at once: ``aria-label`` names
    the mode a click switches to, and the icon draws that mode's symbol. Both are read
    here from the default markup, which is what the browser paints before any script
    runs: light is the theme then, because the anti-FOUC script only adds the ``.dark``
    class when storage asks for it, so the label must offer dark mode and the visible
    icon must be the moon.

    Two assertions carry this, and neither is enough alone. The first pins the
    visibility contract: each path carries exactly one of the two rules, so neither can
    lose its rule and stay visible in both themes. The second pins the meaning the first
    cannot see, by asking which shape sits behind the rule for dark mode. An earlier
    version asserted only ``icons[SUN_CLASSES] == SUN_ICON``, with ``SUN_CLASSES`` named
    after the sun, which read the markup back to itself and passed while the two icons
    were inverted.
    """

    document = _theme_document(client.get("/").text)
    label = document.get_element_by_id("theme-toggle").get("aria-label")
    assert label == "Cambiar a modo oscuro", (
        "the markup must offer dark mode before any script runs"
    )

    icons = _theme_icons(document)
    assert set(icons) == {DARK_ONLY, LIGHT_ONLY}, (
        f"expected a dark-only and a light-only icon, got {sorted(map(sorted, icons))}"
    )
    assert icons[DARK_ONLY] == SUN_ICON, "dark mode must offer the sun"
    assert icons[LIGHT_ONLY] == MOON_ICON, "light mode must offer the moon"


def test_page_does_not_build_markup_at_runtime(client: Any) -> None:
    """GET / never assigns to ``innerHTML``.

    The toggle used to write its icon in from one of two literals in the script. That
    is a small detail with a large blast radius: a computed right-hand side is the
    shape injected content takes, and nothing else in these templates builds markup at
    runtime. ``hx-swap="innerHTML"`` is an attribute value rather than an assignment,
    so the pattern is anchored on the ``=``. A literal check for the forms these
    templates could use, not a taint analysis: it catches this pattern coming back, not
    every way markup can be built.
    """

    page = client.get("/").text
    assert not re.search(r"\.(?:inner|outer)HTML\s*=|insertAdjacentHTML", page), (
        "the page builds element markup at runtime"
    )


def test_tailwind_dark_mode_is_class_based() -> None:
    """``tailwind.config.js`` keeps ``darkMode: 'class'``.

    The ``dark:`` variants on the icons only mean anything with this setting. Under
    ``darkMode: 'media'`` Tailwind emits ``@media (prefers-color-scheme: dark)``
    instead, the button keeps flipping a class nothing reads, and the icons stop
    following the toggle without anything else on the page looking broken.
    """

    config = (Path(__file__).resolve().parents[1] / "tailwind.config.js").read_text()
    assert re.search(r"darkMode\s*:\s*['\"]class['\"]", config), (
        "dark: variants need darkMode: 'class' to follow the .dark class"
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
