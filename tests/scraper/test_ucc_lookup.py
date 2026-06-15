"""Unit tests for :mod:`scraper.ucc_lookup`.

The lookup is a pure function over a static dictionary, so the suite
focuses on three contracts:

* **Mapped id_ucc** — known ``id_ucc`` returns the organism name.
* **Unknown id_ucc** — unmapped id returns ``"Desconocido"`` and
  emits a WARNING log with the id visible (per ``ucc-organism-lookup``
  spec, "Unknown id_ucc returns Desconocido" scenario).
* **None input** — ``None`` is treated like an unmapped key without
  raising and without a warning (it's the documented "no UCC" signal).

The static :data:`scraper.ucc_lookup.UCC_MAP` is also asserted to be
non-empty so a future cleanup pass can't accidentally delete every
row.
"""

from __future__ import annotations

import logging

from scraper.ucc_lookup import UCC_MAP, resolve_ucc_organism

# ---------------------------------------------------------------------------
# Mapped ids
# ---------------------------------------------------------------------------


def test_resolve_ucc_organism_returns_name_for_known_id() -> None:
    """A canonical id_ucc must hit the table."""

    # 1 → "UCAMAE" — also referenced in the ``ucc-organism-lookup`` spec
    # "Sample entry round-trips" scenario.
    assert resolve_ucc_organism(1) == "UCAMAE"


def test_resolve_ucc_organism_resolves_multiple_known_ids() -> None:
    """A sample of documented entries must round-trip."""

    samples = [
        (2, "UCAA (Alimentos)"),
        (43, "UCC MSP"),
        (54, "UCC Presidencia"),
        (69, "UCC - ASSE"),
        (75, "UACM - MTOP"),
    ]
    for ucc_id, expected in samples:
        assert resolve_ucc_organism(ucc_id) == expected


def test_ucc_map_is_non_empty() -> None:
    """Defensive: a fully-empty table would silently regress the pipeline."""

    assert len(UCC_MAP) > 0


# ---------------------------------------------------------------------------
# Unmapped ids — fallback + WARNING log
# ---------------------------------------------------------------------------


def test_resolve_ucc_organism_returns_desconocido_for_unmapped_id(
    caplog,
) -> None:
    """An unknown id_ucc MUST return ``"Desconocido"`` and log a WARNING."""

    with caplog.at_level(logging.WARNING, logger="scraper.ucc_lookup"):
        result = resolve_ucc_organism(99999)

    assert result == "Desconocido"
    # WARNING logged with the id visible.
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "expected a WARNING log for an unmapped id_ucc"
    assert any("99999" in r.getMessage() for r in warning_records)


def test_resolve_ucc_organism_logs_warning_for_every_unmapped_call(
    caplog,
) -> None:
    """Each unmapped call MUST emit a warning — the spec is explicit about this."""

    with caplog.at_level(logging.WARNING, logger="scraper.ucc_lookup"):
        resolve_ucc_organism(12345)
        resolve_ucc_organism(12345)
        resolve_ucc_organism(12345)

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 3


def test_resolve_ucc_organism_unmapped_call_returns_same_string_each_time() -> None:
    """The fallback string MUST be stable — it ends up in the database."""

    a = resolve_ucc_organism(54321)
    b = resolve_ucc_organism(54321)
    assert a == b == "Desconocido"


# ---------------------------------------------------------------------------
# None inputs — graceful, never raise
# ---------------------------------------------------------------------------


def test_resolve_ucc_organism_handles_none() -> None:
    """``None`` is the documented "no UCC" signal — return ``Desconocido``."""

    assert resolve_ucc_organism(None) == "Desconocido"


def test_resolve_ucc_organism_does_not_log_warning_for_none(caplog) -> None:
    """``None`` is the explicit "no UCC" case — no warning should fire.

    A missing integer id is the anomaly; ``None`` is the expected
    absence. Differentiating them keeps the warning log actionable.
    """

    with caplog.at_level(logging.WARNING, logger="scraper.ucc_lookup"):
        resolve_ucc_organism(None)

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records == []


# ---------------------------------------------------------------------------
# Table integrity
# ---------------------------------------------------------------------------


def test_ucc_map_meets_minimum_size() -> None:
    """The spec requires the table to carry at least 30 entries."""

    assert len(UCC_MAP) >= 30


def test_ucc_map_values_are_non_empty_strings() -> None:
    """Every value in the table MUST be a non-empty string."""

    for ucc_id, name in UCC_MAP.items():
        assert isinstance(name, str), f"id_ucc={ucc_id!r} has non-str value {name!r}"
        assert name, f"id_ucc={ucc_id!r} maps to an empty string"


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------


def test_resolve_ucc_organism_always_returns_str() -> None:
    """The ``organism`` DB column is ``String(255) NOT NULL`` — no exceptions."""

    samples: list[int | None] = [
        1,
        99999,
        None,
        0,
    ]
    for ucc_id in samples:
        result = resolve_ucc_organism(ucc_id)
        assert isinstance(result, str)
        assert result  # never empty — the fallback always populates
