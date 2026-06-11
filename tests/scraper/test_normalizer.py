"""Unit tests for :mod:`scraper.normalizer`.

The normalizer's job is straightforward — translate a :class:`JoinedRecord`
with a procurement ``id_moneda`` into a :class:`NormalizedRecord` whose
``amount_uyu`` reflects the BCU rate for the adjudication date. The test
suite is mostly table-driven: one parametrized case per (id_moneda, mode)
combination the spec defines.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from scraper.bcu_client import BcuClient
from scraper.normalizer import (
    CONVERSION_TABLE,
    NON_CONVERTIBLE_TABLE,
    PASSTHROUGH_TABLE,
    ConversionMode,
    _resolve_mode,
    normalize_record,
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
            ).encode("utf-8")
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    return BcuClient(
        "https://example.test/wscotizaciones/servlet/wsbcucotizaciones",
        client=client,
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
        (20, ConversionMode.CONVERT),   # USD cable
        (37, ConversionMode.CONVERT),   # USD cable alt code
        (36, ConversionMode.CONVERT),   # USD billete
        (15, ConversionMode.CONVERT),   # EUR
        (25, ConversionMode.CONVERT),   # BRL
        (8, ConversionMode.CONVERT),    # CAD
        (11, ConversionMode.CONVERT),   # GBP
        (12, ConversionMode.CONVERT),   # JPY
        (17, ConversionMode.CONVERT),   # XDR
        # Non-convertible — no BCU call, amount_uyu = NULL
        (4, ConversionMode.NULL),   # UI
        (5, ConversionMode.NULL),   # UR
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


def test_normalize_uyu_passes_amount_through_unchanged(make_joined_record) -> None:
    record = make_joined_record(id_moneda=0, precio_tot_imp=Decimal("1234.56"))

    result = normalize_record(record, _static_bcu_client(rate=Decimal("38.50")))

    assert result.currency == "UYU"
    assert result.amount_uyu == Decimal("1234.56")


def test_normalize_uyu_does_not_call_bcu(make_joined_record) -> None:
    """id_moneda=0 MUST short-circuit — no SOAP request is issued."""

    call_log: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        call_log.append(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            content=b"<?xml version='1.0'?><root><datos><TCC>99.99</TCC></datos></root>",
        )

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    bcu = BcuClient("https://example.test/wsbcucotizaciones", client=client)

    record = make_joined_record(id_moneda=0, precio_tot_imp=Decimal("100.00"))
    result = normalize_record(record, bcu)

    assert result.amount_uyu == Decimal("100.00")
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
    make_joined_record, id_moneda: int, amount: Decimal, rate: Decimal, expected_uyu: Decimal
) -> None:
    record = make_joined_record(id_moneda=id_moneda, precio_tot_imp=amount)

    result = normalize_record(record, _static_bcu_client(rate=rate))

    assert result.amount_uyu == expected_uyu


def test_normalize_sets_currency_code_for_usd(make_joined_record) -> None:
    record = make_joined_record(id_moneda=20, precio_tot_imp=Decimal("100"))

    result = normalize_record(record, _static_bcu_client())

    assert result.currency == "USD"


def test_normalize_sets_currency_code_for_eur(make_joined_record) -> None:
    record = make_joined_record(id_moneda=15, precio_tot_imp=Decimal("100"))

    result = normalize_record(record, _static_bcu_client())

    assert result.currency == "EUR"


# ---------------------------------------------------------------------------
# Non-convertible currency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("id_moneda", [4, 5, 22, 39])
def test_normalize_returns_null_amount_uyu_for_non_convertible(
    make_joined_record, id_moneda: int
) -> None:
    record = make_joined_record(id_moneda=id_moneda, precio_tot_imp=Decimal("500.00"))

    result = normalize_record(record, _static_bcu_client())

    assert result.amount_uyu is None
    # The currency is a 3-letter placeholder (NOT "UYU") so the chart can
    # still group the row.
    assert result.currency != "UYU"


def test_normalize_non_convertible_does_not_call_bcu(make_joined_record) -> None:
    call_log: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        call_log.append(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            content=b"<?xml version='1.0'?><root><datos><TCC>99.99</TCC></datos></root>",
        )

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    bcu = BcuClient("https://example.test/wsbcucotizaciones", client=client)

    record = make_joined_record(id_moneda=4, precio_tot_imp=Decimal("100.00"))
    result = normalize_record(record, bcu)

    assert result.amount_uyu is None
    assert call_log == []


# ---------------------------------------------------------------------------
# Unknown currency ID
# ---------------------------------------------------------------------------


def test_normalize_unknown_currency_returns_null_and_warns(
    make_joined_record, caplog
) -> None:
    record = make_joined_record(id_moneda=99999, precio_tot_imp=Decimal("100.00"))

    # The lookup first tries the static tables, then the BCU monedas endpoint.
    # Both miss for an unknown code, so a warning MUST be logged and
    # ``amount_uyu`` MUST be NULL.
    def _monedas_handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        # Empty monedas list — no resolution.
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

    with caplog.at_level("WARNING", logger="scraper.normalizer"):
        result = normalize_record(record, bcu)

    assert result.amount_uyu is None
    # The fallback display code signals "unknown" to the user.
    assert result.currency == "UNK"
    assert any("Unknown id_moneda=99999" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Provenance fields are passed through unchanged
# ---------------------------------------------------------------------------


def test_normalize_preserves_provenance_fields(make_joined_record) -> None:
    record = make_joined_record(
        id_compra="7777",
        fecha_pub_adj=date(2024, 6, 1),
        organism="OSE",
        license_link="https://example.test/id/7777",
        source_url="https://example.test/source-A",
        nombre_comercial="Acme",
        nro_doc_prov="210000000077",
        tipo_doc_prov="RUT",
        desc_articulo="Silla",
        id_tipocompra="CD",
        cant_adj=Decimal("2.00"),
        id_moneda=20,
        precio_tot_imp=Decimal("1000.00"),
    )

    result = normalize_record(record, _static_bcu_client(rate=Decimal("40.00")))

    assert result.id_compra == "7777"
    assert result.date == date(2024, 6, 1)
    assert result.organism == "OSE"
    assert result.license_link == "https://example.test/id/7777"
    assert result.source_url == "https://example.test/source-A"
    assert result.winning_company == "Acme"
    assert result.company_document == "210000000077"
    assert result.company_document_type == "RUT"
    assert result.article == "Silla"
    assert result.license_type == "CD"
    assert result.article_quantity == Decimal("2.00")
    assert result.amount == Decimal("1000.00")
    assert result.amount_uyu == Decimal("40000.00")


# ---------------------------------------------------------------------------
# Conversion table is internally consistent
# ---------------------------------------------------------------------------


def test_conversion_table_every_entry_has_bcu_code_and_iso() -> None:
    """Defensive check — keep the table honest in case someone adds a row."""

    for id_moneda, (bcu_code, iso) in CONVERSION_TABLE.items():
        assert isinstance(bcu_code, int)
        assert isinstance(iso, str)
        assert len(iso) == 3
        # The same id_moneda must not appear in any other table.
        assert id_moneda not in PASSTHROUGH_TABLE
        assert id_moneda not in NON_CONVERTIBLE_TABLE
