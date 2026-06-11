"""Unit tests for :mod:`scraper.rss_feed`."""

from __future__ import annotations

import httpx
import pytest

from scraper.rss_feed import fetch_rss_feed, parse_rss_feed


# ---------------------------------------------------------------------------
# Fixture RSS payloads
# ---------------------------------------------------------------------------

VALID_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Adjudicaciones</title>
    <item>
      <title>Compra Directa 86825/2024 - Ministerio de Interior | Ministerio de Interior</title>
      <link>http://www.comprasestatales.gub.uy/consultas/detalle/id/1319278</link>
      <pubDate>Mon, 15 Jan 2024 12:34:56 +0000</pubDate>
    </item>
    <item>
      <title>Licitacion Publica 12/2024 - Administracion de Obras Sanitarias | Administracion de Obras Sanitarias</title>
      <link>http://www.comprasestatales.gub.uy/consultas/detalle/id/1319279</link>
      <pubDate>Tue, 20 Feb 2024 09:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

# Item with no parseable id in the link.
RSS_BAD_LINK = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Compra X - Organismo Y | Organismo Y</title>
      <link>http://www.example.test/no-id-here</link>
    </item>
  </channel>
</rss>
"""

# Item with a title that has no ``|`` and no `` - `` separator.
RSS_BAD_TITLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Plain title without separators</title>
      <link>http://www.comprasestatales.gub.uy/consultas/detalle/id/9999</link>
    </item>
  </channel>
</rss>
"""

RSS_MALFORMED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Compra
      <link>http://example.test/id/1
    </item>
"""


# ---------------------------------------------------------------------------
# Valid payload
# ---------------------------------------------------------------------------


def test_parse_valid_rss_yields_one_item_per_entry() -> None:
    items = list(parse_rss_feed(VALID_RSS))
    assert len(items) == 2


def test_parse_valid_rss_extracts_organism_from_title() -> None:
    items = list(parse_rss_feed(VALID_RSS))
    organisms = {item.organism for item in items}
    # The parser strips the duplicated segment after the pipe and trims whitespace.
    assert organisms == {"Ministerio de Interior", "Administracion de Obras Sanitarias"}


def test_parse_valid_rss_extracts_id_from_link() -> None:
    items = list(parse_rss_feed(VALID_RSS))
    ids = {item.id_compra for item in items}
    assert ids == {"1319278", "1319279"}


def test_parse_valid_rss_preserves_full_link() -> None:
    items = list(parse_rss_feed(VALID_RSS))
    first = items[0]
    assert first.license_link == (
        "http://www.comprasestatales.gub.uy/consultas/detalle/id/1319278"
    )


# ---------------------------------------------------------------------------
# Malformed / incomplete payloads
# ---------------------------------------------------------------------------


def test_parse_rss_skips_item_without_id() -> None:
    items = list(parse_rss_feed(RSS_BAD_LINK))
    assert items == []


def test_parse_rss_handles_item_with_simple_title() -> None:
    """A title with no ``|`` separator should still produce a record using the title.

    The parser falls back to the full (trimmed) title as the organism
    when the standard separators are missing — this is exercised in
    the data-ingestion spec "Missing or malformed items" branch.
    """

    items = list(parse_rss_feed(RSS_BAD_TITLE))
    assert len(items) == 1
    assert items[0].id_compra == "9999"
    assert items[0].organism == "Plain title without separators"


def test_parse_malformed_rss_returns_empty_iterable() -> None:
    items = list(parse_rss_feed(RSS_MALFORMED))
    assert items == []


def test_parse_empty_rss_returns_empty_iterable() -> None:
    assert list(parse_rss_feed("")) == []


def test_parse_rss_handles_mixed_valid_and_invalid_items() -> None:
    payload = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Compra - O | O</title>
      <link>http://example.test/id/1</link>
    </item>
    <item>
      <title>Compra sin link decente</title>
      <link>not-a-url</link>
    </item>
  </channel>
</rss>
"""
    items = list(parse_rss_feed(payload))
    # Only the first item has a parseable id.
    assert len(items) == 1
    assert items[0].id_compra == "1"


# ---------------------------------------------------------------------------
# Network failure
# ---------------------------------------------------------------------------


def test_fetch_rss_propagates_http_errors() -> None:
    class _BoomClient:
        def get(self, url: str):  # noqa: ARG002
            request = httpx.Request("GET", url)
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_rss_feed("https://example.test/rss", client=_BoomClient())


def test_fetch_rss_returns_body_on_success() -> None:
    class _OkClient:
        def get(self, url: str):  # noqa: ARG002
            request = httpx.Request("GET", url)
            return httpx.Response(200, request=request, text=VALID_RSS)

    body = fetch_rss_feed("https://example.test/rss", client=_OkClient())
    assert body == VALID_RSS
