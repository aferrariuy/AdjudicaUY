"""Unit tests for :mod:`scraper.xml_report`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from scraper.xml_report import fetch_xml_report, parse_xml_report

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
                    id_moneda="20" />  <!-- missing nro_doc_prov intentionally omitted? no — it stays -->
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


def test_parse_valid_xml_returns_one_record_per_adjudicacion() -> None:
    records = list(parse_xml_report(VALID_XML))

    # Two ``<compra>`` blocks; the first has 2 adjudicaciones, the second has 1.
    assert len(records) == 3
    companies = [r.nombre_comercial for r in records]
    assert companies == ["Empresa SA", "Otra Empresa", "Tercera Empresa"]


def test_parse_valid_xml_extracts_all_eight_fields() -> None:
    records = list(parse_xml_report(VALID_XML))
    first = records[0]

    assert first.id_compra == "1319278"
    assert first.fecha_pub_adj == date(2024, 1, 15)
    assert first.id_tipocompra == "CD"
    assert first.id_moneda_monto_adj == 20
    assert first.nombre_comercial == "Empresa SA"
    assert first.nro_doc_prov == "210000000018"
    assert first.tipo_doc_prov == "RUT"
    assert first.cant_adj == Decimal("10.00")
    assert first.precio_tot_imp == Decimal("1234.56")
    assert first.desc_articulo == "Laptop Dell"
    assert first.id_moneda == 20


def test_parse_valid_xml_preserves_uyu_record() -> None:
    records = list(parse_xml_report(VALID_XML))
    uyu_record = records[2]

    assert uyu_record.id_moneda == 0
    assert uyu_record.precio_tot_imp == Decimal("99999.99")
    assert uyu_record.fecha_pub_adj == date(2024, 2, 20)


# ---------------------------------------------------------------------------
# Malformed payloads — must not raise
# ---------------------------------------------------------------------------


def test_parse_malformed_xml_returns_empty_iterable() -> None:
    # An empty generator is a valid response — the orchestrator can
    # treat it as "nothing to ingest" without crashing.
    records = list(parse_xml_report(UNBALANCED_XML))
    assert records == []


def test_parse_empty_string_returns_empty_iterable() -> None:
    assert list(parse_xml_report("")) == []


def test_parse_xml_with_partial_invalid_block_still_keeps_valid_blocks() -> None:
    records = list(parse_xml_report(MIXED_XML))
    assert len(records) == 1
    assert records[0].id_compra == "1319281"
    assert records[0].nombre_comercial == "Empresa Valida"


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
    records = list(parse_xml_report(payload))
    assert [r.fecha_pub_adj for r in records] == [
        date(2024, 1, 15),
        date(2024, 2, 15),
        date(2024, 3, 20),
    ]


# ---------------------------------------------------------------------------
# Network failure
# ---------------------------------------------------------------------------


def test_fetch_xml_report_propagates_http_errors() -> None:
    """HTTP errors are NOT swallowed by the parser; the orchestrator handles them.

    The parser receives the raw text from the fetcher. ``fetch_xml_report``
    must surface a transport failure so the worker can log it and abort
    the run (see :mod:`scraper.main`).
    """

    import httpx

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
