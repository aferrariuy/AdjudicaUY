"""Tests for the per-URL ``<lastmod>`` in sitemap.xml.

A ``<lastmod>`` tells a crawler when to come back. It is only worth publishing
while it stays "consistently and verifiably accurate": a catalogue-wide date
stamped on all ~28.7k URLs would claim every page changed whenever any page did,
which is worse than publishing no date. So the sitemap dates each URL with the
newest publication recorded *for that page*, read through the adjudicacion join
so a parent-only compra cannot make a page advertise data it cannot display.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _entries(xml: str) -> list[tuple[str, str | None]]:
    """Return ``(loc, lastmod)`` for every URL, in document order."""

    root = ET.fromstring(xml)  # noqa: S314 — test data, not untrusted input
    return [
        (
            url.findtext(f"{SITEMAP_NS}loc") or "",
            url.findtext(f"{SITEMAP_NS}lastmod"),
        )
        for url in root.findall(f"{SITEMAP_NS}url")
    ]


def test_sitemap_omits_lastmod_when_catalog_is_empty(client: Any) -> None:
    """An empty catalogue asserts no dates rather than a fabricated one."""

    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert "<lastmod>" not in response.text


def test_sitemap_dates_every_url_once_data_exists(
    client: Any, make_adjudication: Any
) -> None:
    """No URL is left undated when the catalogue has publication dates."""

    make_adjudication()

    body = client.get("/sitemap.xml").text

    assert body.count("<loc>") == 3
    assert body.count("<lastmod>") == body.count("<loc>")


def test_index_lastmod_is_the_newest_publication(
    client: Any, make_adjudication: Any
) -> None:
    """The index page changes whenever any new award is published."""

    make_adjudication(compra_overrides={"fecha_pub_adj": date(2021, 3, 4)})
    make_adjudication(compra_overrides={"fecha_pub_adj": date(2026, 9, 13)})

    entries = _entries(client.get("/sitemap.xml").text)
    loc, lastmod = entries[0]

    assert loc.endswith("/")
    assert lastmod == "2026-09-13"


def test_organism_lastmod_is_its_own_newest_publication(
    client: Any, make_adjudication: Any
) -> None:
    """Each organism page carries its own date, not the catalogue's.

    This is the whole point of the change: an organism that last saw an award in
    2020 must not be re-crawled daily because a different organism published
    yesterday.
    """

    make_adjudication(
        organism="ANEP", compra_overrides={"fecha_pub_adj": date(2021, 3, 4)}
    )
    make_adjudication(
        organism="ANEP", compra_overrides={"fecha_pub_adj": date(2023, 11, 30)}
    )
    make_adjudication(
        organism="BPS", compra_overrides={"fecha_pub_adj": date(2020, 5, 5)}
    )

    dated = dict(_entries(client.get("/sitemap.xml").text))

    assert dated["http://localhost:8000/organism/ANEP"] == "2023-11-30"
    assert dated["http://localhost:8000/organism/BPS"] == "2020-05-05"
    assert dated["http://localhost:8000/"] == "2023-11-30"


def test_company_lastmod_is_its_own_newest_publication(
    client: Any, make_adjudication: Any
) -> None:
    """Company pages are dated by their own awards."""

    make_adjudication(
        company_document_type="RUT",
        company_document="210000000012",
        compra_overrides={"fecha_pub_adj": date(2022, 7, 1)},
    )
    make_adjudication(
        company_document_type="RUT",
        company_document="210000000012",
        compra_overrides={"fecha_pub_adj": date(2025, 2, 20)},
    )

    dated = dict(_entries(client.get("/sitemap.xml").text))

    assert dated["http://localhost:8000/company/RUT/210000000012"] == "2025-02-20"


def test_lastmod_is_a_plain_iso_date(client: Any, make_adjudication: Any) -> None:
    """Google accepts any W3C datetime; a date is unambiguous and compact.

    Emitting a timestamp would add bytes to every one of the ~28.7k entries and
    imply an hour-level precision the source data does not have.
    """

    make_adjudication()

    body = client.get("/sitemap.xml").text
    for value in re.findall(r"<lastmod>([^<]+)</lastmod>", body):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), value


def test_sitemap_with_lastmod_is_still_valid_xml(
    client: Any, make_adjudication: Any
) -> None:
    """The added element keeps the document well-formed and namespaced."""

    make_adjudication(organism="<A> & </B>", company_document="RUT & 1/2?")

    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert len(_entries(response.text)) == 3


def test_sitemap_lastmod_is_cached_with_the_body(client: Any) -> None:
    """The dates ride the existing body cache: no extra work on a warm hit."""

    from unittest.mock import patch

    with (
        patch(
            "app.main.organisms_with_last_activity",
            return_value=[("ANEP", date(2023, 11, 30))],
        ) as organisms_mock,
        patch(
            "app.main.companies_with_last_activity",
            return_value=[("RUT", "1", date(2022, 1, 2))],
        ) as companies_mock,
        patch(
            "app.main.catalog_date_span",
            return_value=(date(2020, 1, 1), date(2026, 9, 13)),
        ) as span_mock,
    ):
        first = client.get("/sitemap.xml")
        second = client.get("/sitemap.xml")

    assert first.content == second.content
    organisms_mock.assert_called_once()
    companies_mock.assert_called_once()
    assert span_mock.call_count == 1
    dated = dict(_entries(first.text))
    assert dated["http://localhost:8000/"] == "2026-09-13"
    assert dated["http://localhost:8000/organism/ANEP"] == "2023-11-30"
    assert dated["http://localhost:8000/company/RUT/1"] == "2022-01-02"
