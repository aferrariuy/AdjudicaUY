"""Convert non-UYU adjudication amounts to UYU using BCU exchange rates.

The normalization step takes an :class:`XmlCompra` (already-parsed
XML, defined in :mod:`scraper.xml_report`) plus a per-compra organism
enrichment and produces one :class:`CompraRow` carrying the compra
metadata plus a list of :class:`AdjudicacionRow` carrying each
adjudicated line item with its per-row ``amount_uyu``.

The pipeline is, per ``<adjudicacion>`` child:

1. Look up the procurement ``id_moneda`` in :data:`CONVERSION_TABLE`. If
   the code is ``0`` (Pesos Uruguayos), ``amount_uyu`` is set to
   ``precio_tot_imp`` and no BCU call is made.
2. If the code is a known non-convertible currency (UI, UR, OHR, …),
   ``amount_uyu`` is ``NULL`` and no BCU call is made.
3. If the code maps to a BCU currency, the TCC rate is fetched for
   the adjudication date with a 7-day lookback fallback. ``amount_uyu``
   is ``precio_tot_imp * TCC`` rounded to two decimal places.
4. If the code is unmapped, the normalizer queries the BCU ``monedas``
   endpoint as a best-effort sanity check. If the procurement ID
   happens to coincide with a valid BCU code, the conversion proceeds;
   otherwise ``amount_uyu`` is ``NULL`` and a warning is logged.

The BCU rate is resolved per adjudicated line item, using
``adjudicacion.id_moneda`` (NOT the compra-level
``id_moneda_monto_adj``) — the spec changed the conversion path to
be per-line-item (data-ingestion spec, "BCU Exchange Rate Fetching"
requirement).
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

    from scraper.bcu_client import BcuClient
    from scraper.xml_report import XmlCompra

logger = logging.getLogger(__name__)

# A scale of 2 fits the ``Numeric(14, 2)`` columns on the new models.
_UYU_SCALE = Decimal("0.01")


class ConversionMode(enum.Enum):
    """How a procurement currency ID is handled by the normalizer."""

    PASSTHROUGH = "passthrough"  # UYU — amount_uyu = amount
    CONVERT = "convert"  # Mapped currency — amount_uyu = amount * TCC
    NULL = "null"  # Non-convertible — amount_uyu = NULL


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


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompraEnrichment:
    """Per-compra enrichment shared by every child adjudication.

    Built once per :class:`XmlCompra` by the orchestrator: the
    organism is resolved via the static ``(id_inciso, id_ue)``
    lookup, the license link is built deterministically from
    ``id_compra``, and ``source_url`` is the per-day URL the XML
    was fetched from. Keeping these as a separate dataclass means
    :class:`CompraRow` / :class:`AdjudicacionRow` are pure
    one-to-one projections of the parser's data — no extra
    cross-cutting fields to track.
    """

    organism: str
    license_link: str
    source_url: str


@dataclass(frozen=True)
class AdjudicacionRow:
    """One adjudicated line item, ready for persistence.

    Carries every column the ``adjudicacion`` table needs plus the
    BCU-normalized ``amount_uyu`` and the display-currency code.
    The display code lives here (not in the model) because the
    conversion happens at ingest time and the table does not store
    it — the web layer's :class:`AdjudicationRow` re-derives it from
    the :class:`Compra` row's source data when needed.
    """

    # Provenance
    id_compra: str
    nombre_comercial: str
    nro_doc_prov: str | None
    tipo_doc_prov: str | None

    # Pricing
    cant_adj: Decimal | None
    precio_unit: Decimal | None
    precio_tot_imp: Decimal
    id_moneda: int
    currency: str
    amount_uyu: Decimal | None

    # Article
    desc_articulo: str
    id_articulo: str | None


@dataclass(frozen=True)
class OferenteRow:
    """One bidder record, ready for persistence.

    Bidders carry no normalized-currency field — the upstream
    ``id_moneda`` is stored as-is on the row. The currency code is
    not derived here because the web app does not aggregate
    oferentes by amount.
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
class CompraRow:
    """One compra + its adjudicated children, ready for persistence.

    The persistence layer maps :class:`CompraRow` to the ``compra``
    table and each :class:`AdjudicacionRow` /
    :class:`OferenteRow` to its child table. The shape is
    flat-on-the-parent / nested-on-the-children — the database is
    normalized, but the orchestrator's view stays one XmlCompra =
    one CompraRow.
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
    organismo: str
    license_link: str
    source_url: str
    adjudicaciones: list[AdjudicacionRow]
    oferentes: list[OferenteRow]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_mode(id_moneda: int) -> ConversionMode:
    """Classify ``id_moneda`` into one of the three conversion modes."""

    if id_moneda in PASSTHROUGH_TABLE:
        return ConversionMode.PASSTHROUGH
    if id_moneda in NON_CONVERTIBLE_TABLE:
        return ConversionMode.NULL
    if id_moneda in CONVERSION_TABLE:
        return ConversionMode.CONVERT
    return ConversionMode.NULL  # default for unknown IDs


def _display_currency(id_moneda: int) -> str:
    """Pick the 3-letter display code for ``id_moneda``.

    Falls back to a generic placeholder when the ID is unrecognised so the
    row still satisfies the ``String(3) NOT NULL`` constraint.
    """

    if id_moneda in PASSTHROUGH_TABLE:
        return PASSTHROUGH_TABLE[id_moneda]
    if id_moneda in NON_CONVERTIBLE_TABLE:
        return NON_CONVERTIBLE_TABLE[id_moneda]
    if id_moneda in CONVERSION_TABLE:
        return CONVERSION_TABLE[id_moneda][1]
    return "UNK"


def _quantize_uyu(value: Decimal) -> Decimal:
    """Round ``value`` to two decimal places using banker's-safe rounding."""

    return value.quantize(_UYU_SCALE, rounding=ROUND_HALF_UP)


