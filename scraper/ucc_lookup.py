"""Static ``id_ucc`` → organism name lookup.

Parallel to :mod:`scraper.organism_lookup`, but for the alternative
identifier some purchases carry instead of the usual
``(id_inciso, id_ue)`` pair. The table is a pure Python dictionary
sourced from the government's UCC codiguera. Updating the table is the
only maintenance the module needs — no I/O, no parsing, no rate
limiting.

The :func:`resolve_ucc_organism` function never raises on a missing
key — it logs a warning and returns ``"Desconocido"`` so a partial
mapping does not block ingestion (see ``ucc-organism-lookup`` spec,
"Unknown id_ucc returns Desconocido" scenario).

This module is the fallback step of a two-tier organism resolution
chain in :func:`scraper.main.enrich_xml_compra`. The first tier is
:func:`scraper.organism_lookup.resolve_organism`; when that returns a
``"Desconocido"`` placeholder, :func:`resolve_ucc_organism` is
consulted. The two ID systems are independent — separate modules make
the fallback chain explicit at import time and keep each file focused
on a single static mapping.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static mapping — official government UCC codiguera
# ---------------------------------------------------------------------------
# Key: ``id_ucc`` (int).
# Value: full organism name as displayed in the UI.
#
# Source: <unidades-centralizadas-de-contratacion> codiguera from the
# procurement system. New entries appear whenever the government
# registers a new UCC; the operator must update this dict when that
# happens. No schema migration, no settings, no I/O.
# ---------------------------------------------------------------------------
UCC_MAP: dict[int, str] = {
    1: "UCAMAE",
    2: "UCAA (Alimentos)",
    21: "UCC MTOP",
    43: "UCC MSP",
    44: "MSP 35/43/47/53",
    45: "Etchepare y S.C.Rossi",
    48: "UCA",
    49: "UCC MGAP",
    52: "UCC Armada",
    53: "UCC MI",
    54: "UCC Presidencia",
    55: "UCC MTSS",
    56: "UCC Turismo y Deporte",
    57: "UCC MIDES",
    58: "UCC MEC",
    59: "Unidad Centralizada San José",
    60: "UACM - AGESIC",
    61: "UCC MVOTMA",
    62: "UACM - MI",
    63: "UACM - Presidencia",
    64: "UACM - MEF",
    65: "UACM - ARCE",
    66: "UCC MIEM",
    67: "UACM - Ejército",
    69: "UCC - ASSE",
    70: "PE 60/021",
    71: "UCA CM",
    72: "UCC MA",
    73: "UACC - ARCE",
    74: "UCM - ASSE",
    75: "UACM - MTOP",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_ucc_organism(id_ucc: int | None) -> str:
    """Return the organism name for an ``id_ucc`` value.

    Parameters
    ----------
    id_ucc:
        The procurement-system UCC identifier extracted from the XML
        ``<compra>`` attribute. ``None`` is treated like any other
        unmapped key — the function returns ``"Desconocido"`` and does
        NOT log a warning (a ``None`` value is the documented "no UCC"
        signal; a missing integer key is the actual anomaly).

    Returns
    -------
    str
        The organism name when the key exists in :data:`UCC_MAP`, or
        ``"Desconocido"`` otherwise.

    Notes
    -----
    The function is a pure read — no I/O, no cache invalidation, no
    side effects beyond logging. The contract is intentionally
    forgiving: an unmapped id is a *log event* plus a placeholder
    string, never an exception. The pipeline must always produce a
    record to insert; a missing organism name would otherwise violate
    the ``organism`` ``NOT NULL`` constraint downstream.
    """

    if id_ucc is None:
        return "Desconocido"

    name = UCC_MAP.get(id_ucc)
    if name is not None:
        return name

    logger.warning(
        "Unmapped id_ucc=%r; using fallback 'Desconocido'",
        id_ucc,
    )
    return "Desconocido"


__all__ = ["UCC_MAP", "resolve_ucc_organism"]
