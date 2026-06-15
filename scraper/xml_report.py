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
        <oferentes>
          <oferente nombre_comercial="Otra Empresa"
                    nro_doc_prov="210000000099"
                    tipo_doc_prov="RUT"
                    cant_ofertada="10"
                    precio_unit_ofertado="110.00"
                    id_moneda="20" />
        </oferentes>
      </compra>
    </reporte>

A single ``<compra>`` produces exactly one :class:`XmlCompra`, carrying
every compra-level attribute plus lists of nested
:class:`XmlAdjudicacion` (one per ``<adjudicacion>``) and
:class:`XmlOferente` (one per ``<oferente>``). Malformed purchase or
adjudication blocks are skipped and logged so a partial failure never
aborts a run. Unknown attributes on any element log a WARNING but do
not skip the record — that way the parser stays forward-compatible
with XML changes (data-ingestion spec, "Parser Logs Unknown
Attributes" requirement).
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

# ---------------------------------------------------------------------------
# Compra-level attribute set (one column per known XML attribute). The
# parser maps any attr in this set to a typed field on XmlCompra.
# Anything not in the set is logged as a WARNING and ignored — the
# schema does not need to be DDL-changed every time the upstream
# payload grows a new attribute.
# ---------------------------------------------------------------------------
_COMPRA_KNOWN_ATTRS: frozenset[str] = frozenset(
    {
        "id_compra",
        "objeto",
        "monto_adj",
        "id_moneda_monto_adj",
        "fecha_pub_adj",
        "num_compra",
        "anio_compra",
        "id_tipocompra",
        "subtipo_compra",
        "id_inciso",
        "id_ue",
        "id_ucc",
    }
)

_ADJUDICACION_KNOWN_ATTRS: frozenset[str] = frozenset(
    {
        "nombre_comercial",
        "nro_doc_prov",
        "tipo_doc_prov",
        "cant_adj",
        "precio_unit",
        "precio_tot_imp",
        "desc_articulo",
        "id_moneda",
        "id_articulo",
    }
)

_OFERENTE_KNOWN_ATTRS: frozenset[str] = frozenset(
    {
        "nombre_comercial",
        "nro_doc_prov",
        "tipo_doc_prov",
        "cant_ofertada",
        "precio_unit_ofertado",
        "id_moneda",
        "variacion",
        "alternativas",
    }
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class XmlAdjudicacion:
    """One ``<adjudicacion>`` record extracted from the XML report.

    Carries the adjudicated line item's identifying and pricing
    attributes. ``id_compra`` echoes the parent compra's natural key
    for in-memory correlation; the database linkage is enforced via
    the foreign key in :class:`app.models.adjudicacion.Adjudicacion`.
    """

    id_compra: str
    nombre_comercial: str
    nro_doc_prov: str | None
    tipo_doc_prov: str | None
    cant_adj: Decimal | None
    precio_unit: Decimal | None
    precio_tot_imp: Decimal
    desc_articulo: str
    id_moneda: int
    id_articulo: str | None


@dataclass(frozen=True)
class XmlOferente:
    """One ``<oferente>`` record extracted from the XML report.

    Carries the bidder's identifying and pricing attributes.
    ``id_compra`` echoes the parent compra's natural key for
    in-memory correlation; the database linkage is enforced via the
    foreign key in :class:`app.models.oferente.Oferente`.
    """

    id_compra: str
    nombre_comercial: str | None
    nro_doc_prov: str | None
    tipo_doc_prov: str | None
    cant_ofertada: Decimal | None
    precio_unit_ofertado: Decimal | None
    id_moneda: int | None
    variacion: str | None
    alternativas: str | None


@dataclass(frozen=True)
class XmlCompra:
    """One ``<compra>`` block extracted from the XML report.

    Carries the compra-level identifying and pricing attributes plus
    the lists of nested :class:`XmlAdjudicacion` and
    :class:`XmlOferente` records. A compra with no ``<adjudicaciones>``
    children yields an empty ``adjudicaciones`` list (the parser still
    returns the compra — the caller decides what to do with it). A
    compra with an empty ``<oferentes/>`` element similarly yields an
    empty ``oferentes`` list.
    """

    id_compra: str
    fecha_pub_adj: date
    id_tipocompra: str
    id_moneda_monto_adj: int
    objeto: str | None
    monto_adj: Decimal | None
    num_compra: str | None
    anio_compra: str | None
    subtipo_compra: str | None
    id_inciso: int | None
    id_ue: int | None
    id_ucc: int | None
    adjudicaciones: list[XmlAdjudicacion]
    oferentes: list[XmlOferente]


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

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
) -> bytes:
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
    bytes
        The raw XML body as bytes. The caller is responsible for
        parsing. Returning bytes (not ``str``) preserves the original
        encoding declared in the XML declaration — lxml reads the
        ``encoding`` attribute from the ``<?xml ...?>`` prologue and
        decodes accordingly. Decoding via ``response.text`` would
        introduce a double-encoding when the upstream uses ISO-8859-1
        (the government endpoint's default).

    Raises
    ------
    httpx.HTTPError
        Propagated from ``httpx`` on transport or HTTP-status failures.
    """

    if client is None:
        with httpx.Client(timeout=timeout, headers=_HEADERS) as owned:
            response = owned.get(url)
            response.raise_for_status()
            return response.content

    response = client.get(url, headers=_HEADERS)
    response.raise_for_status()
    return response.content


# ---------------------------------------------------------------------------
# Type coercion helpers
# ---------------------------------------------------------------------------


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


def _log_unknown_attrs(
    element: etree._Element, known: frozenset[str]
) -> None:
    """Log at DEBUG level each attribute on ``element`` outside ``known``.

    The schema is forward-compatible: when the upstream XML adds a
    new attribute, the parser logs it so the team can decide whether
    to add a column. The record itself is still produced — the
    unknown attribute is silently dropped (data-ingestion spec,
    "Parser Logs Unknown Attributes" requirement).
    """

    tag = element.tag.rsplit("}", 1)[-1]
    for attr_name in element.attrib:
        if attr_name not in known:
            logger.debug(
                "Unknown attribute on <%s>: attr=%s value=%r",
                tag,
                attr_name,
                element.attrib[attr_name],
            )


# ---------------------------------------------------------------------------
# Per-element extraction
# ---------------------------------------------------------------------------


def _extract_compra(compra: etree._Element) -> XmlCompra | None:
    """Extract one :class:`XmlCompra` from a ``<compra>`` element.

    Returns ``None`` when the element lacks the minimum identifying
    attributes (``id_compra`` and a parseable ``fecha_pub_adj``).
    Malformed child elements (``<adjudicacion>`` or ``<oferente>``)
    are skipped with a warning — the parent compra is still
    returned.
    """

    _log_unknown_attrs(compra, _COMPRA_KNOWN_ATTRS)

    id_compra = _attr(compra, "id_compra")
    if not id_compra:
        logger.warning("Skipping <compra> without id_compra")
        return None

    fecha_raw = _attr(compra, "fecha_pub_adj")
    parsed_date = _parse_date(fecha_raw)
    if parsed_date is None:
        logger.warning(
            "Skipping <compra id_compra=%s> with invalid fecha_pub_adj", id_compra
        )
        return None

    id_moneda_monto_adj_raw = _attr(compra, "id_moneda_monto_adj")
    id_moneda_monto_adj = _parse_int(id_moneda_monto_adj_raw)
    if id_moneda_monto_adj is None:
        logger.warning(
            "Skipping <compra id_compra=%s> with invalid id_moneda_monto_adj",
            id_compra,
        )
        return None

    id_tipocompra = _attr(compra, "id_tipocompra") or ""

    adjudicaciones: list[XmlAdjudicacion] = []
    oferentes: list[XmlOferente] = []

    # Children may be inside wrapper elements (``<adjudicaciones>`` /
    # ``<oferentes>``) or directly under ``<compra>`` (defensive — the
    # upstream XSD uses the wrappers, but we do not crash on either
    # shape). Match by local tag name.
    for child in compra.iter():
        local = child.tag.rsplit("}", 1)[-1]
        if local == "adjudicacion" and child is not compra:
            record = _extract_adjudicacion(id_compra, child)
            if record is not None:
                adjudicaciones.append(record)
        elif local == "oferente" and child is not compra:
            record = _extract_oferente(id_compra, child)
            if record is not None:
                oferentes.append(record)

    return XmlCompra(
        id_compra=id_compra,
        fecha_pub_adj=parsed_date,
        id_tipocompra=id_tipocompra,
        id_moneda_monto_adj=id_moneda_monto_adj,
        objeto=_attr(compra, "objeto"),
        monto_adj=_parse_decimal(_attr(compra, "monto_adj")),
        num_compra=_attr(compra, "num_compra"),
        anio_compra=_attr(compra, "anio_compra"),
        subtipo_compra=_attr(compra, "subtipo_compra"),
        id_inciso=_parse_int(_attr(compra, "id_inciso")),
        id_ue=_parse_int(_attr(compra, "id_ue")),
        id_ucc=_parse_int(_attr(compra, "id_ucc")),
        adjudicaciones=adjudicaciones,
        oferentes=oferentes,
    )


def _extract_adjudicacion(
    id_compra: str, adj_el: etree._Element
) -> XmlAdjudicacion | None:
    """Extract one :class:`XmlAdjudicacion` from a ``<adjudicacion>`` child.

    Returns ``None`` when the element lacks the minimum required
    fields (``nombre_comercial``, ``desc_articulo``, ``precio_tot_imp``,
    ``id_moneda``). Each missing field logs a warning with the
    parent id_compra so the team can investigate upstream.
    """

    _log_unknown_attrs(adj_el, _ADJUDICACION_KNOWN_ATTRS)

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

    return XmlAdjudicacion(
        id_compra=id_compra,
        nombre_comercial=nombre_comercial,
        nro_doc_prov=_attr(adj_el, "nro_doc_prov"),
        tipo_doc_prov=_attr(adj_el, "tipo_doc_prov"),
        cant_adj=_parse_decimal(_attr(adj_el, "cant_adj")),
        precio_unit=_parse_decimal(_attr(adj_el, "precio_unit")),
        precio_tot_imp=precio_tot_imp,
        desc_articulo=desc_articulo,
        id_moneda=id_moneda,
        id_articulo=_attr(adj_el, "id_articulo"),
    )


def _extract_oferente(
    id_compra: str, of_el: etree._Element
) -> XmlOferente | None:
    """Extract one :class:`XmlOferente` from a ``<oferente>`` child.

    Oferente rows are intentionally permissive: an empty element
    yields a row with every field ``None`` (the schema's nullable
    columns tolerate this). We only skip when ``_extract_oferente``
    itself cannot produce a coherent record (none of the current
    extraction paths fail outright, but the ``None``-tolerant
    contract is documented here for future schema tightening).
    """

    _log_unknown_attrs(of_el, _OFERENTE_KNOWN_ATTRS)

    return XmlOferente(
        id_compra=id_compra,
        nombre_comercial=_attr(of_el, "nombre_comercial"),
        nro_doc_prov=_attr(of_el, "nro_doc_prov"),
        tipo_doc_prov=_attr(of_el, "tipo_doc_prov"),
        cant_ofertada=_parse_decimal(_attr(of_el, "cant_ofertada")),
        precio_unit_ofertado=_parse_decimal(_attr(of_el, "precio_unit_ofertado")),
        id_moneda=_parse_int(_attr(of_el, "id_moneda")),
        variacion=_attr(of_el, "variacion"),
        alternativas=_attr(of_el, "alternativas"),
    )


# ---------------------------------------------------------------------------
# Public parser entry point
# ---------------------------------------------------------------------------


def parse_xml_report(xml_text: str | bytes) -> Iterator[XmlCompra]:
    """Yield an :class:`XmlCompra` for every well-formed top-level compra.

    Parameters
    ----------
    xml_text:
        The raw XML payload returned by :func:`fetch_xml_report`.
        Accepts both ``bytes`` (preferred — preserves the upstream
        encoding declaration) and ``str`` (for test fixtures and
        in-memory payloads). When a ``str`` with an XML encoding
        declaration is passed, it is re-encoded to UTF-8 bytes so
        lxml can honour the declared encoding.

    Yields
    ------
    XmlCompra
        One per well-formed ``<compra>``. Each compra carries its
        own list of nested ``<adjudicacion>`` and ``<oferente>``
        children. Malformed blocks are skipped with a warning; the
        parent compra is still yielded so partial recovery is
        possible.
    """

    # lxml rejects ``str`` input when the XML declaration contains an
    # ``encoding`` attribute.  Always pass bytes so lxml can honour the
    # declared encoding (ISO-8859-1 from the government endpoint,
    # UTF-8 in test fixtures, etc.).
    if isinstance(xml_text, str):
        raw = xml_text.encode("utf-8")
    else:
        raw = xml_text

    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError as exc:
        logger.error("XML payload is malformed: %s", exc)
        return

    # Use a forgiving xpath: namespaces are unlikely in the report (top-level
    # elements are unprefixed), but tolerating them is cheap.
    for compra in root.iter():
        if not compra.tag.endswith("compra"):
            continue
        record = _extract_compra(compra)
        if record is not None:
            yield record


__all__ = [
    "XmlAdjudicacion",
    "XmlCompra",
    "XmlOferente",
    "fetch_xml_report",
    "parse_xml_report",
]
