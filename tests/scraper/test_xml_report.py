"""Unit tests for :mod:`scraper.xml_report`."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import httpx
import pytest

from scraper.xml_report import (
    XmlCompra,
    fetch_xml_report,
    parse_xml_report,
)

# ---------------------------------------------------------------------------
# Fixture XML payloads
# ---------------------------------------------------------------------------

VALID_XML = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1319278"
          fecha_pub_adj="2024-01-15"
          id_moneda_monto_adj="20"
          id_tipocompra="CD">
    <adjudicaciones>
      <adjudicacion nombre_comercial="Empresa SA"
                    nro_doc_prov="210000000018"
                    tipo_doc_prov="RUT"
                    cant_adj="10.00"
                    precio_tot_imp="1234.56"
                    desc_articulo="Laptop Dell"
                    id_moneda="20" />
      <adjudicacion nombre_comercial="Otra Empresa"
                    nro_doc_prov="210000000099"
                    tipo_doc_prov="RUT"
                    cant_adj="5.00"
                    precio_tot_imp="500.00"
                    desc_articulo="Monitor"
                    id_moneda="20" />
    </adjudicaciones>
    <oferentes>
      <oferente nombre_comercial="Bidder A"
                nro_doc_prov="210000000050"
                tipo_doc_prov="RUT"
                cant_ofertada="15"
                precio_unit_ofertado="80.00"
                id_moneda="20" />
    </oferentes>
  </compra>
  <compra id_compra="1319279"
          fecha_pub_adj="2024-02-20"
          id_moneda_monto_adj="0"
          id_tipocompra="LP">
    <adjudicaciones>
      <adjudicacion nombre_comercial="Tercera Empresa"
                    nro_doc_prov="210000000077"
                    tipo_doc_prov="RUT"
                    cant_adj="100.00"
                    precio_tot_imp="99999.99"
                    desc_articulo="Servicio de limpieza"
                    id_moneda="0" />
    </adjudicaciones>
  </compra>
</reporte>
"""

# Malformed at the root level — the parser should swallow the syntax error
# and yield nothing (no records), rather than raising.
MALFORMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1319280"
          fecha_pub_adj="2024-03-10"
          id_moneda_monto_adj="20">
    <adjudicaciones>
      <adjudicacion nombre_comercial="Empresa"
                    precio_tot_imp="100.00"
                    desc_articulo="Laptop"
                    id_moneda="20" />
    </adjudicaciones>
  </compra>
"""

# Truly malformed: unbalanced tags. Must NOT raise; must yield nothing.
UNBALANCED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="9999">
    <adjudicaciones>
      <adjudicacion nombre_comercial="X" precio_tot_imp="1.00" desc_articulo="Y" id_moneda="0">
    </adjudicaciones>
"""

# Mixed: one well-formed ``<compra>`` and one with invalid data.
# The valid block must still come through.
MIXED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1319281"
          fecha_pub_adj="2024-04-01"
          id_moneda_monto_adj="20"
          id_tipocompra="CD">
    <adjudicaciones>
      <adjudicacion nombre_comercial="Empresa Valida"
                    precio_tot_imp="200.00"
                    desc_articulo="Teclado"
                    id_moneda="20" />
    </adjudicaciones>
  </compra>
  <compra id_compra=""
          fecha_pub_adj="not-a-date"
          id_moneda_monto_adj="20">
    <adjudicaciones>
      <adjudicacion nombre_comercial="X"
                    precio_tot_imp="100.00"
                    desc_articulo="Y"
                    id_moneda="20" />
    </adjudicaciones>
  </compra>