def _try_resolve_unknown(
    id_moneda: int, bcu_client: BcuClient
) -> tuple[int, str] | None:
    """Best-effort lookup of an unmapped ``id_moneda`` against the BCU catalogue.

    The procurement and BCU ID spaces are independent, so this only
    succeeds when the unknown procurement ID *coincidentally* equals a
    BCU currency code. The endpoint call is intentionally not cached —
    the catalogue is small and rarely changes.
    """

    try:
        monedas = bcu_client.list_monedas()
    except Exception as exc:  # BcuError, HTTPError, etc.
        logger.warning("BCU monedas lookup failed for id_moneda=%s: %s", id_moneda, exc)
        return None

    for entry in monedas:
        if entry.codigo == id_moneda:
            iso = entry.codigo_iso or "UNK"
            logger.info(
                "Resolved unknown id_moneda=%s via BCU monedas -> bcu_code=%s ISO=%s",
                id_moneda,
                entry.codigo,
                iso,
            )
            return entry.codigo, iso[:3]  # ISO column is 3 chars
    return None


def _convert_amount(
    id_moneda: int,
    amount: Decimal,
    on_date: date,
    bcu_client: BcuClient,
    *,
    max_lookback_days: int = 7,
) -> tuple[str, Decimal | None]:
    """Return ``(currency_display_code, amount_uyu_or_None)`` for one line.

    Centralizes the BCU lookup + currency classification so the
    :func:`normalize_compra` driver does not have to repeat the
    dispatch table per adjudicated row.
    """

    mode = _resolve_mode(id_moneda)
    currency = _display_currency(id_moneda)

    if mode is ConversionMode.PASSTHROUGH:
        return currency, _quantize_uyu(amount)
    if mode is ConversionMode.NULL:
        return currency, None

    bcu_code, _ = CONVERSION_TABLE[id_moneda]
    rate = bcu_client.get_tcc(
        bcu_code, on_date, max_lookback_days=max_lookback_days
    )
    return currency, None if rate is None else _quantize_uyu(amount * rate)


# ---------------------------------------------------------------------------
# Public normalizer
# ---------------------------------------------------------------------------


