"""Integration tests for :mod:`scraper.main`.

The module's public surface — :func:`enrich_xml_compra` and
:func:`_compra_dict` — is what stitches the new ``id_ucc`` pipeline
together. These tests verify the fallback-priority contract
documented in the ``compra-normalization`` spec:

1. ``inciso/ue`` wins when mapped.
2. UCC fallback resolves when inciso/ue is unmapped and id_ucc is known.
3. Both lookups miss → final value is the inciso/ue fallback.
4. ``id_ucc is None`` and inciso/ue unknown → inciso/ue fallback only
   (``resolve_ucc_organism`` is NOT consulted).
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from scraper.bcu_client import BcuClient
from scraper.main import _run_scrape_for_day, enrich_xml_compra
from scraper.normalizer import CompraRow
from scraper.persistence import _compra_dict
from scraper.xml_report import XmlAdjudicacion, XmlCompra

# ---------------------------------------------------------------------------
# XmlCompra factory — minimal fields needed to drive enrich_xml_compra
# ---------------------------------------------------------------------------


def _xml_compra(
    *,
    id_compra: str = "1319278",
    id_inciso: int | None = None,
    id_ue: int | None = None,
    id_ucc: int | None = None,
) -> XmlCompra:
    """Build a minimal :class:`XmlCompra` for enrichment tests.

    Only the fields the enrich / normalize path actually reads are
    populated; the rest use type-correct defaults.
    """

    return XmlCompra(
        id_compra=id_compra,
        fecha_pub_adj=date(2024, 1, 15),
        id_tipocompra="CD",
        id_moneda_monto_adj=0,
        objeto=None,
        monto_adj=None,
        num_compra=None,
        anio_compra=None,
        subtipo_compra=None,
        id_inciso=id_inciso,
        id_ue=id_ue,
        id_ucc=id_ucc,
        adjudicaciones=[],
        oferentes=[],
    )


# ---------------------------------------------------------------------------
# Fallback priority — inciso/ue wins over UCC
# ---------------------------------------------------------------------------


def test_enrich_xml_compra_inciso_ue_wins_over_known_ucc() -> None:
    """When (id_inciso, id_ue) is mapped, UCC MUST NOT be consulted.

    (4, 1) → 'Secretaría del Ministerio del Interior' is the canonical
    sample. Adding a known id_ucc alongside it must not change the
    resolved organism.
    """

    compra = _xml_compra(id_inciso=4, id_ue=1, id_ucc=54)  # UCC 54 = UCC Presidencia

    enrichment = enrich_xml_compra(compra, source_url="https://example.test/xml")

    assert enrichment.organism == "Secretaría del Ministerio del Interior"


# ---------------------------------------------------------------------------
# Fallback priority — UCC resolves the Desconocido placeholder
# ---------------------------------------------------------------------------


def test_enrich_xml_compra_ucc_fallback_resolves_desconocido() -> None:
    """When (id_inciso, id_ue) is unmapped, UCC is consulted.

    (99, 99) is NOT in ORGANISM_MAP → resolve_organism returns
    'Desconocido (99-99)'. id_ucc=54 is mapped to 'UCC Presidencia'
    → the fallback chain must produce that value.
    """

    compra = _xml_compra(id_inciso=99, id_ue=99, id_ucc=54)

    enrichment = enrich_xml_compra(compra, source_url="https://example.test/xml")

    assert enrichment.organism == "UCC Presidencia"


def test_enrich_xml_compra_ucc_fallback_handles_unmapped_pair() -> None:
    """An unmapped inciso/ue + a known id_ucc → UCC name (not Desconocido).

    Uses (None, None) for inciso/ue so resolve_organism produces
    'Desconocido (None-None)', and id_ucc=1 (UCAMAE).
    """

    compra = _xml_compra(id_inciso=None, id_ue=None, id_ucc=1)

    enrichment = enrich_xml_compra(compra, source_url="https://example.test/xml")

    assert enrichment.organism == "UCAMAE"


# ---------------------------------------------------------------------------
# Fallback priority — both lookups miss
# ---------------------------------------------------------------------------


def test_enrich_xml_compra_both_lookups_miss_returns_desconocido() -> None:
    """Unmapped inciso/ue + unknown id_ucc → 'Desconocido' (the UCC side).

    resolve_organism returns 'Desconocido (99-99)'; id_ucc=99999 is
    unmapped so resolve_ucc_organism returns 'Desconocido' (overriding
    the inciso/ue placeholder).
    """

    compra = _xml_compra(id_inciso=99, id_ue=99, id_ucc=99999)

    enrichment = enrich_xml_compra(compra, source_url="https://example.test/xml")

    assert enrichment.organism == "Desconocido"


# ---------------------------------------------------------------------------
# Fallback priority — id_ucc absent and inciso/ue unknown
# ---------------------------------------------------------------------------


def test_enrich_xml_compra_no_ucc_keeps_inciso_ue_fallback() -> None:
    """id_ucc=None with unmapped inciso/ue → the (i-u) placeholder is kept.

    resolve_ucc_organism MUST NOT be consulted; the result is the
    inciso/ue 'Desconocido (99-99)' string verbatim.
    """

    compra = _xml_compra(id_inciso=99, id_ue=99, id_ucc=None)

    enrichment = enrich_xml_compra(compra, source_url="https://example.test/xml")

    assert enrichment.organism == "Desconocido (99-99)"


# ---------------------------------------------------------------------------
# Known inciso/ue + absent id_ucc — happy path
# ---------------------------------------------------------------------------


def test_enrich_xml_compra_known_inciso_ue_no_ucc() -> None:
    """Known inciso/ue + id_ucc=None → inciso/ue name, no fallback call."""

    compra = _xml_compra(id_inciso=4, id_ue=1, id_ucc=None)

    enrichment = enrich_xml_compra(compra, source_url="https://example.test/xml")

    assert enrichment.organism == "Secretaría del Ministerio del Interior"


# ---------------------------------------------------------------------------
# License link is built deterministically
# ---------------------------------------------------------------------------


def test_enrich_xml_compra_builds_license_link_from_id_compra() -> None:
    """The ``license_link`` is built from ``id_compra`` — not from UCC."""

    compra = _xml_compra(id_compra="1319278", id_inciso=99, id_ue=99, id_ucc=54)

    enrichment = enrich_xml_compra(compra, source_url="https://example.test/xml")

    assert (
        enrichment.license_link
        == "https://www.comprasestatales.gub.uy/consultas/detalle/id/1319278"
    )


def test_enrich_xml_compra_preserves_source_url() -> None:
    """``source_url`` is passed through unchanged."""

    compra = _xml_compra(id_inciso=4, id_ue=1)

    enrichment = enrich_xml_compra(
        compra, source_url="https://example.test/source?dia=2024-01-15"
    )

    assert enrichment.source_url == "https://example.test/source?dia=2024-01-15"


# ---------------------------------------------------------------------------
# _compra_dict — id_ucc flows from CompraRow
# ---------------------------------------------------------------------------


def _compra_row(
    *,
    id_ucc: int | None = None,
    organismo: str = "Desconocido",
) -> CompraRow:
    """Build a minimal :class:`CompraRow` for the dict-projection test."""

    return CompraRow(
        id_compra="1319278",
        fecha_pub_adj=date(2024, 1, 15),
        id_tipocompra="CD",
        id_moneda_monto_adj=0,
        objeto=None,
        monto_adj=None,
        num_compra=None,
        anio_compra=None,
        subtipo_compra=None,
        id_inciso=None,
        id_ue=None,
        id_ucc=id_ucc,
        organismo=organismo,
        license_link="https://www.comprasestatales.gub.uy/consultas/detalle/id/1319278",
        source_url="https://example.test/xml",
        adjudicaciones=[],
        oferentes=[],
    )


def test_compra_dict_includes_id_ucc_when_present() -> None:
    """``_compra_dict`` MUST carry ``id_ucc`` when the row has it."""

    row = _compra_row(id_ucc=54)

    payload: dict[str, Any] = _compra_dict(row)

    assert payload["id_ucc"] == 54


def test_compra_dict_includes_id_ucc_when_none() -> None:
    """``_compra_dict`` MUST carry ``id_ucc`` as ``None`` when absent."""

    row = _compra_row(id_ucc=None)

    payload: dict[str, Any] = _compra_dict(row)

    assert "id_ucc" in payload
    assert payload["id_ucc"] is None


@pytest.mark.parametrize(
    "id_ucc,expected",
    [
        (1, "UCAMAE"),
        (43, "UCC MSP"),
        (54, "UCC Presidencia"),
        (69, "UCC - ASSE"),
        (75, "UACM - MTOP"),
    ],
)
def test_enrich_xml_compra_ucc_fallback_table_driven(
    id_ucc: int, expected: str
) -> None:
    """Table-driven check: every documented UCC id_ucc maps to its name."""

    compra = _xml_compra(id_inciso=None, id_ue=None, id_ucc=id_ucc)

    enrichment = enrich_xml_compra(compra, source_url="https://example.test/xml")

    assert enrichment.organism == expected


# ---------------------------------------------------------------------------
# Orchestrator error handling
# ---------------------------------------------------------------------------


def _mock_bcu_client() -> BcuClient:
    """Return a BCU client that never needs the real network.

    The orchestrator test uses id_moneda=0 (UYU passthrough), so no SOAP
    request is ever issued.
    """

    def _handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200,
            content=b"<?xml version='1.0'?><root><datos><TCC>1.0</TCC></datos></root>",
        )

    return BcuClient(
        "https://example.test/wsbcucotizaciones",
        client=httpx.Client(transport=httpx.MockTransport(_handler)),
    )


def _xml_compra_with_amount(
    id_compra: str,
    precio_tot_imp: Decimal,
) -> XmlCompra:
    """Build a single-adjudication XmlCompra using id_moneda=0 (UYU)."""

    return XmlCompra(
        id_compra=id_compra,
        fecha_pub_adj=date(2024, 1, 15),
        id_tipocompra="CD",
        id_moneda_monto_adj=0,
        objeto=None,
        monto_adj=None,
        num_compra=None,
        anio_compra=None,
        subtipo_compra=None,
        id_inciso=4,
        id_ue=1,
        id_ucc=None,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra=id_compra,
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("1.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=precio_tot_imp,
                desc_articulo="Laptop",
                id_moneda=0,
                id_articulo="42851",
            )
        ],
        oferentes=[],
    )


def test_run_scrape_for_day_skips_compra_on_normalization_error(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Any,
) -> None:
    """A normalization failure should log a warning and continue processing."""

    import scraper.main as scraper_main

    bad_compra = _xml_compra_with_amount("BAD", Decimal("-100.00"))
    good_compra = _xml_compra_with_amount("GOOD", Decimal("100.00"))

    monkeypatch.setattr(
        scraper_main,
        "fetch_xml_report",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        scraper_main,
        "parse_xml_report",
        lambda _xml_text: [bad_compra, good_compra],
    )

    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200))
    )
    bcu = _mock_bcu_client()

    with caplog.at_level(logging.WARNING, logger="scraper.run_scrape"):
        result = _run_scrape_for_day(
            target_day=date(2024, 1, 15),
            source_a_base_url="https://example.test/xml",
            session=db_session,
            bcu_client=bcu,
            client=http_client,
            start_hour=0,
            end_hour=23,
        )

    assert len(result) == 1
    assert result[0].id_compra == "GOOD"

    warning_messages = [
        record.message for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert any(
        "BAD" in msg and "Normalization failed" in msg for msg in warning_messages
    )