</reporte>
"""


# ---------------------------------------------------------------------------
# Valid payload
# ---------------------------------------------------------------------------


def test_parse_valid_xml_yields_one_compra_per_block() -> None:
    compras = list(parse_xml_report(VALID_XML))
    assert len(compras) == 2
    # First compra carries two adjudicaciones + one oferente.
    first, second = compras
    assert len(first.adjudicaciones) == 2
    assert len(first.oferentes) == 1
    # Second compra has one adjudicacion and no oferentes.
    assert len(second.adjudicaciones) == 1
    assert second.oferentes == []


def test_parse_valid_xml_extracts_compra_attributes() -> None:
    compras = list(parse_xml_report(VALID_XML))
    first = compras[0]

    assert first.id_compra == "1319278"
    assert first.fecha_pub_adj == date(2024, 1, 15)
    assert first.id_tipocompra == "CD"
    assert first.id_moneda_monto_adj == 20


def test_parse_valid_xml_extracts_adjudicacion_attributes() -> None:
    compras = list(parse_xml_report(VALID_XML))
    first = compras[0]
    adj = first.adjudicaciones[0]

    assert adj.id_compra == "1319278"
    assert adj.nombre_comercial == "Empresa SA"
    assert adj.nro_doc_prov == "210000000018"
    assert adj.tipo_doc_prov == "RUT"
    assert adj.cant_adj == Decimal("10.00")
    assert adj.precio_tot_imp == Decimal("1234.56")
    assert adj.desc_articulo == "Laptop Dell"
    assert adj.id_moneda == 20


def test_parse_valid_xml_extracts_oferente_attributes() -> None:
    compras = list(parse_xml_report(VALID_XML))
    first = compras[0]
    of = first.oferentes[0]

    assert of.id_compra == "1319278"
    assert of.nombre_comercial == "Bidder A"
    assert of.nro_doc_prov == "210000000050"
    assert of.cant_ofertada == Decimal("15")
    assert of.precio_unit_ofertado == Decimal("80.00")
    assert of.id_moneda == 20


def test_parse_valid_xml_preserves_uyu_compra() -> None:
    compras = list(parse_xml_report(VALID_XML))
    second = compras[1]

    assert second.id_moneda_monto_adj == 0
    assert second.adjudicaciones[0].id_moneda == 0
    assert second.adjudicaciones[0].precio_tot_imp == Decimal("99999.99")
    assert second.fecha_pub_adj == date(2024, 2, 20)


# ---------------------------------------------------------------------------
# Oferente tests
# ---------------------------------------------------------------------------


XML_NO_OFERENTES = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1" fecha_pub_adj="2024-01-01" id_moneda_monto_adj="0">
    <adjudicaciones>
      <adjudicacion nombre_comercial="A" precio_tot_imp="1.00" desc_articulo="x" id_moneda="0" />
    </adjudicaciones>
    <oferentes/>
  </compra>
</reporte>
"""


def test_parse_xml_with_empty_oferentes_returns_empty_list() -> None:
    compras = list(parse_xml_report(XML_NO_OFERENTES))
    assert len(compras) == 1
    assert compras[0].oferentes == []


XML_MULTI_OFERENTES = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1" fecha_pub_adj="2024-01-01" id_moneda_monto_adj="0">
    <adjudicaciones>
      <adjudicacion nombre_comercial="A" precio_tot_imp="1.00" desc_articulo="x" id_moneda="0" />
    </adjudicaciones>
    <oferentes>
      <oferente nombre_comercial="O1" />
      <oferente nombre_comercial="O2" />
      <oferente nombre_comercial="O3" />
    </oferentes>
  </compra>
