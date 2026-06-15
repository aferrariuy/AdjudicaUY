"""Convert non-UYU adjudication amounts to UYU using BCU exchange rates.

The normalization step takes a :class:`JoinedRecord` (an already-enriched
adjudication, defined here in the normalizer module — see
``Decision: JoinedRecord disposition`` in the design) and produces a
:class:`NormalizedRecord` where ``currency`` is a 3-letter display code
and ``amount_uyu`` is the equivalent value in Uruguayan pesos, or
``NULL`` when conversion was impossible.

The pipeline is:

1. Look up the procurement ``id_moneda`` in :data:`CONVERSION_TABLE`. If
   the code is ``0`` (Pesos Uruguayos), ``amount_uyu`` is set to
   ``amount`` and no BCU call is made.
2. If the code is a known non-convertible currency (UI, UR, OHR, …),
   ``amount_uyu`` is ``NULL`` and no BCU call is made.
3. If the code maps to a BCU currency, the TCC rate is fetched for the
   adjudication date with a 7-day lookback fallback. ``amount_uyu`` is
   ``amount * TCC`` rounded to two decimal places.
4. If the code is unmapped, the normalizer queries the BCU ``monedas``
   endpoint as a best-effort sanity check. If the procurement ID happens
   to coincide with a valid BCU code, the conversion proceeds; otherwise
   ``amount_uyu`` is ``NULL`` and a warning is logged.
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

logger = logging.getLogger(__name__)

# A scale of 2 fits the ``Numeric(12, 2)`` column on the Adjudication model.
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
    41: (5100, "HKD"),  # DOLAR HONG KONG
    42: (5300, "KRW"),  # WON
    44: (5500, "COP"),  # PESO COLOMBIANO
    46: (5700, "INR"),  # RUPIA INDIA
    48: (4900, "ISK"),  # CORONA ISLANDESA
    17: (2, "XDR"),  # DER.ESP. DE GIRO (SDR)
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


@dataclass(frozen=True)
class JoinedRecord:
    """A fully-enriched adjudication, ready for normalization and insertion.

    ``source_url`` is the URL the XML report was fetched from — the same for
    every record in a run. It is the first column of the unique constraint
    defined on the ``adjudications`` table, so re-scraping the same source
    is naturally idempotent.
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
    organism: str
    license_link: str
    source_url: str
    id_articulo: str | None
    num_compra: str | None
    anio_compra: str | None
    id_inciso: int | None
    id_ue: int | None


@dataclass(frozen=True)
class NormalizedRecord:
    """A joined record enriched with ``currency`` and ``amount_uyu``.

    Has exactly the fields needed to populate an :class:`Adjudication` row
    — no more, no less — so the orchestrator can map them one-to-one.
    """

    # Provenance / identification
    id_compra: str
    source_url: str
    license_link: str
    date: object  # ``datetime.date`` — written as object to avoid an import dance
    organism: str
    id_inciso: int | None
    id_ue: int | None

    # Financials
    amount: Decimal
    currency: str
    amount_uyu: Decimal | None

    # Company
    winning_company: str
    company_document: str | None
    company_document_type: str | None

    # License / item
    license_type: str
    article: str
    article_quantity: Decimal | None
    article_id: str | None


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


def normalize_record(
    record: JoinedRecord,
    bcu_client: BcuClient,
    *,
    max_lookback_days: int = 7,
) -> NormalizedRecord:
    """Convert ``record`` into a :class:`NormalizedRecord` ready for insertion.

    The BCU client is used as a context manager internally to keep its
    lifecycle predictable when this function is called in a tight loop —
    the caller may also pass a long-lived client, which the function will
    not close.
    """

    id_moneda = record.id_moneda
    mode = _resolve_mode(id_moneda)

    currency = _display_currency(id_moneda)
    amount = record.precio_tot_imp

    if mode is ConversionMode.PASSTHROUGH:
        amount_uyu: Decimal | None = _quantize_uyu(amount)
    elif mode is ConversionMode.NULL:
        amount_uyu = None
    else:  # CONVERT
        bcu_code, _ = CONVERSION_TABLE[id_moneda]
        rate = bcu_client.get_tcc(
            bcu_code, record.fecha_pub_adj, max_lookback_days=max_lookback_days
        )
        amount_uyu = None if rate is None else _quantize_uyu(amount * rate)

    # If we couldn't resolve ``id_moneda`` at all, try a BCU monedas lookup
    # as a last resort. This branch only runs for the small set of codes
    # that are not in the static tables.
    if (
        id_moneda not in PASSTHROUGH_TABLE
        and id_moneda not in NON_CONVERTIBLE_TABLE
        and id_moneda not in CONVERSION_TABLE
    ):
        resolved = _try_resolve_unknown(id_moneda, bcu_client)
        if resolved is not None:
            bcu_code, iso = resolved
            currency = iso
            rate = bcu_client.get_tcc(
                bcu_code, record.fecha_pub_adj, max_lookback_days=max_lookback_days
            )
            amount_uyu = None if rate is None else _quantize_uyu(amount * rate)
        else:
            logger.warning(
                "Unknown id_moneda=%s for id_compra=%s; setting amount_uyu=NULL",
                id_moneda,
                record.id_compra,
            )
            amount_uyu = None
            currency = "UNK"

    return NormalizedRecord(
        id_compra=record.id_compra,
        source_url=record.source_url,
        license_link=record.license_link,
        date=record.fecha_pub_adj,
        organism=record.organism,
        id_inciso=record.id_inciso,
        id_ue=record.id_ue,
        amount=_quantize_uyu(amount),
        currency=currency,
        amount_uyu=amount_uyu,
        winning_company=record.nombre_comercial,
        company_document=record.nro_doc_prov,
        company_document_type=record.tipo_doc_prov,
        license_type=record.id_tipocompra,
        article=record.desc_articulo,
        article_quantity=(
            _quantize_uyu(record.cant_adj) if record.cant_adj is not None else None
        ),
        article_id=record.id_articulo,
    )


__all__ = [
    "CONVERSION_TABLE",
    "ConversionMode",
    "JoinedRecord",
    "NON_CONVERTIBLE_TABLE",
    "NormalizedRecord",
    "PASSTHROUGH_TABLE",
    "normalize_record",
]
