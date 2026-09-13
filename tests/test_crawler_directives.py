"""Tests for the crawler directives on fragment and download endpoints.

The HTMX ``/partial`` responses, ``/adjudications`` (the results fragment
that duplicates ``/``), and the two CSV exports are not pages: they carry
no ``<head>``, no canonical, and no meta robots. Left crawlable they double
the crawler-visible URL surface of the site — roughly 28.7k organism and
company pages become ~57.5k URLs — and split the crawl budget of a young
site across duplicate fragments.

The contract asserted here:

* every fragment/download path returns ``X-Robots-Tag: noindex``;
* every real page keeps the header absent (no accidental de-indexing);
* the directive is ``noindex`` alone, never ``noindex, nofollow``, so link
  discovery from those fragments still works.
"""

from __future__ import annotations

from typing import Any

import pytest

# ``app.main`` is imported lazily inside the predicate tests below: the
# module builds the application at import time, and ``Settings`` only
# accepts the ``example.test`` hosts from ``_TEST_ENV`` while a test is
# actually running (``PYTEST_CURRENT_TEST``), not during collection.

NOINDEX = "noindex"

# Seeded by the ``seed`` fixture below.
ORGANISM_PATH = "/organism/Ministerio%20de%20Interior"
COMPANY_PATH = "/company/RUT/210000000001"


@pytest.fixture
def seed(make_adjudication: Any) -> None:
    """Persist one adjudication so the profile routes resolve to 200."""

    make_adjudication()


# ── Non-indexable: fragments and downloads ─────────────────────────────


NON_INDEXABLE_PATHS = [
    "/adjudications",
    "/adjudications?page=3",
    "/adjudications?page=2&partial=table",
    "/adjudications/export",
    f"{ORGANISM_PATH}/partial",
    f"{COMPANY_PATH}/partial",
    f"{COMPANY_PATH}/export",
]


@pytest.mark.usefixtures("seed")
@pytest.mark.parametrize("path", NON_INDEXABLE_PATHS)
def test_fragment_and_download_paths_are_noindex(client: Any, path: str) -> None:
    """Fragments and CSV exports carry ``X-Robots-Tag: noindex``."""

    response = client.get(path)
    assert response.status_code == 200
    assert response.headers.get("X-Robots-Tag") == NOINDEX


@pytest.mark.usefixtures("seed")
def test_noindex_directive_keeps_link_crawling(client: Any) -> None:
    """The directive is ``noindex`` alone — never ``noindex, nofollow``.

    ``nofollow`` would cut the internal link paths (organism partial →
    companies) that Google uses to discover the real pages.
    """

    response = client.get(f"{ORGANISM_PATH}/partial")
    directive = response.headers.get("X-Robots-Tag", "")
    assert directive == NOINDEX
    assert "nofollow" not in directive


@pytest.mark.usefixtures("seed")
def test_partial_header_survives_a_redirect(client: Any) -> None:
    """A trailing slash still ends up noindexed after the redirect."""

    response = client.get(f"{ORGANISM_PATH}/partial/", follow_redirects=True)
    assert response.status_code == 200
    assert response.headers.get("X-Robots-Tag") == NOINDEX


# ── Indexable: real pages must stay untouched ──────────────────────────


INDEXABLE_PATHS = [
    "/",
    "/about",
    ORGANISM_PATH,
    COMPANY_PATH,
    "/robots.txt",
    "/sitemap.xml",
    "/healthz",
    "/static/css/fonts.css",
]


@pytest.mark.usefixtures("seed")
@pytest.mark.parametrize("path", INDEXABLE_PATHS)
def test_real_pages_have_no_noindex_directive(client: Any, path: str) -> None:
    """Every real page is left indexable (the header is absent)."""

    response = client.get(path)
    assert response.status_code == 200
    assert "X-Robots-Tag" not in response.headers


# ── The predicate itself ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/adjudications",
        "/adjudications/",
        "/adjudications/export",
        "/organism/BPS/partial",
        "/organism/BPS/partial/",
        "/company/RUT/1/partial",
        "/company/RUT/1/export",
    ],
)
def test_predicate_matches_fragments(path: str) -> None:
    from app.main import is_non_indexable_path

    assert is_non_indexable_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/about",
        "/organism/BPS",
        "/company/RUT/1",
        # An entity whose *name* contains "partial"/"export" is a real page:
        # the suffix must be a path segment, not a substring.
        "/organism/Ministerio%20de%20Export",
        "/organism/Partial",
    ],
)
def test_predicate_leaves_real_pages_alone(path: str) -> None:
    from app.main import is_non_indexable_path

    assert is_non_indexable_path(path) is False