</reporte>
"""


def test_parse_xml_with_multiple_oferentes_returns_three_records() -> None:
    compras = list(parse_xml_report(XML_MULTI_OFERENTES))
    assert len(compras) == 1
    ofs = compras[0].oferentes
    assert [o.nombre_comercial for o in ofs] == ["O1", "O2", "O3"]


# ---------------------------------------------------------------------------
# Organism-lookup attribute extraction
# ---------------------------------------------------------------------------


# Payload carrying both ``id_inciso`` and ``id_ue`` — the happy path
# for the new organism-lookup pipeline (see ``organism-lookup`` spec,
# "Both attributes present" scenario).
XML_WITH_INCISO_UE = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1319278"
          fecha_pub_adj="2024-01-15"
          id_moneda_monto_adj="20"
          id_tipocompra="CD"
          id_inciso="3"
          id_ue="15">
    <adjudicaciones>
      <adjudicacion nombre_comercial="Empresa SA"
                    nro_doc_prov="210000000018"
                    tipo_doc_prov="RUT"
                    cant_adj="10.00"
                    precio_tot_imp="1234.56"
                    desc_articulo="Laptop Dell"
                    id_moneda="20" />
    </adjudicaciones>
  </compra>
</reporte>
"""


def test_parse_xml_extracts_id_inciso_and_id_ue() -> None:
    """``<compra>`` attributes must surface as ints on the parsed record."""

    compras = list(parse_xml_report(XML_WITH_INCISO_UE))
    assert len(compras) == 1
    record = compras[0]
    assert record.id_inciso == 3
    assert record.id_ue == 15


def test_parse_xml_returns_none_for_missing_id_inciso_and_id_ue() -> None:
    """Absent attributes must round-trip as ``None`` (never raise)."""

    compras = list(parse_xml_report(VALID_XML))
    # ``VALID_XML`` doesn't carry id_inciso/id_ue on any ``<compra>``.
    assert all(c.id_inciso is None for c in compras)
    assert all(c.id_ue is None for c in compras)


def test_parse_xml_handles_partially_missing_organism_attributes() -> None:
    """Only one of id_inciso / id_ue present → the other is ``None``."""

    payload = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1319278"
          fecha_pub_adj="2024-01-15"
          id_moneda_monto_adj="20"
          id_tipocompra="CD"
          id_inciso="3">
    <adjudicaciones>
      <adjudicacion nombre_comercial="A"
                    precio_tot_imp="10.00"
                    desc_articulo="x"
                    id_moneda="0" />
    </adjudicaciones>
  </compra>
</reporte>
"""
    compras = list(parse_xml_report(payload))
    assert len(compras) == 1
    assert compras[0].id_inciso == 3
    assert compras[0].id_ue is None


def test_parse_xml_treats_non_numeric_organism_attributes_as_none() -> None:
    """Garbage values for id_inciso / id_ue must NOT abort the record."""

    payload = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1319278"
          fecha_pub_adj="2024-01-15"
          id_moneda_monto_adj="20"
          id_tipocompra="CD"
          id_inciso="not-a-number"
          id_ue="also-not">
    <adjudicaciones>
      <adjudicacion nombre_comercial="A"
                    precio_tot_imp="10.00"
                    desc_articulo="x"
                    id_moneda="0" />
    </adjudicaciones>
  </compra>
</reporte>
"""
    compras = list(parse_xml_report(payload))
    assert len(compras) == 1
    assert compras[0].id_inciso is None
    assert compras[0].id_ue is None


# ---------------------------------------------------------------------------
# Malformed payloads — must not raise
# ---------------------------------------------------------------------------


def test_parse_malformed_xml_returns_empty_iterable() -> None:
    records = list(parse_xml_report(UNBALANCED_XML))
    assert records == []


def test_parse_empty_string_returns_empty_iterable() -> None:
    assert list(parse_xml_report("")) == []


def test_parse_xml_with_partial_invalid_block_still_keeps_valid_blocks() -> None:
    """The well-formed compra comes through; the malformed one is skipped."""

    compras = list(parse_xml_report(MIXED_XML))
    assert len(compras) == 1
    assert compras[0].id_compra == "1319281"
    assert compras[0].adjudicaciones[0].nombre_comercial == "Empresa Valida"


# ---------------------------------------------------------------------------
# Date parsing edge cases
# ---------------------------------------------------------------------------


