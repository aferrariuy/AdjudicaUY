"""Fetch and parse the RSS adjudication feed (Source B).

The RSS feed lists public procurement adjudications published on
``comprasestatales.gub.uy``. Each ``<item>`` carries a human-readable title
and a link to the public detail page. The downstream joiner matches these
items to the records produced by :mod:`scraper.xml_report` by their shared
``id_compra``.

A representative item looks like::

    <item>
      <title>Compra Directa 86825/2026 - Administración de las Obras
             Sanitarias del Estado | Administración de las Obras
             Sanitarias del Estado</title>
      <link>http://www.comprasestatales.gub.uy/consultas/detalle/id/1319278</link>
      <pubDate>Mon, 15 Jan 2024 12:34:56 +0000</pubDate>
      <description>...</description>
    </item>

The organism is the title segment *after* the last ``" - "`` and *before*
``" | "``; the numeric identifier parsed from the link is the same as
``id_compra`` in the XML report.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from lxml import etree

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# ``/id/{numeric-id}`` or ``/id/i{numeric-id}`` somewhere in the link path.
_ID_IN_LINK_RE = re.compile(r"/id/i?(\d+)(?:/|$)")


@dataclass(frozen=True)
class RssItem:
    """One feed entry, normalized to the fields the joiner needs."""

    id_compra: str
    organism: str
    license_link: str


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def fetch_rss_feed(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> str:
    """Fetch the raw RSS XML payload from ``url``.

    Mirrors :func:`scraper.xml_report.fetch_xml_report`.
    """

    if client is None:
        with httpx.Client(timeout=timeout, headers=_HEADERS) as owned:
            response = owned.get(url)
            response.raise_for_status()
            return response.text

    response = client.get(url, headers=_HEADERS)
    response.raise_for_status()
    return response.text


def _extract_id_from_link(link: str | None) -> str | None:
    """Pull the numeric ``id_compra`` from a detail-page URL.

    Accepts anything matching ``.../id/{digits}...``. Returns ``None`` for
    unparseable links so the joiner can log and skip the entry.
    """

    if not link:
        return None
    match = _ID_IN_LINK_RE.search(link)
    if match is None:
        return None
    return match.group(1)


def _extract_organism_from_title(title: str | None) -> str | None:
    """Extract the organism name from an RSS item title.

    The observed pattern is
    ``"<type> <number>/<year> - <organism> | <organism>"``; the organism is
    the segment between the last ``" - "`` and ``" | "``. Falls back to the
    trimmed full title when the separators are absent.
    """

    if not title:
        return None
    cleaned = title.strip()
    if not cleaned:
        return None

    # Split off the redundant trailing ``| organism`` (it duplicates the
    # organism already present before the pipe in practice).
    left = cleaned.split(" | ", 1)[0].strip()
    if " - " in left:
        return left.rsplit(" - ", 1)[1].strip() or None
    return left or None


def _normalize_item(item_el: etree._Element) -> RssItem | None:
    """Build a :class:`RssItem` from one ``<item>`` element."""

    title_el = None
    link_el = None
    for child in item_el:
        if child.tag.endswith("title") and title_el is None:
            title_el = child
        elif child.tag.endswith("link") and link_el is None:
            link_el = child
        if title_el is not None and link_el is not None:
            break

    title = (
        (title_el.text or "").strip()
        if title_el is not None and title_el.text
        else None
    )
    link = (
        (link_el.text or "").strip() if link_el is not None and link_el.text else None
    )

    id_compra = _extract_id_from_link(link)
    organism = _extract_organism_from_title(title)

    if id_compra is None:
        logger.warning("Skipping RSS item without parseable id_compra: link=%r", link)
        return None
    if organism is None:
        logger.warning(
            "Skipping RSS item id_compra=%s without parseable organism: title=%r",
            id_compra,
            title,
        )
        return None

    return RssItem(id_compra=id_compra, organism=organism, license_link=link or "")


def parse_rss_feed(xml_text: str) -> Iterator[RssItem]:
    """Yield an :class:`RssItem` for every well-formed ``<item>`` element.

    Parameters
    ----------
    xml_text:
        The raw RSS payload returned by :func:`fetch_rss_feed`.

    Yields
    ------
    RssItem
        One per well-formed item. Unparseable items are skipped with a
        warning.
    """

    try:
        root = etree.fromstring(
            xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text
        )
    except etree.XMLSyntaxError as exc:
        logger.error("RSS payload is malformed: %s", exc)
        return

    for item in root.iter():
        if not item.tag.endswith("item"):
            continue
        record = _normalize_item(item)
        if record is not None:
            yield record


__all__ = ["RssItem", "fetch_rss_feed", "parse_rss_feed"]
