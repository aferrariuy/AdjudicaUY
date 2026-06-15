"""Fetch and parse the XML adjudication report (Source A).

The upstream endpoint returns a ``<reporte>`` document whose structure is::

    <reporte>
      <reporte_cabezal>...</reporte_cabezal>
      <compra id_compra="1319278"
              objeto="Adquisición de ..."
              monto_adj="1234.56"
              id_moneda_monto_adj="20"
              fecha_pub_adj="2024-01-15"
              num_compra="86825"
              anio_compra="2024"
              id_tipocompra="CD"
              subtipo_compra="">
        <adjudicaciones>
          <adjudicacion nombre_comercial="Empresa SA"
                        nro_doc_prov="210000000018"
                        tipo_doc_prov="RUT"
                        cant_adj="10"
                        precio_unit="100.00"
                        precio_tot_imp="1234.56"
                        desc_articulo="Laptop"
                        id_moneda="20" />
        </adjudicaciones>
        <oferentes>...</oferentes>
      </compra>
    </reporte>

A single ``<compra>`` may produce several ``XmlAdjudication`` records — one per
nested ``<adjudicacion>``. Malformed purchase or adjudication blocks are
skipped and logged so a partial failure never aborts a run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx
from lxml import etree

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# Date formats we expect to see in ``fecha_pub_adj``. The first match wins.
_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d-%m-%Y",
)


@dataclass(frozen=True)
class XmlAdjudication:
    """One adjudication record extracted from the XML report.

    The record is a *partial* view — it intentionally lacks the organism name
    and the public detail URL, which the pipeline enriches from
    :mod:`scraper.organism_lookup` and from the deterministic
    ``/detalle/id/{id_compra}`` URL template respectively.
    """

    id_compra: str
    fecha_pub_adj: date
    id_tipocompra: str
    id_moneda_monto_adj: int
    nombre_comercial: str
    nro_doc_prov: str | None
    tipo_doc_prov: str | None
    cant_adj: Decimal | None
    precio_tot_imp: Decimal
    desc_articulo: str
    id_moneda: int
    id_articulo: str | None
    num_compra: str | None
    anio_compra: str | None
    id_inciso: int | None
    id_ue: int | None


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml, text/xml, */*",
}


def fetch_xml_report(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> str:
    """Fetch the raw XML payload from ``url``.

    Parameters
    ----------
    url:
        Fully-qualified URL of the report endpoint.
    client:
        Optional ``httpx.Client`` to use (mainly for tests). When ``None``, a
        short-lived client is created for the call.
    timeout:
        Per-request timeout, in seconds.

    Returns
    -------
    str
        The XML body. The caller is responsible for parsing.

    Raises
    ------
    httpx.HTTPError
        Propagated from ``httpx`` on transport or HTTP-status failures.
    """

    if client is None:
        with httpx.Client(timeout=timeout, headers=_HEADERS) as owned:
            response = owned.get(url)
            response.raise_for_status()
            return response.text

    response = client.get(url, headers=_HEADERS)
    response.raise_for_status()
    return response.text


def _parse_date(value: str | None) -> date | None:
    """Best-effort parse of a date string in any of the supported formats."""

    if not value:
        return None
    candidate = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    logger.warning(
        "Could not parse fecha_pub_adj=%r; expected one of %s", candidate, _DATE_FORMATS
    )
    return None


def _parse_decimal(value: str | None) -> Decimal | None:
    """Parse a string into a ``Decimal``; return ``None`` on empty/invalid."""

    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return Decimal(candidate)
    except (InvalidOperation, ValueError):
        logger.warning("Could not parse numeric value %r as Decimal", candidate)
        return None


def _parse_int(value: str | None) -> int | None:
    """Parse a string into an ``int``; return ``None`` on empty/invalid."""

    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return int(candidate)
    except (ValueError, TypeError):
        logger.warning("Could not parse integer value %r", candidate)
        return None


def _attr(element: etree._Element, name: str) -> str | None:
    """Return ``element.attrib[name]`` or ``None`` when missing/empty."""

    raw = element.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _normalize_compra(
    compra: etree._Element,
) -> tuple[str, date, str, int, str | None, str | None, int | None, int | None] | None:
    """Extract the ``<compra>``-level fields shared by every adjudication.

    Returns a tuple of ``(id_compra, fecha_pub_adj, id_tipocompra,
    id_moneda_monto_adj, num_compra, anio_compra, id_inciso, id_ue)``.
    ``id_inciso`` and ``id_ue`` are the procurement-system identifiers
    used to look up the organism name in
    :data:`scraper.organism_lookup.ORGANISM_MAP`; both are ``None`` when
    the upstream attributes are absent (see ``organism-lookup`` spec,
    "Missing attributes" scenario).
    """

    id_compra = _attr(compra, "id_compra")
    fecha_raw = _attr(compra, "fecha_pub_adj")
    id_tipocompra = _attr(compra, "id_tipocompra") or ""
    id_moneda_monto_adj_raw = _attr(compra, "id_moneda_monto_adj")

    if not id_compra:
        logger.warning("Skipping <compra> without id_compra")
        return None

    parsed_date = _parse_date(fecha_raw)
    if parsed_date is None:
        logger.warning(
            "Skipping <compra id_compra=%s> with invalid fecha_pub_adj", id_compra
        )
        return None

    id_moneda_monto_adj = _parse_int(id_moneda_monto_adj_raw)
    if id_moneda_monto_adj is None:
        logger.warning(
            "Skipping <compra id_compra=%s> with invalid id_moneda_monto_adj", id_compra
        )
        return None

    num_compra = _attr(compra, "num_compra")
    anio_compra = _attr(compra, "anio_compra")
    id_inciso = _parse_int(_attr(compra, "id_inciso"))
    id_ue = _parse_int(_attr(compra, "id_ue"))

    return (
        id_compra,
        parsed_date,
        id_tipocompra,
        id_moneda_monto_adj,
        num_compra,
        anio_compra,
        id_inciso,
        id_ue,
    )


def _normalize_adjudicacion(
    parent: tuple[str, date, str, int, str | None, str | None, int | None, int | None],
    adj_el: etree._Element,
) -> XmlAdjudication | None:
    """Extract one ``<adjudicacion>`` record, scoped under its parent ``<compra>``."""

    (
        id_compra,
        parsed_date,
        id_tipocompra,
        id_moneda_monto_adj,
        num_compra,
        anio_compra,
        id_inciso,
        id_ue,
    ) = parent

    nombre_comercial = _attr(adj_el, "nombre_comercial")
    desc_articulo = _attr(adj_el, "desc_articulo")
    precio_raw = _attr(adj_el, "precio_tot_imp")
    id_moneda_raw = _attr(adj_el, "id_moneda")

    if not nombre_comercial or not desc_articulo:
        logger.warning(
            "Skipping <adjudicacion> under id_compra=%s: "
            "missing nombre_comercial/desc_articulo",
            id_compra,
        )
        return None

    precio_tot_imp = _parse_decimal(precio_raw)
    if precio_tot_imp is None:
        logger.warning(
            "Skipping <adjudicacion> under id_compra=%s: invalid precio_tot_imp=%r",
            id_compra,
            precio_raw,
        )
        return None

    id_moneda = _parse_int(id_moneda_raw)
    if id_moneda is None:
        logger.warning(
            "Skipping <adjudicacion> under id_compra=%s: invalid id_moneda=%r",
            id_compra,
            id_moneda_raw,
        )
        return None

    return XmlAdjudication(
        id_compra=id_compra,
        fecha_pub_adj=parsed_date,
        id_tipocompra=id_tipocompra,
        id_moneda_monto_adj=id_moneda_monto_adj,
        nombre_comercial=nombre_comercial,
        nro_doc_prov=_attr(adj_el, "nro_doc_prov"),
        tipo_doc_prov=_attr(adj_el, "tipo_doc_prov"),
        cant_adj=_parse_decimal(_attr(adj_el, "cant_adj")),
        precio_tot_imp=precio_tot_imp,
        desc_articulo=desc_articulo,
        id_moneda=id_moneda,
        id_articulo=_attr(adj_el, "id_articulo"),
        num_compra=num_compra,
        anio_compra=anio_compra,
        id_inciso=id_inciso,
        id_ue=id_ue,
    )


def parse_xml_report(xml_text: str) -> Iterator[XmlAdjudication]:
    """Yield an :class:`XmlAdjudication` for every well-formed nested record.

    Parameters
    ----------
    xml_text:
        The raw XML payload returned by :func:`fetch_xml_report`.

    Yields
    ------
    XmlAdjudication
        One per well-formed ``<adjudicacion>`` under a well-formed
        ``<compra>``. Malformed blocks are skipped with a warning.
    """

    try:
        root = etree.fromstring(
            xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text
        )
    except etree.XMLSyntaxError as exc:
        logger.error("XML payload is malformed: %s", exc)
        return

    # Use a forgiving xpath: namespaces are unlikely in the report (top-level
    # elements are unprefixed), but tolerating them is cheap.
    for compra in root.iter():
        if not compra.tag.endswith("compra"):
            continue
        parent = _normalize_compra(compra)
        if parent is None:
            continue

        for adj_el in compra.iter():
            if not adj_el.tag.endswith("adjudicacion"):
                continue
            record = _normalize_adjudicacion(parent, adj_el)
            if record is not None:
                yield record


__all__ = ["XmlAdjudication", "fetch_xml_report", "parse_xml_report"]
