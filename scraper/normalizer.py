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
   otherwise the line is skipped with a warning and the parent is
   retained.

Adjudication lines are isolated from each other: an invalid line
(negative or non-finite ``precio_tot_imp``, negative ``id_moneda``, an
unresolved currency, or a line-specific BCU/conversion failure) is
skipped with a warning while the parent compra and its valid siblings
are retained. A constructible compra may therefore have zero surviving
adjudications and is still returned as a valid parent-only row;
``monto_adj`` is always persisted verbatim from the XML parent and is
never recomputed from surviving children.

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

from app.formatting import (
    CONVERSION_TABLE,
    NON_CONVERTIBLE_TABLE,
    PASSTHROUGH_TABLE,
    display_currency,
)
from scraper.bcu_client import BcuError

if TYPE_CHECKING:
    from datetime import date

    from scraper.bcu_client import BcuClient
    from scraper.xml_report import XmlAdjudicacion, XmlCompra

logger = logging.getLogger(__name__)

# A scale of 2 fits the ``Numeric(14, 2)`` columns on the new models.
_UYU_SCALE = Decimal("0.01")


class NormalizationError(Exception):
    """Base class for normalization failures."""


class CurrencyNotResolvedError(NormalizationError):
    """Raised when a procurement currency ID cannot be mapped or resolved."""


class MalformedCompraError(NormalizationError):
    """Raised when a compra fails parent-level validation or construction.

    Line-scoped adjudication failures are skipped and warned instead of
    raising; this error is reserved for failures that prevent building
    the parent :class:`CompraRow` itself.
    """


class ConversionMode(enum.Enum):
    """How a procurement currency ID is handled by the normalizer."""

    PASSTHROUGH = "passthrough"  # UYU — amount_uyu = amount
    CONVERT = "convert"  # Mapped currency — amount_uyu = amount * TCC
    NULL = "null"  # Non-convertible — amount_uyu = NULL


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
    one CompraRow. ``adjudicaciones`` may be empty: a constructible
    compra whose lines were all skipped is still a valid parent-only
    row.
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


def _quantize_uyu(value: Decimal) -> Decimal:
    """Round ``value`` to two decimal places using banker's-safe rounding."""

    return value.quantize(_UYU_SCALE, rounding=ROUND_HALF_UP)


def _try_resolve_unknown(
    id_moneda: int, bcu_client: BcuClient
) -> tuple[int, str] | None:
    """Best-effort lookup of an unmapped ``id_moneda`` against the BCU catalogue.

    The procurement and BCU ID spaces are independent, so this only
    succeeds when the unknown procurement ID *coincidentally* equals a
    BCU currency code. The catalogue is cached by the shared
    :class:`BcuClient` for the lifetime of the client instance, so
    repeated lookups across a scrape reuse the same parsed catalogue
    instead of fetching it per line.
    """

    monedas = bcu_client.list_monedas()

    for entry in monedas:
        if entry.codigo == id_moneda:
            iso = entry.codigo_iso or "N/D"
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
    currency = display_currency(id_moneda)

    if mode is ConversionMode.PASSTHROUGH:
        return currency, _quantize_uyu(amount)
    if mode is ConversionMode.NULL:
        return currency, None

    bcu_code, _ = CONVERSION_TABLE[id_moneda]
    rate = bcu_client.get_tcc(bcu_code, on_date, max_lookback_days=max_lookback_days)
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
    :class:`AdjudicacionRow`, not the parent. Invalid adjudication
    lines (negative/non-finite ``precio_tot_imp``, negative
    ``id_moneda``, unresolved currency, or a line-specific
    BCU/conversion failure) are skipped and warned at line scope;
    they never discard the parent compra or its valid siblings. A
    constructible compra may therefore have zero surviving
    adjudications and is still returned as a valid parent-only row.
    Unmapped ``id_moneda`` codes fall back to the BCU ``monedas``
    endpoint as a last resort (the same path the legacy normalizer
    took).
    """

    adjudicaciones: list[AdjudicacionRow] = []

    def _skip_line(reason: str, adj: XmlAdjudicacion) -> None:
        """Emit the line-scoped skip warning with the full line identity."""
        logger.warning(
            "Skipping adjudication: id_compra=%s reason=%s "
            "nombre_comercial=%r desc_articulo=%r id_moneda=%s "
            "id_articulo=%r precio_tot_imp=%s",
            compra.id_compra,
            reason,
            adj.nombre_comercial,
            adj.desc_articulo,
            adj.id_moneda,
            adj.id_articulo,
            adj.precio_tot_imp,
        )

    for adj in compra.adjudicaciones:
        try:
            # Data-quality gate: non-finite/negative amounts and
            # negative currency IDs are line-scoped skips — they are
            # not valid procurement data, but they must not reject
            # the whole compra.
            if not adj.precio_tot_imp.is_finite() or adj.precio_tot_imp < 0:
                _skip_line("invalid precio_tot_imp", adj)
                continue
            if adj.id_moneda < 0:
                _skip_line("negative id_moneda", adj)
                continue

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
                        None
                        if rate is None
                        else _quantize_uyu(adj.precio_tot_imp * rate)
                    )
                else:
                    raise CurrencyNotResolvedError(
                        f"Could not resolve id_moneda={id_moneda} "
                        f"for id_compra={compra.id_compra}"
                    )
            else:
                currency, amount_uyu = _convert_amount(
                    id_moneda,
                    adj.precio_tot_imp,
                    compra.fecha_pub_adj,
                    bcu_client,
                    max_lookback_days=max_lookback_days,
                )

            # Append only after every line step succeeded.
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
        except CurrencyNotResolvedError:
            _skip_line("currency not resolved", adj)
        except (BcuError, NormalizationError) as exc:
            _skip_line(f"BCU/conversion failure: {exc}", adj)

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
    "CurrencyNotResolvedError",
    "MalformedCompraError",
    "NON_CONVERTIBLE_TABLE",
    "NormalizationError",
    "OferenteRow",
    "PASSTHROUGH_TABLE",
    "AdjudicacionRow",
    "normalize_compra",
]
