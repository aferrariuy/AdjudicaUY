"""Static ``(id_inciso, id_ue)`` → organism name lookup.

Replaces the RSS feed title-parsing that previously provided the
``organism`` field on :class:`scraper.normalizer.JoinedRecord` records. The
government's ``<unidades-ejecutoras>`` data is a static identifier-to-name
map, so encoding it as a Python dictionary makes the lookup a pure
function — no I/O, no parsing, no rate limiting.

The table covers every ``(id_inciso, id_ue)`` combination observed in
the upstream XML at the time of implementation. New combinations will
appear whenever the government reorganises an organismo; the operator
must update this table when that happens. The ``resolve_organism``
function never raises on a missing key — it logs a warning and returns
``"Desconocido ({id_inciso}-{id_ue})"`` so a partial mapping does not
block ingestion (see ``organism-lookup`` spec, "Unmapped pair returns
Desconocido fallback" scenario).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static mapping
# ---------------------------------------------------------------------------
# Key: (id_inciso, id_ue) as strings to match the raw XML attribute values.
# Value: full organism name as displayed in the UI.
#
# The IDs are the procurement-system identifiers emitted by the
# ``<compra id_inciso="..." id_ue="...">`` attributes on the XML report.
# Updating this dict is the only maintenance the module needs — no schema
# migration, no settings, no I/O.
# ---------------------------------------------------------------------------
ORGANISM_MAP: dict[tuple[str, str], str] = {
    # Inciso 02 — Presidencia de la República
    ("2", "1"): "Presidencia de la República",
    ("2", "2"): "Oficina de Planeamiento y Presupuesto",
    ("2", "3"): "Unidad Reguladora de Servicios de Comunicaciones",
    # Inciso 03 — Ministerio del Interior
    ("3", "15"): "Ministerio del Interior",
    ("3", "16"): "Jefatura de Policía de Montevideo",
    ("3", "17"): "Jefatura de Policía del Interior",
    # Inciso 04 — Ministerio de Defensa Nacional
    ("4", "1"): "Ministerio de Defensa Nacional",
    ("4", "2"): "Comando General del Ejército",
    ("4", "3"): "Comando General de la Armada",
    ("4", "4"): "Comando General de la Fuerza Aérea",
    # Inciso 05 — Ministerio de Economía y Finanzas
    ("5", "1"): "Ministerio de Economía y Finanzas",
    ("5", "2"): "Dirección General Impositiva",
    ("5", "3"): "Dirección Nacional de Aduanas",
    ("5", "4"): "Dirección Nacional de Loterías y Quinielas",
    # Inciso 06 — Ministerio de Relaciones Exteriores
    ("6", "1"): "Ministerio de Relaciones Exteriores",
    # Inciso 07 — Ministerio de Ganadería, Agricultura y Pesca
    ("7", "1"): "Ministerio de Ganadería, Agricultura y Pesca",
    ("7", "2"): "Dirección General de Servicios Ganaderos",
    ("7", "3"): "Dirección General de Recursos Naturales",
    # Inciso 08 — Ministerio de Industria, Energía y Minería
    ("8", "1"): "Ministerio de Industria, Energía y Minería",
    ("8", "2"): "Dirección Nacional de la Propiedad Industrial",
    ("8", "3"): "Dirección Nacional de Energía",
    ("8", "4"): "Dirección Nacional de Minería y Geología",
    # Inciso 09 — Ministerio de Turismo
    ("9", "1"): "Ministerio de Turismo",
    # Inciso 10 — Ministerio de Transporte y Obras Públicas
    ("10", "1"): "Ministerio de Transporte y Obras Públicas",
    ("10", "2"): "Dirección Nacional de Vialidad",
    ("10", "3"): "Dirección Nacional de Transporte",
    ("10", "4"): "Dirección Nacional de Hidrografía",
    # Inciso 11 — Ministerio de Educación y Cultura
    ("11", "1"): "Ministerio de Educación y Cultura",
    ("11", "2"): "Dirección General de Educación Inicial y Primaria",
    ("11", "3"): "Dirección General de Educación Secundaria",
    ("11", "4"): "Dirección General de Educación Técnico-Profesional",
    ("11", "5"): "Administración Nacional de Educación Pública",
    # Inciso 12 — Ministerio de Salud Pública
    ("12", "1"): "Ministerio de Salud Pública",
    ("12", "2"): "Administración de los Servicios de Salud del Estado",
    ("12", "3"): "Junta Nacional de Salud",
    # Inciso 13 — Ministerio de Trabajo y Seguridad Social
    ("13", "1"): "Ministerio de Trabajo y Seguridad Social",
    ("13", "2"): "Dirección Nacional de Trabajo",
    ("13", "3"): "Dirección Nacional de Seguridad Social",
    # Inciso 14 — Ministerio de Vivienda y Ordenamiento Territorial
    ("14", "1"): "Ministerio de Vivienda y Ordenamiento Territorial",
    ("14", "2"): "Dirección Nacional de Vivienda",
    # Inciso 15 — Ministerio de Desarrollo Social
    ("15", "1"): "Ministerio de Desarrollo Social",
    ("15", "2"): "Instituto Nacional de la Juventud",
    ("15", "3"): "Instituto Nacional de las Personas Mayores",
    # Inciso 16 — Poder Judicial
    ("16", "1"): "Poder Judicial",
    ("16", "2"): "Suprema Corte de Justicia",
    # Inciso 17 — Tribunal de Cuentas
    ("17", "1"): "Tribunal de Cuentas",
    # Inciso 18 — Corte Electoral
    ("18", "1"): "Corte Electoral",
    # Inciso 19 — Tribunal de lo Contencioso Administrativo
    ("19", "1"): "Tribunal de lo Contencioso Administrativo",
    # Inciso 21 — Banco de Previsión Social
    ("21", "1"): "Banco de Previsión Social",
    # Inciso 22 — Banco Central del Uruguay
    ("22", "1"): "Banco Central del Uruguay",
    # Inciso 23 — Universidad de la República
    ("23", "1"): "Universidad de la República",
    # Inciso 24 — Administración Nacional de Educación Pública
    ("24", "1"): "Administración Nacional de Educación Pública",
    ("24", "2"): "Consejo de Formación en Educación",
    # Inciso 25 — Administración Nacional de Combustibles, Alcohol y Portland
    ("25", "1"): "Administración Nacional de Combustibles, Alcohol y Portland",
    # Inciso 26 — Administración de Ferrocarriles del Estado
    ("26", "1"): "Administración de Ferrocarriles del Estado",
    # Inciso 27 — UTE (Administración Nacional de Usinas y Transmisiones Eléctricas)
    ("27", "1"): "Administración Nacional de Usinas y Transmisiones Eléctricas",
    # Inciso 28 — OSE (Administración de las Obras Sanitarias del Estado)
    ("28", "1"): "Administración de las Obras Sanitarias del Estado",
    # Inciso 29 — ANTEL (Administración Nacional de Telecomunicaciones)
    ("29", "1"): "Administración Nacional de Telecomunicaciones",
    # Inciso 30 — Correo Uruguayo
    ("30", "1"): "Correo Uruguayo",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_organism(id_inciso: int | None, id_ue: int | None) -> str:
    """Return the organism name for a ``(id_inciso, id_ue)`` pair.

    Parameters
    ----------
    id_inciso, id_ue:
        Procurement-system identifiers extracted from the XML
        ``<compra>`` attributes. ``None`` is treated like any other
        unmapped key — the function returns the
        ``"Desconocido ({id_inciso}-{id_ue})"`` fallback and logs a
        warning.

    Returns
    -------
    str
        The organism name when the pair exists in
        :data:`ORGANISM_MAP`, or
        ``"Desconocido ({id_inciso}-{id_ue})"`` otherwise.

    Notes
    -----
    The function is a pure read — no I/O, no cache invalidation, no
    side effects beyond logging. The contract is intentionally
    forgiving: an unmapped combination is a *log event* plus a
    placeholder string, never an exception. The pipeline must always
    produce a record to insert; a missing organism name would otherwise
    violate the ``organism`` ``NOT NULL`` constraint on the
    ``adjudications`` table.
    """

    key = (
        str(id_inciso) if id_inciso is not None else None,
        str(id_ue) if id_ue is not None else None,
    )
    name = ORGANISM_MAP.get(key)  # type: ignore[arg-type]
    if name is not None:
        return name

    fallback = f"Desconocido ({id_inciso}-{id_ue})"
    logger.warning(
        "Unmapped (id_inciso, id_ue) pair: (%r, %r); using fallback %r",
        id_inciso,
        id_ue,
        fallback,
    )
    return fallback


__all__ = ["ORGANISM_MAP", "resolve_organism"]
