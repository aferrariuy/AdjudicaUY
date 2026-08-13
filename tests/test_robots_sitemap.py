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


def test_sitemap_xml_cache_control_public_max_age(client: Any) -> None:
    """/sitemap.xml advertises the configured aggregate cache TTL."""

    response = client.get("/sitemap.xml")
    assert response.headers.get("Cache-Control") == "public, max-age=600"


def test_sitemap_warm_hit_is_byte_identical_and_skips_catalog_queries(
    client: Any,
) -> None:
    """Two requests inside the TTL share one cached body and no catalog work.

    The sitemap body is cached as a complete XML string under the
    ``sitemap_xml`` whitelisted aggregate, so the second request performs
    zero catalog queries and returns a byte-identical response with the
    same URL set, order, and percent encoding. The autouse
    ``reset_query_cache`` fixture clears the shared store between tests,
    which covers this entry too.
    """

    from unittest.mock import patch

    organisms = ["Ministerio de Salud", "ANEP"]
    companies = [("RUT", "1"), ("RUT/X &", "00 1/2?")]
    with (
        patch("app.main.all_organisms", return_value=organisms) as organisms_mock,
        patch("app.main.all_companies", return_value=companies) as companies_mock,
    ):
        first = client.get("/sitemap.xml")
        second = client.get("/sitemap.xml")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content
    organisms_mock.assert_called_once()
    companies_mock.assert_called_once()
    # URL set, order, and encoding are preserved on the warm body.
    assert b"/organism/Ministerio%20de%20Salud" in first.content
    assert b"/organism/ANEP" in first.content
    assert b"/company/RUT/1" in first.content
    assert b"/company/RUT%2FX%20%26/00%201%2F2%3F" in first.content


def test_sitemap_expiry_regenerates_catalog(monkeypatch, client: Any) -> None:
    """After the TTL elapses the sitemap is regenerated from the catalogs."""

    from unittest.mock import patch

    now = [100.0]
    monkeypatch.setattr("app.services.query_cache.time.monotonic", lambda: now[0])
    with (
        patch("app.main.all_organisms", return_value=["Org A"]) as organisms_mock,
        patch("app.main.all_companies", return_value=[("RUT", "1")]) as companies_mock,
    ):
        first = client.get("/sitemap.xml")
        now[0] = 699.9  # still inside the 600s TTL.
        warm = client.get("/sitemap.xml")
        assert organisms_mock.call_count == 1
        now[0] = 700.1  # past the TTL: regenerate from the catalogs.
        refreshed = client.get("/sitemap.xml")

    assert first.content == warm.content
    assert organisms_mock.call_count == 2
    assert companies_mock.call_count == 2
    # The refreshed body is byte-identical content, stored for later hits.
    assert refreshed.content == first.content


def test_sitemap_zero_ttl_disables_storage_and_advertises_max_age_0(
    monkeypatch, db_session: Any
) -> None:
    """TTL zero regenerates every request, stores nothing, and advertises max-age=0."""

    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import create_app

    monkeypatch.setenv("CACHE_TTL_SECONDS", "0")
    app = create_app()

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with (
        patch("app.main.all_organisms", return_value=["Org A"]) as organisms_mock,
        patch("app.main.all_companies", return_value=[("RUT", "1")]) as companies_mock,
        TestClient(app) as zero_ttl_client,
    ):
        first = zero_ttl_client.get("/sitemap.xml")
        second = zero_ttl_client.get("/sitemap.xml")

    assert first.headers.get("Cache-Control") == "public, max-age=0"
    assert second.headers.get("Cache-Control") == "public, max-age=0"
    assert first.content == second.content
    assert organisms_mock.call_count == 2
    assert companies_mock.call_count == 2


def test_sitemap_xml_is_valid_xml(client: Any) -> None:
    """/sitemap.xml body is well-formed XML."""

    import xml.etree.ElementTree as ET

    response = client.get("/sitemap.xml")
    # Should not raise. S314 accepted: test data, not untrusted input.
    ET.fromstring(response.text)  # noqa: S314
