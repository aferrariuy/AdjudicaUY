"""Unit tests for :mod:`scraper.normalizer`.

The normalizer's job is straightforward — translate an
:class:`XmlCompra` plus a :class:`CompraEnrichment` into a
:class:`CompraRow` whose per-adjudicacion ``amount_uyu`` reflects
the BCU rate for the adjudication date. The test suite is mostly
table-driven: one parametrized case per (id_moneda, mode)
combination the spec defines.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import httpx
import pytest

import scraper.retry as retry_module
from scraper.bcu_client import BcuClient, BcuError
from scraper.normalizer import (
    CONVERSION_TABLE,
    NON_CONVERTIBLE_TABLE,
    PASSTHROUGH_TABLE,
    CompraEnrichment,
    CompraRow,
    ConversionMode,
    CurrencyNotResolvedError,
    MalformedCompraError,
    NormalizationError,
    _resolve_mode,
    normalize_compra,
)
from scraper.xml_report import (
    XmlAdjudicacion,
    XmlCompra,
    XmlOferente,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _static_bcu_client(rate: Decimal | None = Decimal("38.50")) -> BcuClient:
    """Return a :class:`BcuClient` that always returns ``rate`` for any TCC query."""

    def _handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        if rate is None:
            body = b"<?xml version='1.0'?><root><datos></datos></root>"
        else:
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<root><datos>"
                f"<TCC>{rate}</TCC>"
                "</datos></root>"
            ).encode()
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    return BcuClient(
        "https://example.test/wscotizaciones/servlet/wsbcucotizaciones",
        client=client,
    )


def _build_xml_compra(
    *,
    id_compra: str = "1319278",
    fecha_pub_adj: date = date(2024, 1, 15),
    id_moneda_monto_adj: int = 20,
    id_tipocompra: str = "CD",
    monto_adj: Decimal | None = Decimal("1234.56"),
    adjudicaciones: list[XmlAdjudicacion] | None = None,
    oferentes: list[XmlOferente] | None = None,
) -> XmlCompra:
    """Build a minimal :class:`XmlCompra` for normalizer tests.

    Defaults to a single USD adjudication so each test only has to
    override the fields it cares about.
    """

    if adjudicaciones is None:
        adjudicaciones = [
            XmlAdjudicacion(
                id_compra=id_compra,
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("1234.56"),
                desc_articulo="Laptop",
                id_moneda=20,
                id_articulo="42851",
            )
        ]
    if oferentes is None:
        oferentes = []

    return XmlCompra(
        id_compra=id_compra,
        fecha_pub_adj=fecha_pub_adj,
        id_tipocompra=id_tipocompra,
        id_moneda_monto_adj=id_moneda_monto_adj,
        objeto="Adquisición",
        monto_adj=monto_adj,
        num_compra="86825",
        anio_compra="2024",
        subtipo_compra="",
        id_inciso=3,
        id_ue=15,
        id_ucc=None,
        adjudicaciones=adjudicaciones,
        oferentes=oferentes,
    )


def _enrichment() -> CompraEnrichment:
    return CompraEnrichment(
        organism="Ministerio del Interior",
        license_link="https://example.test/consultas/detalle/id/1319278",
        source_url="https://example.test/xml",
    )


# ---------------------------------------------------------------------------
# Mode classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("id_moneda", "expected_mode"),
    [
        # UYU pass-through
        (0, ConversionMode.PASSTHROUGH),
        # Convertible currencies — sample of the full table
        (20, ConversionMode.CONVERT),  # USD cable
        (37, ConversionMode.CONVERT),  # USD cable alt code
        (36, ConversionMode.CONVERT),  # USD billete
        (15, ConversionMode.CONVERT),  # EUR
        (25, ConversionMode.CONVERT),  # BRL
        (8, ConversionMode.CONVERT),  # CAD
        (11, ConversionMode.CONVERT),  # GBP
        (12, ConversionMode.CONVERT),  # JPY
        (17, ConversionMode.CONVERT),  # XDR
        # Non-convertible — no BCU call, amount_uyu = NULL
        (4, ConversionMode.NULL),  # UI
        (5, ConversionMode.NULL),  # UR
        (22, ConversionMode.NULL),  # ORO
        (39, ConversionMode.NULL),  # EURO TRANSF.
        # Unknown — defaults to NULL
        (9999, ConversionMode.NULL),
        (-1, ConversionMode.NULL),
    ],
)
def test_resolve_mode_classifies_each_id_moneda(
    id_moneda: int, expected_mode: ConversionMode
) -> None:
    assert _resolve_mode(id_moneda) is expected_mode


def test_passthrough_currency_ids_match_spec() -> None:
    assert set(PASSTHROUGH_TABLE) == {0}


def test_non_convertible_currency_ids_match_spec() -> None:
    assert set(NON_CONVERTIBLE_TABLE) == {4, 5, 22, 39}


# ---------------------------------------------------------------------------
# UYU pass-through
# ---------------------------------------------------------------------------


def test_normalize_uyu_passes_amount_through_unchanged() -> None:
    """id_moneda=0: amount_uyu equals precio_tot_imp, no BCU call."""

    compra = _build_xml_compra(
        id_moneda_monto_adj=0,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("1234.56"),
                desc_articulo="Laptop",
                id_moneda=0,
                id_articulo="42851",
            )
        ],
    )
    result = normalize_compra(
        compra, _enrichment(), _static_bcu_client(rate=Decimal("38.50"))
    )

    assert result.adjudicaciones[0].currency == "UYU"
    assert result.adjudicaciones[0].amount_uyu == Decimal("1234.56")


def test_normalize_uyu_does_not_call_bcu() -> None:
    """id_moneda=0 MUST short-circuit — no SOAP request is issued."""

    call_log: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        call_log.append(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            content=(
                b"<?xml version='1.0'?><root><datos><TCC>99.99</TCC></datos></root>"
            ),
        )

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    bcu = BcuClient("https://example.test/wsbcucotizaciones", client=client)

    compra = _build_xml_compra(
        id_moneda_monto_adj=0,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("100.00"),
                desc_articulo="Laptop",
                id_moneda=0,
                id_articulo="42851",
            )
        ],
    )
    result = normalize_compra(compra, _enrichment(), bcu)

    assert result.adjudicaciones[0].amount_uyu == Decimal("100.00")
    assert call_log == []  # No BCU request was made.


# ---------------------------------------------------------------------------
# USD conversion — table-driven for the spec scenarios
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("id_moneda", "amount", "rate", "expected_uyu"),
    [
        # The canonical USD scenario from the data-ingestion spec:
        # id_moneda=20, amount=1000, TCC=38.50 → amount_uyu = 38500
        (20, Decimal("1000"), Decimal("38.50"), Decimal("38500.00")),
        # Smaller values keep decimal precision.
        (20, Decimal("12.34"), Decimal("40.00"), Decimal("493.60")),
        # Truncation is ROUND_HALF_UP to two decimal places (column scale).
        # 1.005 * 38.50 = 38.6925 → rounds to 38.69.
        (20, Decimal("1.005"), Decimal("38.50"), Decimal("38.69")),
        # USD billete (id_moneda=36) uses the same conversion path.
        (36, Decimal("500"), Decimal("38.50"), Decimal("19250.00")),
        # Euro (id_moneda=15).
        (15, Decimal("100"), Decimal("42.00"), Decimal("4200.00")),
    ],
)
def test_normalize_converts_foreign_currency_to_uyu(
    id_moneda: int, amount: Decimal, rate: Decimal, expected_uyu: Decimal
) -> None:
    compra = _build_xml_compra(
        id_moneda_monto_adj=id_moneda,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=amount,
                desc_articulo="Laptop",
                id_moneda=id_moneda,
                id_articulo="42851",
            )
        ],
    )
    result = normalize_compra(compra, _enrichment(), _static_bcu_client(rate=rate))
    assert result.adjudicaciones[0].amount_uyu == expected_uyu


def test_normalize_sets_currency_code_for_usd() -> None:
    compra = _build_xml_compra(
        id_moneda_monto_adj=20,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("100"),
                desc_articulo="Laptop",
                id_moneda=20,
                id_articulo="42851",
            )
        ],
    )
    result = normalize_compra(compra, _enrichment(), _static_bcu_client())
    assert result.adjudicaciones[0].currency == "USD"


def test_normalize_sets_currency_code_for_eur() -> None:
    compra = _build_xml_compra(
        id_moneda_monto_adj=15,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("100"),
                desc_articulo="Laptop",
                id_moneda=15,
                id_articulo="42851",
            )
        ],
    )
    result = normalize_compra(compra, _enrichment(), _static_bcu_client())
    assert result.adjudicaciones[0].currency == "EUR"


# ---------------------------------------------------------------------------
# Non-convertible currency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("id_moneda", [4, 5, 22, 39])
def test_normalize_returns_null_amount_uyu_for_non_convertible(
    id_moneda: int,
) -> None:
    compra = _build_xml_compra(
        id_moneda_monto_adj=id_moneda,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("500.00"),
                desc_articulo="Servicio",
                id_moneda=id_moneda,
                id_articulo="42851",
            )
        ],
    )
    result = normalize_compra(compra, _enrichment(), _static_bcu_client())
    assert result.adjudicaciones[0].amount_uyu is None
    # The currency is a 3-letter placeholder (NOT "UYU") so the chart can
    # still group the row.
    assert result.adjudicaciones[0].currency != "UYU"


def test_normalize_non_convertible_does_not_call_bcu() -> None:
    call_log: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        call_log.append(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            content=(
                b"<?xml version='1.0'?><root><datos><TCC>99.99</TCC></datos></root>"
            ),
        )

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    bcu = BcuClient("https://example.test/wsbcucotizaciones", client=client)

    compra = _build_xml_compra(
        id_moneda_monto_adj=4,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("100.00"),
                desc_articulo="Servicio",
                id_moneda=4,
                id_articulo="42851",
            )
        ],
    )
    result = normalize_compra(compra, _enrichment(), bcu)
    assert result.adjudicaciones[0].amount_uyu is None
    assert call_log == []


# ---------------------------------------------------------------------------
# Unknown currency ID
# ---------------------------------------------------------------------------


def test_normalize_unknown_currency_skips_line_and_retains_parent(caplog) -> None:
    """An unresolved ``id_moneda`` skips only that line; the parent survives.

    The unmapped currency is warned with its identifying fields and the
    parent is returned as a parent-only :class:`CompraRow` with all
    parent fields unchanged.
    """

    compra = _build_xml_compra(
        id_moneda_monto_adj=99999,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("100.00"),
                desc_articulo="Laptop",
                id_moneda=99999,
                id_articulo="42851",
            )
        ],
    )

    def _monedas_handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=b"<?xml version='1.0'?><root></root>")

    def _cotizaciones_handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200,
            content=b"<?xml version='1.0'?><root><datos><TCC>1.0</TCC></datos></root>",
        )

    def _route(request: httpx.Request) -> httpx.Response:
        if "awsbcumonedas" in str(request.url):
            return _monedas_handler(request)
        return _cotizaciones_handler(request)

    transport = httpx.MockTransport(_route)
    client = httpx.Client(transport=transport)
    bcu = BcuClient("https://example.test/wsbcucotizaciones", client=client)

    with caplog.at_level(logging.WARNING, logger="scraper.normalizer"):
        result = normalize_compra(compra, _enrichment(), bcu)

    # One parent-only row; the unresolved line is absent.
    assert isinstance(result, CompraRow)
    assert result.id_compra == "1319278"
    assert result.adjudicaciones == []
    # Parent fields are unchanged by the skipped line.
    assert result.monto_adj == Decimal("1234.56")
    assert result.id_moneda_monto_adj == 99999
    assert result.organismo == "Ministerio del Interior"

    warning_messages = [
        record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "scraper.normalizer"
    ]
    assert len(warning_messages) == 1
    msg = warning_messages[0]
    assert "Skipping adjudication" in msg
    assert "id_compra=1319278" in msg
    assert "currency not resolved" in msg
    assert "id_moneda=99999" in msg
    assert "desc_articulo='Laptop'" in msg
    assert "id_articulo='42851'" in msg


# ---------------------------------------------------------------------------
# Provenance fields are passed through unchanged
# ---------------------------------------------------------------------------


def test_normalize_preserves_provenance_fields() -> None:
    """Compra-level enrichment + adjudication fields round-trip cleanly."""

    compra = _build_xml_compra(
        id_compra="7777",
        fecha_pub_adj=date(2024, 6, 1),
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="7777",
                nombre_comercial="Acme",
                nro_doc_prov="210000000077",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("2.00"),
                precio_unit=Decimal("500.00"),
                precio_tot_imp=Decimal("1000.00"),
                desc_articulo="Silla",
                id_moneda=20,
                id_articulo="42851",
            )
        ],
    )
    enrichment = CompraEnrichment(
        organism="OSE",
        license_link="https://example.test/id/7777",
        source_url="https://example.test/source-A",
    )
    result = normalize_compra(
        compra, enrichment, _static_bcu_client(rate=Decimal("40.00"))
    )

    assert result.id_compra == "7777"
    assert result.fecha_pub_adj == date(2024, 6, 1)
    assert result.organismo == "OSE"
    assert result.license_link == "https://example.test/id/7777"
    assert result.source_url == "https://example.test/source-A"
    assert result.id_tipocompra == "CD"
    assert result.adjudicaciones[0].nombre_comercial == "Acme"
    assert result.adjudicaciones[0].nro_doc_prov == "210000000077"
    assert result.adjudicaciones[0].tipo_doc_prov == "RUT"
    assert result.adjudicaciones[0].desc_articulo == "Silla"
    assert result.adjudicaciones[0].cant_adj == Decimal("2.00")
    assert result.adjudicaciones[0].precio_tot_imp == Decimal("1000.00")
    assert result.adjudicaciones[0].id_articulo == "42851"
    assert result.adjudicaciones[0].amount_uyu == Decimal("40000.00")


# ---------------------------------------------------------------------------
# Conversion table is internally consistent
# ---------------------------------------------------------------------------


def test_conversion_table_every_entry_has_bcu_code_and_iso() -> None:
    """Defensive check — keep the table honest in case someone adds a row."""

    for id_moneda, (bcu_code, iso) in CONVERSION_TABLE.items():
        assert isinstance(bcu_code, int)
        assert isinstance(iso, str)
        assert len(iso) >= 2  # ISO 4217 is 3; BCU allows "U.R." (4)
        # The same id_moneda must not appear in any other table.
        assert id_moneda not in PASSTHROUGH_TABLE
        assert id_moneda not in NON_CONVERTIBLE_TABLE


# ---------------------------------------------------------------------------
# Specific normalization exceptions
# ---------------------------------------------------------------------------


def test_normalize_negative_amount_skips_line_and_retains_parent(caplog) -> None:
    """A negative ``precio_tot_imp`` skips only that line; the parent survives."""

    compra = _build_xml_compra(
        id_moneda_monto_adj=20,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("-100.00"),
                desc_articulo="Laptop",
                id_moneda=20,
                id_articulo="42851",
            )
        ],
    )

    with caplog.at_level(logging.WARNING, logger="scraper.normalizer"):
        result = normalize_compra(compra, _enrichment(), _static_bcu_client())

    assert result.id_compra == "1319278"
    assert result.adjudicaciones == []
    assert result.monto_adj == Decimal("1234.56")

    warning_messages = [
        record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "scraper.normalizer"
    ]
    assert len(warning_messages) == 1
    assert "invalid precio_tot_imp" in warning_messages[0]
    assert "id_compra=1319278" in warning_messages[0]
    assert "precio_tot_imp=-100.00" in warning_messages[0]


@pytest.mark.parametrize(
    "precio_tot_imp",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_normalize_non_finite_amount_skips_line_and_retains_parent(
    precio_tot_imp: Decimal,
    caplog,
) -> None:
    """Non-finite ``precio_tot_imp`` values skip the line and keep the parent."""

    compra = _build_xml_compra(
        id_moneda_monto_adj=0,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=precio_tot_imp,
                desc_articulo="Laptop",
                id_moneda=0,
                id_articulo="42851",
            )
        ],
    )

    with caplog.at_level(logging.WARNING, logger="scraper.normalizer"):
        result = normalize_compra(compra, _enrichment(), _static_bcu_client())

    assert result.id_compra == "1319278"
    assert result.adjudicaciones == []
    assert result.monto_adj == Decimal("1234.56")

    warning_messages = [
        record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "scraper.normalizer"
    ]
    assert len(warning_messages) == 1
    assert "invalid precio_tot_imp" in warning_messages[0]


def test_normalize_bcu_failure_from_monedas_lookup_isolates_line_and_retains_parent(
    monkeypatch,
    caplog,
) -> None:
    """A BCU failure resolving an unknown currency skips only that line.

    The failure is isolated at the normalization line boundary: the
    parent is returned as a parent-only row with a warning, while the
    BCU client API itself still raises :class:`BcuError`.
    """

    monkeypatch.setattr(retry_module.time, "sleep", lambda _seconds: None)

    compra = _build_xml_compra(
        id_moneda_monto_adj=99999,
        adjudicaciones=[
            XmlAdjudicacion(
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("100.00"),
                desc_articulo="Laptop",
                id_moneda=99999,
                id_articulo="42851",
            )
        ],
    )

    def _always_503(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(503, text="service unavailable")

    transport = httpx.MockTransport(_always_503)
    client = httpx.Client(transport=transport)
    bcu = BcuClient("https://example.test/wsbcucotizaciones", client=client)

    with caplog.at_level(logging.WARNING, logger="scraper.normalizer"):
        result = normalize_compra(compra, _enrichment(), bcu)

    assert result.id_compra == "1319278"
    assert result.adjudicaciones == []
    assert result.monto_adj == Decimal("1234.56")

    warning_messages = [
        record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "scraper.normalizer"
    ]
    assert len(warning_messages) == 1
    assert "id_compra=1319278" in warning_messages[0]
    assert "BCU/conversion failure" in warning_messages[0]

    # The client API still raises inside the line boundary — the
    # isolation happens in the normalizer, not in the BCU client.
    with pytest.raises(BcuError):
        bcu.list_monedas()


# ---------------------------------------------------------------------------
# Per-adjudication isolation — sibling retention
# ---------------------------------------------------------------------------


def test_normalize_mixed_valid_invalid_siblings_keeps_only_valid_line(
    caplog,
) -> None:
    """A valid sibling survives when a sibling line in the same compra is invalid."""

    compra = _build_xml_compra(
        id_moneda_monto_adj=20,
        adjudicaciones=[
            XmlAdjudicacion(  # invalid: negative amount
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("-100.00"),
                desc_articulo="Laptop",
                id_moneda=20,
                id_articulo="42851",
            ),
            XmlAdjudicacion(  # valid: 1000 USD at 38.50 → 38500.00 UYU
                id_compra="1319278",
                nombre_comercial="Proveedor SRL",
                nro_doc_prov="210000000077",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("2.00"),
                precio_unit=Decimal("500.00"),
                precio_tot_imp=Decimal("1000.00"),
                desc_articulo="Silla",
                id_moneda=20,
                id_articulo="42851",
            ),
        ],
    )

    with caplog.at_level(logging.WARNING, logger="scraper.normalizer"):
        result = normalize_compra(
            compra, _enrichment(), _static_bcu_client(rate=Decimal("38.50"))
        )

    assert len(result.adjudicaciones) == 1
    survivor = result.adjudicaciones[0]
    assert survivor.nombre_comercial == "Proveedor SRL"
    assert survivor.precio_tot_imp == Decimal("1000.00")
    assert survivor.amount_uyu == Decimal("38500.00")
    assert result.monto_adj == Decimal("1234.56")

    warning_messages = [
        record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "scraper.normalizer"
    ]
    assert len(warning_messages) == 1
    assert "id_compra=1319278" in warning_messages[0]
    assert "invalid precio_tot_imp" in warning_messages[0]
    assert "nombre_comercial='Empresa SA'" in warning_messages[0]


def test_normalize_all_invalid_children_retains_parent_with_oferentes(
    caplog,
) -> None:
    """Zero surviving adjudications still yields a parent-only row with oferentes.

    Each skipped line emits its own warning; no placeholder adjudication
    is fabricated.
    """

    oferentes = [
        XmlOferente(
            id_compra="1319278",
            nombre_comercial="Empresa SA",
            nro_doc_prov="210000000018",
            tipo_doc_prov="RUT",
            cant_ofertada=Decimal("10.00"),
            precio_unit_ofertado=Decimal("100.00"),
            id_moneda=20,
            variacion=None,
            alternativas=None,
        )
    ]
    compra = _build_xml_compra(
        id_moneda_monto_adj=99999,
        adjudicaciones=[
            XmlAdjudicacion(  # invalid: negative amount
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("-100.00"),
                desc_articulo="Laptop",
                id_moneda=0,
                id_articulo="42851",
            ),
            XmlAdjudicacion(  # invalid: unresolved currency
                id_compra="1319278",
                nombre_comercial="Otra SA",
                nro_doc_prov="210000000099",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("1.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("100.00"),
                desc_articulo="Servicio",
                id_moneda=99999,
                id_articulo="42851",
            ),
        ],
        oferentes=oferentes,
    )

    def _monedas_handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=b"<?xml version='1.0'?><root></root>")

    def _cotizaciones_handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200,
            content=b"<?xml version='1.0'?><root><datos><TCC>1.0</TCC></datos></root>",
        )

    def _route(request: httpx.Request) -> httpx.Response:
        if "awsbcumonedas" in str(request.url):
            return _monedas_handler(request)
        return _cotizaciones_handler(request)

    transport = httpx.MockTransport(_route)
    client = httpx.Client(transport=transport)
    bcu = BcuClient("https://example.test/wsbcucotizaciones", client=client)

    with caplog.at_level(logging.WARNING, logger="scraper.normalizer"):
        result = normalize_compra(compra, _enrichment(), bcu)

    assert result.id_compra == "1319278"
    assert result.adjudicaciones == []
    assert result.monto_adj == Decimal("1234.56")
    assert len(result.oferentes) == 1
    assert result.oferentes[0].nombre_comercial == "Empresa SA"

    warning_messages = [
        record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "scraper.normalizer"
    ]
    assert len(warning_messages) == 2
    assert any("invalid precio_tot_imp" in msg for msg in warning_messages)
    assert any("currency not resolved" in msg for msg in warning_messages)


def test_normalize_monto_adj_verbatim_when_it_differs_from_surviving_totals(
    caplog,
) -> None:
    """XML ``monto_adj`` is persisted verbatim, never recomputed from children.

    The parent total deliberately differs from the surviving child's
    converted amount — the stored value must stay exactly the XML value.
    """

    compra = _build_xml_compra(
        id_moneda_monto_adj=20,
        monto_adj=Decimal("999.99"),
        adjudicaciones=[
            XmlAdjudicacion(  # invalid: skipped line
                id_compra="1319278",
                nombre_comercial="Empresa SA",
                nro_doc_prov="210000000018",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("10.00"),
                precio_unit=Decimal("100.00"),
                precio_tot_imp=Decimal("-100.00"),
                desc_articulo="Laptop",
                id_moneda=20,
                id_articulo="42851",
            ),
            XmlAdjudicacion(  # valid: 1000 USD at 38.50 → 38500.00 UYU
                id_compra="1319278",
                nombre_comercial="Proveedor SRL",
                nro_doc_prov="210000000077",
                tipo_doc_prov="RUT",
                cant_adj=Decimal("2.00"),
                precio_unit=Decimal("500.00"),
                precio_tot_imp=Decimal("1000.00"),
                desc_articulo="Silla",
                id_moneda=20,
                id_articulo="42851",
            ),
        ],
    )

    with caplog.at_level(logging.WARNING, logger="scraper.normalizer"):
        result = normalize_compra(
            compra, _enrichment(), _static_bcu_client(rate=Decimal("38.50"))
        )

    # The XML total survives verbatim — no recomputation from children.
    assert result.monto_adj == Decimal("999.99")
    assert len(result.adjudicaciones) == 1
    assert result.adjudicaciones[0].amount_uyu == Decimal("38500.00")

    warning_messages = [
        record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "scraper.normalizer"
    ]
    assert len(warning_messages) == 1


def test_normalize_parent_level_failure_still_raises_malformed_compra(
    monkeypatch,
) -> None:
    """A parent-construction failure still raises ``MalformedCompraError``.

    Parent assembly happens outside the per-line catch: a failure while
    building the parent row must propagate and never be converted into a
    line warning or a partial parent-only row.
    """

    def _boom(*_args: object, **_kwargs: object) -> CompraRow:
        raise MalformedCompraError("parent row construction failed")

    monkeypatch.setattr("scraper.normalizer.CompraRow", _boom)

    compra = _build_xml_compra()  # one valid USD adjudication

    with pytest.raises(MalformedCompraError, match="parent row construction failed"):
        normalize_compra(compra, _enrichment(), _static_bcu_client())


# ---------------------------------------------------------------------------
# NormalizationError hierarchy
# ---------------------------------------------------------------------------


def test_normalization_errors_are_catchable_by_base_class() -> None:
    """Both concrete errors inherit from ``NormalizationError``."""

    assert issubclass(CurrencyNotResolvedError, NormalizationError)
    assert issubclass(MalformedCompraError, NormalizationError)
