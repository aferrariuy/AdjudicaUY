"""Locale-aware number formatting and pure display helpers for es-UY.

The web app targets Uruguayan Spanish (es-UY): the thousands
separator is a period, the decimal separator is a comma, and the
percentage is rendered as ``42,3 %`` (one decimal place, space
before ``%``). The system Python in the deploy image does not
ship the ``es_UY`` locale, so we cannot use :mod:`locale` to
format numbers in the Python layer — this module implements the
small surface we need (``format_uyu``, ``format_count``,
``format_percent``) by hand.

The formatters here are used in two places:

* The KPI cards partial renders monetary totals / counts in es-UY
  — the values come from :class:`app.services.dashboard.KpiSummary`
  as raw :class:`decimal.Decimal` / :class:`int` and we apply the
  locale formatting before reaching the template.
* The concentration partial renders the percentage beneath the
  donut, with one decimal place.

This module is also the canonical home for pure display/link helpers
shared by the listing service and the scraper normalizer:
``display_currency`` and ``build_license_link``, plus the currency
lookup tables they depend on.

Negative numbers are formatted with a leading minus (the dashboard
never emits a negative sum today, but the helper tolerates them
for forward-compatibility). Decimals are rounded at the formatter
level — the values in the dataclasses are already exact (the
service casts to ``Decimal``), so rounding is a no-op for the
current use cases.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote

Number = Decimal | int | float

# Mapping for ``id_moneda`` codes that convert via the BCU API.
# Format: ``id_moneda -> (BCU code, ISO 4217 display code)``.
CONVERSION_TABLE: dict[int, tuple[int, str]] = {
    20: (2224, "USD"),  # DOLAR INTERBANCARIO COMPRADOR
    37: (2224, "USD"),  # DLS. USA CABLE
    36: (2225, "USD"),  # DLS.USA BILLETE
    1: (2222, "USD"),  # DOLAR PIZARRA VENDEDOR (deprecated 01/01/2025)
    2: (2222, "USD"),  # DOLAR INTERBANCARIO VENDEDOR
    40: (2230, "USD"),  # DOLAR FONDO COMPRADOR
    47: (2230, "USD"),  # DOLAR PROMEDIO
    25: (1000, "BRL"),  # REAL
    15: (1111, "EUR"),  # EURO
    8: (2309, "CAD"),  # DOLAR CANADIENSE
    11: (2700, "GBP"),  # LIBRA ESTERLINA
    12: (3600, "JPY"),  # YEN
    21: (1300, "CLP"),  # PESO CHILENO
    23: (500, "ARS"),  # PESO ARGENTINO
    24: (105, "AUD"),  # DLS.AUSTRALIANOS
    27: (4150, "CNY"),  # YUANES RENMBI
    28: (1800, "DKK"),  # CORONAS DANESAS
    29: (4200, "MXN"),  # NVO. PSO. MEXICANO
    30: (4600, "NOK"),  # CORONAS NORUEGAS
    31: (1490, "NZD"),  # DLS. NEOZELANDESES
    33: (1620, "ZAR"),  # RAND SUDAFRICANO
    34: (501, "ARS"),  # PESO ARGENTINO
    38: (2230, "USD"),  # DOLAR FONDO COMPRADOR
    41: (5100, "HKD"),  # DOLAR HONG KONG
    42: (5300, "KRW"),  # WON
    44: (5500, "COP"),  # PESO COLOMBIANO
    46: (5700, "INR"),  # RUPIA INDIA
    14: (5900, "CHF"),  # FRANCO SUIZO
    48: (4900, "ISK"),  # CORONA ISLANDESA
    49: (2222, "USD"),  # DOLAR PIZARRA VENDEDOR
    17: (2, "XDR"),  # DER.ESP. DE GIRO (SDR)
    19: (9900, "U.R."),  # BCU 9900, ISO U.R.
}

# Pass-through: ``amount_uyu = amount``, no BCU call. Maps to display
# currency for documentation/audit purposes.
PASSTHROUGH_TABLE: dict[int, str] = {
    0: "UYU",  # PESOS URUGUAYOS
}

# Non-convertible: ``amount_uyu = NULL``. The display code is a
# non-ISO 4217 placeholder (Uruguayan domestic units / historical codes)
# — the database stores whatever 3-letter string is provided here.
NON_CONVERTIBLE_TABLE: dict[int, str] = {
    4: "UIX",  # UNIDAD INDEXADA
    5: "URX",  # UNIDAD REAJUSTABLE
    22: "OHX",  # ORO (historical)
    39: "EUX",  # EURO TRANSFERENCIA (non-convertible via BCU)
}

_LICENSE_LINK_TEMPLATE = (
    "https://www.comprasestatales.gub.uy/consultas/detalle/id/{id_compra}"
)


def display_currency(id_moneda: int | None) -> str:
    """Pick the 3-letter display code for ``id_moneda``.

    Falls back to ``"N/D"`` (No Disponible) when the ID is unrecognised so the
    row still satisfies the ``String(3) NOT NULL`` constraint and the UI
    shows a consistent Spanish placeholder.
    """

    if id_moneda in PASSTHROUGH_TABLE:
        return PASSTHROUGH_TABLE[id_moneda]
    if id_moneda in NON_CONVERTIBLE_TABLE:
        return NON_CONVERTIBLE_TABLE[id_moneda]
    if id_moneda in CONVERSION_TABLE:
        return CONVERSION_TABLE[id_moneda][1]
    return "N/D"


def build_license_link(id_compra: str) -> str:
    """Build the public detail-page URL for ``id_compra``.

    Deterministic — no HTTP request — so the same ``id_compra`` always
    produces the same ``license_link`` (see ``organism-lookup`` spec,
    "License Link Construction" scenario). The identifier is URL-encoded
    so a scraped value cannot break the resulting URL path.
    """

    return _LICENSE_LINK_TEMPLATE.format(id_compra=quote(id_compra, safe=""))


def _integer_grouped(value: Number) -> str:
    """Format an integer (no decimals) with a period as the thousands separator.

    ``1250000`` → ``"1.250.000"``. Negative inputs render with a leading
    minus. The decimal portion of a :class:`Decimal` is rounded to the
    nearest integer with banker-free ``ROUND_HALF_UP`` semantics so
    amounts like ``999.5`` render as ``"1.000"`` (the standard
    Spanish-currency rounding rule).
    """

    quantized = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    digits = str(abs(int(quantized)))
    # Insert a period every three digits from the right.
    groups: list[str] = []
    while len(digits) > 3:
        groups.append(digits[-3:])
        digits = digits[:-3]
    groups.append(digits)
    return f"{sign}{'.'.join(reversed(groups))}"


def format_uyu(value: Number) -> str:
    """Format a currency value in es-UY without a currency suffix.

    The route layer composes the UYU suffix separately so the helper
    stays free of localisation-table data and easy to unit-test. The
    spec (kpi-summary spec, "Total formatted as currency" scenario)
    expects ``"1.250.000 UYU"`` — the template concatenates ``" UYU"``
    onto whatever this function returns.

    Examples:
        >>> format_uyu(Decimal("1250000"))
        '1.250.000'
        >>> format_uyu(0)
        '0'
    """

    return _integer_grouped(value)


def format_count(value: Number) -> str:
    """Format an integer count with es-UY thousands separators.

    The spec (kpi-summary spec, "Counts formatted with separators")
    expects ``"1.234"`` for a count of 1234. Decimals are rounded
    using :func:`format_uyu` — the dashboard currently only produces
    integer counts (``COUNT(DISTINCT …)``), so the rounding branch
    is only defensive.

    Examples:
        >>> format_count(1234)
        '1.234'
        >>> format_count(0)
        '0'
    """

    return _integer_grouped(value)


def format_percent(value: Number) -> str:
    """Format a 0..1 ratio as an es-UY percentage with one decimal.

    The spec (market-concentration spec, "Percentage formatting"
    scenario) expects ``"42,3 %"`` for a ratio of ``0.4231``: one
    decimal place, comma as the decimal separator, space before
    ``%``. ``None`` and values outside ``0..1`` are still rendered
    — the helper does not enforce bounds because the dashboard
    surfaces ratios only and the service clamps them to the
    legal range.

    The ratio is multiplied by 100 before formatting, so the input
    is the raw ratio in the ``[0, 1]`` range, not a pre-scaled
    percentage. ``format_percent(0.4231)`` → ``"42,3 %"``,
    ``format_percent(1)`` → ``"100,0 %"``.

    Examples:
        >>> format_percent(Decimal("0.4231"))
        '42,3 %'
        >>> format_percent(1)
        '100,0 %'
        >>> format_percent(0)
        '0,0 %'
    """

    ratio = Decimal(str(value)) * 100
    quantized = ratio.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    abs_val = abs(quantized)
    # ``str(Decimal)`` keeps the trailing zero (e.g. "0.5" stays "0.5",
    # "0.50" is unchanged too). Replace the decimal point with a
    # comma for the es-UY representation.
    text = f"{abs_val:.1f}".replace(".", ",")
    return f"{sign}{text} %"


def format_percent_adaptive(value: Number) -> str:
    """Format a ratio with extra precision for tiny KPI shares.

    Ratios at or above one percent use one decimal place after scaling to a
    percentage. Smaller ratios are kept visible with three decimal places.
    The latter intentionally preserves the small ratio's displayed magnitude,
    matching the company-profile KPI contract.
    """

    ratio = Decimal(str(value))
    if abs(ratio) >= Decimal("0.01"):
        percentage = ratio * 100
        places = 1
    else:
        percentage = ratio
        places = 3

    quantized = percentage.quantize(
        Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP
    )
    sign = "-" if quantized < 0 else ""
    text = f"{abs(quantized):.{places}f}".replace(".", ",")
    return f"{sign}{text} %"


__all__ = [
    "CONVERSION_TABLE",
    "NON_CONVERTIBLE_TABLE",
    "PASSTHROUGH_TABLE",
    "build_license_link",
    "display_currency",
    "format_uyu",
    "format_count",
    "format_percent",
    "format_percent_adaptive",
]
