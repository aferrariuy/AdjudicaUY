"""Unit tests for :mod:`scraper.organism_lookup`.

The lookup is a pure function over a static dictionary, so the suite
focuses on three contracts:

* **Mapped pair** — known ``(id_inciso, id_ue)`` returns the organism
  name.
* **Unmapped pair** — unknown pair returns the
  ``"Desconocido ({id_inciso}-{id_ue})"`` fallback and emits a WARNING
  log (per ``organism-lookup`` spec, "Unmapped pair returns Desconocido
  fallback" scenario).
* **None inputs** — ``None`` is treated like any other unmapped key
  (never raises; the call still produces a usable string).

The static :data:`scraper.organism_lookup.ORGANISM_MAP` is also asserted
to be non-empty so a future cleanup pass can't accidentally delete
every row.
"""

from __future__ import annotations

import logging

from scraper.organism_lookup import ORGANISM_MAP, resolve_organism

# ---------------------------------------------------------------------------
# Mapped pairs
# ---------------------------------------------------------------------------


def test_resolve_organism_returns_name_for_known_pair() -> None:
    """The canonical (id_inciso, id_ue) → name lookup must hit the table."""

    # (4, 1) → "Secretaría del Ministerio del Interior" — also referenced
    # in the ``organism-lookup`` spec "Both attributes present" scenario.
    assert resolve_organism(4, 1) == "Secretaría del Ministerio del Interior"


def test_resolve_organism_resolves_multiple_known_pairs() -> None:
    """Every documented entry in the table must round-trip."""

    samples = [
        ((2, 1), "Presidencia de la República y Unidades Dependientes"),
        ((11, 2), "Dirección de Educación"),
        ((53, 1), "Banco de Seguros del Estado"),
        ((65, 1), "Administración Nacional de Telecomunicaciones"),
    ]
    for (inciso, ue), expected in samples:
        assert resolve_organism(inciso, ue) == expected


def test_organism_map_is_non_empty() -> None:
    """Defensive: a fully-empty table would silently regress the pipeline."""

    assert len(ORGANISM_MAP) > 0


# ---------------------------------------------------------------------------
# Unmapped pairs — fallback + WARNING log
# ---------------------------------------------------------------------------


def test_resolve_organism_returns_desconocido_fallback_for_unmapped_pair(
    caplog,
) -> None:
    with caplog.at_level(logging.WARNING, logger="scraper.organism_lookup"):
        result = resolve_organism(99, 99)

    assert result == "Desconocido (99-99)"
    # WARNING logged with both id_inciso and id_ue visible.
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "expected a WARNING log for an unmapped pair"
    assert any("99" in r.getMessage() for r in warning_records)


def test_resolve_organism_logs_warning_for_every_unmapped_call(
    caplog,
) -> None:
    """Each unmapped call MUST emit a warning — the spec is explicit about this."""

    with caplog.at_level(logging.WARNING, logger="scraper.organism_lookup"):
        resolve_organism(77, 88)
        resolve_organism(77, 88)
        resolve_organism(77, 88)

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 3


def test_resolve_organism_unmapped_call_returns_same_string_each_time() -> None:
    """The fallback string MUST be stable — it ends up in the database."""

    a = resolve_organism(123, 456)
    b = resolve_organism(123, 456)
    assert a == b == "Desconocido (123-456)"


# ---------------------------------------------------------------------------
# None inputs — graceful, never raise
# ---------------------------------------------------------------------------


def test_resolve_organism_handles_none_for_id_inciso() -> None:
    assert resolve_organism(None, 15) == "Desconocido (None-15)"


def test_resolve_organism_handles_none_for_id_ue() -> None:
    assert resolve_organism(3, None) == "Desconocido (3-None)"


def test_resolve_organism_handles_both_none() -> None:
    assert resolve_organism(None, None) == "Desconocido (None-None)"


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------


def test_resolve_organism_always_returns_str() -> None:
    """The ``organism`` DB column is ``String(255) NOT NULL`` — no exceptions."""

    samples: list[tuple[int | None, int | None]] = [
        (4, 1),
        (99, 99),
        (None, None),
        (0, 0),
    ]
    for inciso, ue in samples:
        result = resolve_organism(inciso, ue)
        assert isinstance(result, str)
        assert result  # never empty — the fallback always populates