def test_parse_xml_supports_alternative_date_formats() -> None:
    """The parser must accept the formats declared in the source-of-truth list."""

    payload = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1" fecha_pub_adj="15/01/2024" id_moneda_monto_adj="0">
    <adjudicaciones>
      <adjudicacion nombre_comercial="A" precio_tot_imp="10.00" desc_articulo="x" id_moneda="0" />
    </adjudicaciones>
  </compra>
  <compra id_compra="2" fecha_pub_adj="2024/02/15" id_moneda_monto_adj="0">
    <adjudicaciones>
      <adjudicacion nombre_comercial="B" precio_tot_imp="20.00" desc_articulo="y" id_moneda="0" />
    </adjudicaciones>
  </compra>
  <compra id_compra="3" fecha_pub_adj="20-03-2024" id_moneda_monto_adj="0">
    <adjudicaciones>
      <adjudicacion nombre_comercial="C" precio_tot_imp="30.00" desc_articulo="z" id_moneda="0" />
    </adjudicaciones>
  </compra>
</reporte>
"""
    compras = list(parse_xml_report(payload))
    assert [c.fecha_pub_adj for c in compras] == [
        date(2024, 1, 15),
        date(2024, 2, 15),
        date(2024, 3, 20),
    ]


# ---------------------------------------------------------------------------
# Network failure
# ---------------------------------------------------------------------------


def test_fetch_xml_report_propagates_http_errors() -> None:
    """HTTP errors are NOT swallowed by the parser; the orchestrator handles them."""

    class _BoomClient:
        def get(self, url: str, **kwargs):  # noqa: ARG002
            request = httpx.Request("GET", url)
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_xml_report("https://example.test/xml", client=_BoomClient())


def test_fetch_xml_report_returns_body_on_success() -> None:
    class _OkClient:
        def get(self, url: str, **kwargs):  # noqa: ARG002
            request = httpx.Request("GET", url)
            return httpx.Response(200, request=request, text=VALID_XML)

    body = fetch_xml_report("https://example.test/xml", client=_OkClient())
    assert body == VALID_XML


# ---------------------------------------------------------------------------
# Unknown attribute warning
# ---------------------------------------------------------------------------


def test_parse_xml_logs_warning_for_unknown_attribute(caplog) -> None:
    """Unknown attributes on compra/adjudicacion/oferente log a WARNING."""

    payload = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1" fecha_pub_adj="2024-01-15" id_moneda_monto_adj="0"
          nuevo_campo_compra="foo">
    <adjudicaciones>
      <adjudicacion nombre_comercial="A" precio_tot_imp="10.00"
                    desc_articulo="x" id_moneda="0"
                    nuevo_campo_adj="bar" />
    </adjudicaciones>
    <oferentes>
      <oferente nombre_comercial="B" nuevo_campo_of="baz" />
    </oferentes>
  </compra>
</reporte>
"""
    with caplog.at_level(logging.WARNING, logger="scraper.xml_report"):
        compras = list(parse_xml_report(payload))

    # All three records still come through.
    assert len(compras) == 1
    assert compras[0].id_compra == "1"

    # Three WARNING lines — one per element.
    msgs = [record.message for record in caplog.records]
    assert any("nuevo_campo_compra" in m for m in msgs)
    assert any("nuevo_campo_adj" in m for m in msgs)
    assert any("nuevo_campo_of" in m for m in msgs)


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


def test_all_three_dataclasses_are_importable() -> None:
    """The spec requires XmlCompra, XmlAdjudicacion, XmlOferente to be importable."""

    assert XmlCompra is not None
    # XmlAdjudicacion and XmlOferente are also importable from the
    # module (they are referenced by the test fixtures above; this
    # assertion is here to make the spec requirement explicit).
    from scraper.xml_report import XmlAdjudicacion, XmlOferente  # noqa: F401

    assert XmlAdjudicacion is not None
    assert XmlOferente is not None
