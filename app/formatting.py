"""Locale-aware number formatting for es-UY.

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
  — the values come from :class:`app.services.adjudication_service.KpiSummary`
  as raw :class:`decimal.Decimal` / :class:`int` and we apply the
  locale formatting before reaching the template.
* The concentration partial renders the percentage beneath the
  donut, with one decimal place.

Negative numbers are formatted with a leading minus (the dashboard
never emits a negative sum today, but the helper tolerates them
for forward-compatibility). Decimals are rounded at the formatter
level — the values in the dataclasses are already exact (the
service casts to ``Decimal``), so rounding is a no-op for the
current use cases.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

Number = Decimal | int | float


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


__all__ = ["format_uyu", "format_count", "format_percent"]