def normalize_compra(
    compra: XmlCompra,
    enrichment: CompraEnrichment,
    bcu_client: BcuClient,
    *,
    max_lookback_days: int = 7,
) -> CompraRow:
    """Convert ``compra`` into a :class:`CompraRow` ready for insertion.

    Each nested :class:`XmlAdjudicacion` is BCU-normalized
    independently — the per-row ``amount_uyu`` lives on the child
    :class:`AdjudicacionRow`, not the parent. Unmapped
    ``id_moneda`` codes fall back to the BCU ``monedas`` endpoint
    as a last resort (the same path the legacy normalizer took).
    """

    adjudicaciones: list[AdjudicacionRow] = []
    for adj in compra.adjudicaciones:
        # Last-resort: if id_moneda is not in any of the static
        # tables, try the BCU monedas catalogue. This branch only
        # runs for the small set of unmapped codes; the rest take
        # the fast path in ``_convert_amount``.
        id_moneda = adj.id_moneda
        if (
            id_moneda not in PASSTHROUGH_TABLE
            and id_moneda not in NON_CONVERTIBLE_TABLE
            and id_moneda not in CONVERSION_TABLE
        ):
            resolved = _try_resolve_unknown(id_moneda, bcu_client)
            if resolved is not None:
                # Inject the resolved code into the static table so
                # the rest of the run can use the fast path. Use a
                # per-call local copy to keep the module-level
                # tables pristine (and the dispatch predictable).
                local_conversion = {**CONVERSION_TABLE, id_moneda: resolved}
                bcu_code, iso = local_conversion[id_moneda]
                rate = bcu_client.get_tcc(
                    bcu_code,
                    compra.fecha_pub_adj,
                    max_lookback_days=max_lookback_days,
                )
                currency = iso[:3]
                amount_uyu = (
                    None if rate is None else _quantize_uyu(adj.precio_tot_imp * rate)
                )
            else:
                logger.warning(
                    "Unknown id_moneda=%s for id_compra=%s; setting amount_uyu=NULL",
                    id_moneda,
                    compra.id_compra,
                )
                currency = "UNK"
                amount_uyu = None
        else:
            currency, amount_uyu = _convert_amount(
                id_moneda,
                adj.precio_tot_imp,
                compra.fecha_pub_adj,
                bcu_client,
                max_lookback_days=max_lookback_days,
            )

        adjudicaciones.append(
            AdjudicacionRow(
                id_compra=compra.id_compra,
                nombre_comercial=adj.nombre_comercial,
                nro_doc_prov=adj.nro_doc_prov,
                tipo_doc_prov=adj.tipo_doc_prov,
                cant_adj=adj.cant_adj,
                precio_unit=adj.precio_unit,
                precio_tot_imp=adj.precio_tot_imp,
                id_moneda=id_moneda,
                currency=currency,
                amount_uyu=amount_uyu,
                desc_articulo=adj.desc_articulo,
                id_articulo=adj.id_articulo,
            )
        )

    oferentes: list[OferenteRow] = [
        OferenteRow(
            id_compra=compra.id_compra,
            nombre_comercial=of.nombre_comercial,
            nro_doc_prov=of.nro_doc_prov,
            tipo_doc_prov=of.tipo_doc_prov,
            cant_ofertada=of.cant_ofertada,
            precio_unit_ofertado=of.precio_unit_ofertado,
            id_moneda=of.id_moneda,
            variacion=of.variacion,
            alternativas=of.alternativas,
        )
        for of in compra.oferentes
    ]

    return CompraRow(
        id_compra=compra.id_compra,
        fecha_pub_adj=compra.fecha_pub_adj,
        id_tipocompra=compra.id_tipocompra,
        id_moneda_monto_adj=compra.id_moneda_monto_adj,
        objeto=compra.objeto,
        monto_adj=compra.monto_adj,
        num_compra=compra.num_compra,
        anio_compra=compra.anio_compra,
        subtipo_compra=compra.subtipo_compra,
        id_inciso=compra.id_inciso,
        id_ue=compra.id_ue,
        id_ucc=compra.id_ucc,
        organismo=enrichment.organism,
        license_link=enrichment.license_link,
        source_url=enrichment.source_url,
        adjudicaciones=adjudicaciones,
        oferentes=oferentes,
    )


__all__ = [
    "CONVERSION_TABLE",
    "CompraEnrichment",
    "CompraRow",
    "ConversionMode",
    "NON_CONVERTIBLE_TABLE",
    "OferenteRow",
    "PASSTHROUGH_TABLE",
    "AdjudicacionRow",
    "normalize_compra",
]
