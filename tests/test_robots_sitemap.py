"""Tests for Phase 3: robots.txt and sitemap.xml routes.

Verifies that /robots.txt returns correct plain-text content and
/sitemap.xml returns valid XML with <loc> entries for index and
organism pages.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


def test_robots_txt_returns_200(client: Any) -> None:
    """/robots.txt returns HTTP 200."""

    response = client.get("/robots.txt")
    assert response.status_code == 200


def test_robots_txt_content_type(client: Any) -> None:
    """/robots.txt returns text/plain content type."""

    response = client.get("/robots.txt")
    assert "text/plain" in response.headers.get("content-type", "")


def test_robots_txt_contains_user_agent(client: Any) -> None:
    """/robots.txt body contains 'User-agent: *'."""

    response = client.get("/robots.txt")
    assert "User-agent: *" in response.text


def test_robots_txt_contains_allow(client: Any) -> None:
    """/robots.txt body contains 'Allow: /'."""

    response = client.get("/robots.txt")
    assert "Allow: /" in response.text


def test_robots_txt_contains_sitemap(client: Any) -> None:
    """/robots.txt body contains a Sitemap: line pointing to /sitemap.xml."""

    response = client.get("/robots.txt")
    assert "Sitemap:" in response.text
    assert "/sitemap.xml" in response.text


# ---------------------------------------------------------------------------
# sitemap.xml
# ---------------------------------------------------------------------------


def test_sitemap_xml_returns_200(client: Any) -> None:
    """/sitemap.xml returns HTTP 200."""

    response = client.get("/sitemap.xml")
    assert response.status_code == 200


def test_sitemap_xml_content_type(client: Any) -> None:
    """/sitemap.xml returns application/xml content type."""

    response = client.get("/sitemap.xml")
    content_type = response.headers.get("content-type", "")
    assert "application/xml" in content_type or "text/xml" in content_type


def test_sitemap_xml_contains_loc_for_index(client: Any) -> None:
    """/sitemap.xml includes a <loc> entry for the index page."""

    response = client.get("/sitemap.xml")
    assert "<loc>" in response.text
    # The sitemap should contain at least one <loc> entry.
    assert "</loc>" in response.text


def test_sitemap_xml_contains_organism_locs(
    client: Any, make_adjudication: Any
) -> None:
    """/sitemap.xml includes <loc> entries for organism pages in the DB."""

    make_adjudication(organism="Ministerio de Salud")
    make_adjudication(organism="ANEP")

    response = client.get("/sitemap.xml")
    # Organism pages should appear as <loc> entries with URL-encoded names.
    assert "Ministerio" in response.text
    assert "ANEP" in response.text


def test_sitemap_xml_reflects_live_organisms(
    client: Any, make_adjudication: Any
) -> None:
    """/sitemap.xml reflects the current set of organisms in the DB."""

    make_adjudication(organism="BPS")

    response = client.get("/sitemap.xml")
    assert "BPS" in response.text


def test_sitemap_xml_contains_distinct_encoded_company_urls(
    client: Any, make_adjudication: Any
) -> None:
    """Company sitemap entries encode both document path segments."""

    make_adjudication(
        company_document_type="RUT/X &",
        company_document="00 1/2?",
    )
    make_adjudication(
        company_document_type="RUT/X &",
        company_document="00 1/2?",
    )
    make_adjudication(
        company_document_type=None,
        company_document=None,
    )

    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    encoded_url = "/company/RUT%2FX%20%26/00%201%2F2%3F"
    assert encoded_url in response.text
    assert response.text.count(encoded_url) == 1


def test_sitemap_xml_is_valid_xml(client: Any) -> None:
    """/sitemap.xml body is well-formed XML."""

    import xml.etree.ElementTree as ET

    response = client.get("/sitemap.xml")
    # Should not raise. S314 accepted: test data, not untrusted input.
    ET.fromstring(response.text)  # noqa: S314
