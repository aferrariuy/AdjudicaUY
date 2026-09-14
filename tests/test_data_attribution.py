"""Tests for the attribution the source licence requires.

Uruguay makes the licence of its open data a matter of decree, not of taste.
The "Licencia de Datos Abiertos - Uruguay" is Annex I of Decreto N° 54/017,
which regulates article 82 of Ley N° 19.355, and adopting it is mandatory for
every public body that publishes open data. Article 2 of that decree is the
part that reaches this project: the licence "deberá estar identificada en el
sitio web, aplicación o sistema que use datos abiertos". AdjudicaUY uses them,
so AdjudicaUY has to identify it.

The licence's own "Nota de Origen" clause names three things that have to be
cited together, and the site named only one of them:

* the name of the provider,
* the reference to the "Licencia de Datos Abiertos - Uruguay",
* the reference to the data set being used.

The provider is the Agencia Reguladora de Compras Estatales (ARCE). It was
created by Ley N° 18.362 in 2008 as the "Agencia de Compras y Contrataciones
del Estado" (ACCE) and transformed into ARCE by article 329 of Ley N° 19.889,
in force since 2020. So the old name is not a stylistic preference: the site
shipped a factual error for years, and the first two tests below are what
keeps it from coming back.

The last test is the one that would have caught a subtler failure. The licence
is stated twice — once in the ``Dataset`` markup a crawler reads, once as the
link a citizen can click — and the two must be the same document. Asserting
each against a constant would let one drift while both kept "passing"; asking
them to agree with each other cannot.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from bs4 import BeautifulSoup

PROVIDER_NAME = "Agencia Reguladora de Compras Estatales"
"Current name, in force since Ley N° 19.889 art. 329 (2020)."

FORMER_PROVIDER_NAME = "Agencia de Compras y Contrataciones"
"Pre-2020 name. It must not appear anywhere a reader can see it."

LICENCE_NAME = "Licencia de Datos Abiertos"
"Annex I of Decreto N° 54/017; the Nota de Origen requires naming it."

DATA_SET_MARKER = "reportes XML"
"How the footer identifies the data set actually consumed."

ORGANISM_URL = "/organism/Ministerio%20de%20Interior"
COMPANY_URL = "/company/RUT/210000000001"

FULL_PAGES = ["/", "/about", ORGANISM_URL, COMPANY_URL]


@pytest.fixture
def seed(make_adjudication: Any) -> None:
    """Persist one adjudication so the entity routes resolve to 200."""

    make_adjudication()


def _footer(html: str) -> Any:
    """The rendered footer. Every full page has one; fragments have none."""

    footer = BeautifulSoup(html, "html.parser").find("footer")
    assert footer is not None, "a full page must render the footer"
    return footer


def _footer_text(html: str) -> str:
    """The footer's visible text, whitespace collapsed."""

    return " ".join(_footer(html).get_text(" ", strip=True).split())


def _own_content_text(html: str) -> str:
    """A page's own text, excluding the chrome every page shares.

    The footer is on every page, so it is the wrong place to look when the
    question is whether *this* page says something. Scoping to ``main`` keeps a
    test named after a page from passing on the footer's words instead.
    """

    main = BeautifulSoup(html, "html.parser").find("main")
    assert main is not None, "a full page renders its content inside main"
    return " ".join(main.get_text(" ", strip=True).split())


def _licence_href(html: str) -> str:
    """The URL behind whichever link carries the licence's name.

    Reading the link rather than restating a constant is deliberate: this is
    the value the ``Dataset`` markup is compared against, and two copies of a
    constant would agree with each other while disagreeing with the page.
    """

    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a"):
        if LICENCE_NAME in anchor.get_text():
            href = anchor.get("href")
            assert href, "the licence link must carry an href"
            return str(href)
    raise AssertionError(
        "the licence must be linked, not merely mentioned; "
        f"no anchor is labelled with {LICENCE_NAME!r}"
    )


def _dataset_block(html: str) -> dict[str, Any]:
    """The catalogue ``Dataset`` node, which must be the only one on the page."""

    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    found = []
    for script in scripts:
        assert script.string, "a JSON-LD script must not be empty"
        block = json.loads(script.string)
        if block.get("@type") == "Dataset":
            found.append(block)
    assert len(found) == 1, f"expected exactly one Dataset node, got {len(found)}"
    return cast("dict[str, Any]", found[0])


# ── The provider's current name ────────────────────────────────────────────


@pytest.mark.usefixtures("seed")
@pytest.mark.parametrize("path", FULL_PAGES)
def test_full_pages_name_the_current_agency(client: Any, path: str) -> None:
    assert PROVIDER_NAME in client.get(path).text


@pytest.mark.usefixtures("seed")
@pytest.mark.parametrize("path", FULL_PAGES)
def test_full_pages_never_use_the_agencys_former_name(client: Any, path: str) -> None:
    """The 2020 rename is the fact; the old name is a wrong answer, not a variant.

    The acronym is checked parenthesised rather than bare: "ACCE" is a substring
    of ordinary Spanish words such as "ACCESO", and a substring test would fail
    on prose that is perfectly correct.
    """

    body = client.get(path).text

    assert FORMER_PROVIDER_NAME not in body
    assert "(ACCE)" not in body


# ── The licence and the data set ───────────────────────────────────────────


@pytest.mark.usefixtures("seed")
@pytest.mark.parametrize("path", FULL_PAGES)
def test_every_full_page_cites_the_three_nota_de_origen_elements(
    client: Any, path: str
) -> None:
    """One footer, three citations, on every page the decree can reach.

    Article 2 asks for the licence to be identified on the site; the licence's
    Nota de Origen says a citation carries the provider, the licence and the
    data set. Asserting all three against the footer keeps this test about the
    citation itself rather than about any one page's prose.
    """

    html = client.get(path).text
    footer = _footer_text(html)

    assert PROVIDER_NAME in footer
    assert LICENCE_NAME in footer
    assert DATA_SET_MARKER in footer
    assert _licence_href(html).startswith("https://")


def test_about_page_explains_the_licence_in_its_own_content(client: Any) -> None:
    """The about page states where the obligation comes from, in its own words.

    Scoped to the page's content on purpose. An earlier version of this test
    searched the whole response, so it kept passing when the legal citation was
    deleted from ``about.html`` — the footer on every page carries "54/017"
    too, and the test could not tell whose words it was reading.
    """

    content = _own_content_text(client.get("/about").text)

    assert LICENCE_NAME in content
    assert "54/017" in content
    assert "19.355" in content


# ── No drift between what a crawler reads and what a reader clicks ─────────


def test_declared_licence_is_the_one_this_page_links(client: Any) -> None:
    """The ``Dataset`` licence and the visible link are one document, not two.

    Comparing each against its own constant would pass while one of them
    pointed somewhere else entirely. Comparing them against each other is the
    assertion that cannot be satisfied by two copies of the same mistake.
    """

    body = client.get("/").text

    assert _dataset_block(body)["license"] == _licence_href(body)


def test_every_page_links_the_same_licence(client: Any) -> None:
    """One licence across the footer and the about page.

    A site that names two different licences in two places contradicts itself.
    """

    hrefs = {_licence_href(client.get(path).text) for path in FULL_PAGES}

    assert len(hrefs) == 1, hrefs


def test_licence_url_points_at_the_uruguayan_governments_document(
    client: Any,
) -> None:
    """Pinned to the source of the licence, not just to "some URL".

    The exact path may move; the fact that this is the Uruguayan state's own
    publication of its open-data licence may not.
    """

    assert "impo.com.uy" in _dataset_block(client.get("/").text)["license"]
