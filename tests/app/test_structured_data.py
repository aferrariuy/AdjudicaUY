"""Tests for the breadcrumb and catalogue structured data.

Two additions, both driven by what Google actually requires rather than by
what looks like more markup:

* ``BreadcrumbList`` on the entity pages, plus the matching visible trail.
  Google requires ``name``, ``position`` and ``item`` on every ``ListItem``
  except the last, where ``item`` may be omitted, and requires at least two
  items. That is why the trail is two levels and not the "adjudicauy ›
  Organismos › X" three-level trail an earlier audit sketched: an
  intermediate level needs a real URL, and a fabricated one would be invalid
  markup rather than a richer trail.
* A ``Dataset`` node for the whole catalogue on the index page, so the data
  can surface in Google Dataset Search. Only ``name`` and ``description`` are
  required there, and the description must be 50-5000 characters.

The visible trail and the JSON-LD are built from one list, so these tests
assert they cannot drift apart.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from bs4 import BeautifulSoup

ORGANISM = "Ministerio de Interior"
ORGANISM_URL = "/organism/Ministerio%20de%20Interior"
COMPANY_URL = "/company/RUT/210000000001"
COMPANY_NAME = "Empresa 1"

BREADCRUMB_NAV_LABEL = "Ruta de navegación"


def _ld_blocks(html: str) -> list[dict[str, Any]]:
    """Return every JSON-LD block on the page, in document order."""

    soup = BeautifulSoup(html, "html.parser")
    blocks: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        assert script.string, "a JSON-LD script must not be empty"
        blocks.append(json.loads(script.string))
    return blocks


def _blocks_of_type(html: str, type_name: str) -> list[dict[str, Any]]:
    return [block for block in _ld_blocks(html) if block.get("@type") == type_name]


def _visible_trail(html: str) -> list[str]:
    """Return the visible breadcrumb labels, without the separators."""

    soup = BeautifulSoup(html, "html.parser")
    nav = soup.find("nav", attrs={"aria-label": BREADCRUMB_NAV_LABEL})
    assert nav is not None, "the visible breadcrumb trail must be rendered"
    return [
        item.get_text(strip=True)
        for item in nav.find_all("li")
        if not item.has_attr("aria-hidden")
    ]


# ── The builder ────────────────────────────────────────────────────────


def test_breadcrumb_builder_wraps_the_trail_in_a_breadcrumb_list() -> None:
    """The envelope is the part only this test covers.

    The element shape — positions, names, where ``item`` appears — is pinned by the
    tests below, so this one asserts the ``@context``/``@type`` pair that makes the
    node machine-readable and the element count that matches the two-level trail both
    call sites build.
    """

    from app.presenters import _build_breadcrumb_json_ld

    node = _build_breadcrumb_json_ld([("Inicio", "/"), ("ANP", None)])

    assert node["@context"] == "https://schema.org"
    assert node["@type"] == "BreadcrumbList"
    assert len(node["itemListElement"]) == 2


def test_breadcrumb_builder_rejects_a_trail_shorter_than_two_items() -> None:
    """Google needs two ListItems, so a one-item trail is a caller bug.

    The builder used to accept it and emit a trail that can never be eligible, while
    its docstring claimed the two-item minimum. Failing here turns an invisible SEO
    regression into a loud one.
    """

    from app.presenters import _build_breadcrumb_json_ld

    with pytest.raises(ValueError, match="at least 2 items"):
        _build_breadcrumb_json_ld([("Inicio", "/")])


def test_breadcrumb_builder_positions_are_sequential_from_one() -> None:
    from app.presenters import _build_breadcrumb_json_ld

    node = _build_breadcrumb_json_ld([("Inicio", "/"), ("ANP", None)])
    positions = [element["position"] for element in node["itemListElement"]]

    assert positions == [1, 2]


def test_breadcrumb_builder_omits_item_only_for_the_last_element() -> None:
    """`item` is required on every ListItem except the final one."""

    from app.presenters import _build_breadcrumb_json_ld

    node = _build_breadcrumb_json_ld([("Inicio", "/"), ("ANP", None)])
    first, last = node["itemListElement"]

    assert first["item"].endswith("/")
    assert "item" not in last, "the last item must not fabricate a URL"
    assert last["name"] == "ANP"


def test_breadcrumb_builder_names_every_element() -> None:
    from app.presenters import _build_breadcrumb_json_ld

    node = _build_breadcrumb_json_ld([("Inicio", "/"), ("ANP", None)])

    assert [element["name"] for element in node["itemListElement"]] == [
        "Inicio",
        "ANP",
    ]


# ── Organism page ──────────────────────────────────────────────────────


def test_organism_page_emits_a_breadcrumb_list(
    client: Any, make_adjudication: Any
) -> None:
    make_adjudication(organism=ORGANISM)

    blocks = _blocks_of_type(client.get(ORGANISM_URL).text, "BreadcrumbList")

    assert len(blocks) == 1
    names = [element["name"] for element in blocks[0]["itemListElement"]]
    assert names == ["Inicio", ORGANISM]


def test_organism_breadcrumb_links_home_absolutely(
    client: Any, make_adjudication: Any
) -> None:
    from app.config import get_settings

    make_adjudication(organism=ORGANISM)

    block = _blocks_of_type(client.get(ORGANISM_URL).text, "BreadcrumbList")[0]

    assert block["itemListElement"][0]["item"] == f"{get_settings().site_url}/"


def test_organism_visible_trail_matches_the_json_ld(
    client: Any, make_adjudication: Any
) -> None:
    """The trail on screen and the trail in the markup cannot drift apart."""

    make_adjudication(organism=ORGANISM)

    body = client.get(ORGANISM_URL).text
    block = _blocks_of_type(body, "BreadcrumbList")[0]

    assert _visible_trail(body) == [
        element["name"] for element in block["itemListElement"]
    ]


def test_organism_visible_trail_links_home_and_marks_the_current_page(
    client: Any, make_adjudication: Any
) -> None:
    make_adjudication(organism=ORGANISM)

    soup = BeautifulSoup(client.get(ORGANISM_URL).text, "html.parser")
    nav = soup.find("nav", attrs={"aria-label": BREADCRUMB_NAV_LABEL})
    assert nav is not None

    home_link = nav.find("a")
    assert home_link is not None and home_link["href"] == "/"
    assert home_link.get_text(strip=True) == "Inicio"
    assert nav.find("li", attrs={"aria-current": "page"}) is not None


# ── Company page ───────────────────────────────────────────────────────


def test_company_page_emits_a_breadcrumb_list(
    client: Any, make_adjudication: Any
) -> None:
    make_adjudication()

    blocks = _blocks_of_type(client.get(COMPANY_URL).text, "BreadcrumbList")

    assert len(blocks) == 1
    names = [element["name"] for element in blocks[0]["itemListElement"]]
    assert names == ["Inicio", COMPANY_NAME]


def test_company_visible_trail_matches_the_json_ld(
    client: Any, make_adjudication: Any
) -> None:
    make_adjudication()

    body = client.get(COMPANY_URL).text
    block = _blocks_of_type(body, "BreadcrumbList")[0]

    assert _visible_trail(body) == [
        element["name"] for element in block["itemListElement"]
    ]


# ── Where the breadcrumb must NOT appear ───────────────────────────────


def test_index_page_has_no_breadcrumb(client: Any) -> None:
    """The root has no trail: Google needs two items and the path is just "/"."""

    body = client.get("/").text

    assert _blocks_of_type(body, "BreadcrumbList") == []
    assert BREADCRUMB_NAV_LABEL not in body


# ── The catalogue Dataset ──────────────────────────────────────────────


def test_index_emits_a_dataset_node(client: Any) -> None:
    blocks = _blocks_of_type(client.get("/").text, "Dataset")

    assert len(blocks) == 1


def test_dataset_has_both_required_properties(client: Any) -> None:
    """Google requires exactly `name` and `description` for a Dataset."""

    block = _blocks_of_type(client.get("/").text, "Dataset")[0]

    assert block["name"]
    assert block["description"]


def test_dataset_description_is_within_googles_bounds(client: Any) -> None:
    """A description outside 50-5000 characters is rejected outright."""

    description = _blocks_of_type(client.get("/").text, "Dataset")[0]["description"]

    assert 50 <= len(description) <= 5000, len(description)


def test_dataset_distribution_points_at_an_absolute_url(client: Any) -> None:
    from app.config import get_settings

    block = _blocks_of_type(client.get("/").text, "Dataset")[0]
    distribution = block["distribution"][0]

    assert distribution["@type"] == "DataDownload"
    assert distribution["encodingFormat"] == "text/csv"
    assert distribution["contentUrl"].startswith(f"{get_settings().site_url}/")


def test_dataset_declares_the_source_license(client: Any) -> None:
    """The catalogue names the licence its data is published under.

    This test used to assert the opposite, reasoning that the project declared
    no licence and that naming one would grant terms nobody granted. The
    reasoning was sound and the premise was false: the source data is published
    by the Uruguayan state under the "Licencia de Datos Abiertos - Uruguay",
    which Decreto N° 54/017 makes mandatory for public bodies publishing open
    data, and whose article 2 requires the licence to be identified on the site
    that uses it. Omitting it was the misstatement.

    The provenance of the URL is pinned by literal in
    ``tests/test_data_attribution.py``, together with the assertion that this
    value and the link a reader can click are the same document.
    """

    block = _blocks_of_type(client.get("/").text, "Dataset")[0]

    assert block["license"].startswith("https://")


def test_dataset_claims_the_measured_temporal_coverage(
    client: Any, make_adjudication: Any
) -> None:
    """The catalogue's real span, in schema.org's ISO 8601 ``start/end`` form.

    The span was a deliberate omission while it was unmeasured, and a guessed one
    would have been a fabrication. It is now read from the same query that dates
    the sitemap, so publishing it costs nothing extra and stops being a guess.
    """

    make_adjudication(compra_overrides={"fecha_pub_adj": date(2021, 3, 4)})
    make_adjudication(compra_overrides={"fecha_pub_adj": date(2026, 9, 13)})

    block = _blocks_of_type(client.get("/").text, "Dataset")[0]

    assert block["temporalCoverage"] == "2021-03-04/2026-09-13"


def test_dataset_omits_temporal_coverage_on_an_empty_catalogue(
    client: Any,
) -> None:
    """An unmeasured span stays unasserted rather than guessed."""

    block = _blocks_of_type(client.get("/").text, "Dataset")[0]

    assert "temporalCoverage" not in block


def test_website_node_remains_the_first_json_ld_block(client: Any) -> None:
    """Existing rich-result tooling reads the first block.

    Adding the Dataset node must not displace the WebSite node that
    ``tests/test_seo_e2e.py`` asserts on.
    """

    blocks = _ld_blocks(client.get("/").text)

    assert blocks[0]["@type"] == "WebSite"


def test_entity_pages_do_not_emit_a_dataset(
    client: Any, make_adjudication: Any
) -> None:
    """A per-entity slice is a view of the catalogue, not a separate dataset.

    Emitting a Dataset for each of the ~28.4k company pages would add markup to
    the thinnest pages on the site, which is how structured data gets
    devalued rather than how it earns a rich result.
    """

    make_adjudication()

    assert _blocks_of_type(client.get(ORGANISM_URL).text, "Dataset") == []
    assert _blocks_of_type(client.get(COMPANY_URL).text, "Dataset") == []
