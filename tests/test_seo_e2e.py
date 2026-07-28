"""Phase 4: E2E verification of the full SEO pipeline.

These tests exercise the complete request → route → template → rendered
HTML path. They parse the response with BeautifulSoup to verify that
meta tags, OG tags, canonical URLs, JSON-LD, pagination hrefs, and
security headers are all present and correct in the final output.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(html: str) -> BeautifulSoup:
    """Parse an HTML string into a BeautifulSoup tree."""

    return BeautifulSoup(html, "html.parser")


def _meta_content(soup: BeautifulSoup, *, name: str | None = None, property: str | None = None) -> str | None:
    """Return the ``content`` attribute of a ``<meta>`` tag matched by name or property."""

    if name is not None:
        tag = soup.find("meta", attrs={"name": name})
    elif property is not None:
        tag = soup.find("meta", attrs={"property": property})
    else:
        return None
    return tag["content"] if tag and tag.has_attr("content") else None


# ---------------------------------------------------------------------------
# 4.1 — Index page (/) SEO tags
# ---------------------------------------------------------------------------


class TestIndexSEO:
    """E2E: GET / → full HTML with all SEO meta tags."""

    def test_meta_description_present(self, client: Any) -> None:
        """Index page has a non-empty <meta name="description">."""

        response = client.get("/")
        assert response.status_code == 200
        soup = _parse(response.text)
        content = _meta_content(soup, name="description")
        assert content is not None, "<meta name='description'> must be present"
        assert len(content) > 0, "meta description must not be empty"
        assert "adjudicacion" in content.lower()

    def test_og_title_present(self, client: Any) -> None:
        """Index page has <meta property="og:title">."""

        response = client.get("/")
        soup = _parse(response.text)
        content = _meta_content(soup, property="og:title")
        assert content is not None, "<meta property='og:title'> must be present"
        assert "AdjudicaUY" in content

    def test_og_type_is_website(self, client: Any) -> None:
        """Index page has <meta property="og:type" content="website">."""

        response = client.get("/")
        soup = _parse(response.text)
        content = _meta_content(soup, property="og:type")
        assert content == "website", f"og:type must be 'website', got {content!r}"

    def test_canonical_link_present(self, client: Any) -> None:
        """Index page has <link rel="canonical" href="...">."""

        response = client.get("/")
        soup = _parse(response.text)
        canonical = soup.find("link", attrs={"rel": "canonical"})
        assert canonical is not None, "<link rel='canonical'> must be present"
        href = canonical.get("href", "")
        assert href, "canonical href must not be empty"
        # In test env, site_url defaults to http://localhost:8000.
        assert "localhost:8000" in href or "adjudica" in href

    def test_json_ld_with_website_type(self, client: Any) -> None:
        """Index page has a JSON-LD script with @type: WebSite."""

        response = client.get("/")
        soup = _parse(response.text)
        ld_script = soup.find("script", attrs={"type": "application/ld+json"})
        assert ld_script is not None, "JSON-LD <script> must be present"
        data = json.loads(ld_script.string)
        assert data.get("@type") == "WebSite", f"@type must be 'WebSite', got {data.get('@type')!r}"
        assert "name" in data, "JSON-LD must include 'name'"


# ---------------------------------------------------------------------------
# 4.2 — About page (/about) SEO tags
# ---------------------------------------------------------------------------


class TestAboutSEO:
    """E2E: GET /about → full HTML with SEO meta tags."""

    def test_about_returns_200(self, client: Any) -> None:
        """About page returns HTTP 200."""

        response = client.get("/about")
        assert response.status_code == 200

    def test_about_meta_description(self, client: Any) -> None:
        """About page has a non-empty meta description."""

        response = client.get("/about")
        soup = _parse(response.text)
        content = _meta_content(soup, name="description")
        assert content is not None, "<meta name='description'> must be present"
        assert len(content) > 0
        assert "adjudica" in content.lower() or "plataforma" in content.lower()

    def test_about_canonical_url(self, client: Any) -> None:
        """About page has a canonical URL pointing to /about."""

        response = client.get("/about")
        soup = _parse(response.text)
        canonical = soup.find("link", attrs={"rel": "canonical"})
        assert canonical is not None
        href = canonical.get("href", "")
        assert "/about" in href, f"canonical href must contain '/about', got {href!r}"

    def test_about_og_tags(self, client: Any) -> None:
        """About page has OG title, description, and type tags."""

        response = client.get("/about")
        soup = _parse(response.text)
        og_title = _meta_content(soup, property="og:title")
        og_description = _meta_content(soup, property="og:description")
        og_type = _meta_content(soup, property="og:type")
        assert og_title is not None, "og:title must be present"
        assert og_description is not None, "og:description must be present"
        assert og_type == "website", f"og:type must be 'website', got {og_type!r}"

    def test_about_og_title_mentions_adjudicauy(self, client: Any) -> None:
        """About page OG title references AdjudicaUY."""

        response = client.get("/about")
        soup = _parse(response.text)
        og_title = _meta_content(soup, property="og:title")
        assert og_title is not None
        assert "AdjudicaUY" in og_title


# ---------------------------------------------------------------------------
# 4.3 — Pagination links have real href attributes
# ---------------------------------------------------------------------------


class TestPaginationHrefs:
    """E2E: Pagination <a> elements have real href attributes."""

    def test_next_link_has_href(
        self, client: Any, make_adjudication: Any
    ) -> None:
        """When on page 1 with multiple pages, the 'Siguiente' link has a real href."""

        # Create enough adjudications to trigger pagination (PAGE_SIZE=10).
        # Use date 2024-01-15 (factory default) and pass matching date params.
        for i in range(15):
            make_adjudication(
                organism=f"Organismo {i}",
                compra_overrides={"id_compra": f"pag-test-{i}"},
            )

        response = client.get("/?date_from=2024-01-01&date_to=2024-12-31")
        assert response.status_code == 200
        soup = _parse(response.text)

        # Find the "Siguiente" (next) pagination link.
        next_link = soup.find("a", string="Siguiente")
        assert next_link is not None, "'Siguiente' link must be present when total_pages > 1"
        href = next_link.get("href")
        assert href is not None, "Next link must have a real 'href' attribute"
        assert "page=2" in href, f"Next link href must contain 'page=2', got {href!r}"

    def test_prev_link_has_href_on_page_2(
        self, client: Any, make_adjudication: Any
    ) -> None:
        """When on page 2, the 'Anterior' link has a real href pointing to page 1."""

        # Create enough adjudications for 2+ pages.
        for i in range(15):
            make_adjudication(
                organism=f"OrganismoPrev {i}",
                compra_overrides={"id_compra": f"pag-prev-{i}"},
            )

        response = client.get("/?page=2&date_from=2024-01-01&date_to=2024-12-31")
        assert response.status_code == 200
        soup = _parse(response.text)

        # Find the "Anterior" (previous) pagination link.
        prev_link = soup.find("a", string="Anterior")
        assert prev_link is not None, "'Anterior' link must be present on page 2"
        href = prev_link.get("href")
        assert href is not None, "Previous link must have a real 'href' attribute"
        assert "page=1" in href, f"Previous link href must contain 'page=1', got {href!r}"

    def test_pagination_links_have_htmx_attrs(
        self, client: Any, make_adjudication: Any
    ) -> None:
        """Pagination <a> elements have both href AND hx-get attributes."""

        for i in range(15):
            make_adjudication(
                organism=f"OrganismoHtmx {i}",
                compra_overrides={"id_compra": f"pag-htmx-{i}"},
            )

        response = client.get("/?date_from=2024-01-01&date_to=2024-12-31")
        soup = _parse(response.text)

        next_link = soup.find("a", string="Siguiente")
        assert next_link is not None
        # Must have BOTH href (for crawlability) AND hx-get (for HTMX).
        assert next_link.get("href") is not None, "Must have href"
        assert next_link.get("hx-get") is not None, "Must have hx-get"


# ---------------------------------------------------------------------------
# 4.4 — Security headers on HTML responses
# ---------------------------------------------------------------------------


class TestSecurityHeadersE2E:
    """E2E: Every HTML response includes security headers."""

    def test_x_content_type_options_on_index(self, client: Any) -> None:
        """GET / response includes X-Content-Type-Options: nosniff."""

        response = client.get("/")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options_on_index(self, client: Any) -> None:
        """GET / response includes X-Frame-Options: DENY."""

        response = client.get("/")
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_x_content_type_options_on_about(self, client: Any) -> None:
        """GET /about response includes X-Content-Type-Options: nosniff."""

        response = client.get("/about")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options_on_about(self, client: Any) -> None:
        """GET /about response includes X-Frame-Options: DENY."""

        response = client.get("/about")
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_security_headers_on_htmx_partial(self, client: Any) -> None:
        """HTMX partial responses also include security headers."""

        response = client.get("/adjudications")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
