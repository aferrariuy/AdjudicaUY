"""add unique constraints to adjudicacion and oferente

Revision ID: 1573c3ee8233
Revises: 007_add_id_ucc
Create Date: 2026-06-19 03:51:05.777039+00:00

Adds the natural-key unique constraints that back the
``ON CONFLICT DO NOTHING`` clause in :func:`scraper.persistence._bulk_insert`:

* ``adjudicacion`` — one awarded line item per (compra, company, article).
  The triple is the natural key in the upstream XML report; a second
  scrape of the same data must NOT add a second row.
* ``oferente`` — one bidder entry per (compra, company).
  The pair is the natural key; a second scrape of the same data must
  NOT add a second row.

The migration is safe to apply against existing rows: both constraints
use the natural key, and re-running the scraper is already idempotent
at the parent (``compra.id_compra``) level. Any duplicate child rows
that were inserted by older runs of the scraper will block constraint
creation — the operator must clean them up before applying the upgrade
(see data-ingestion spec, "Child dedup" scenario).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "1573c3ee8233"
down_revision: str | Sequence[str] | None = "007_add_id_ucc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_adjudicacion_line_item",
        "adjudicacion",
        ["compra_id", "nombre_comercial", "desc_articulo"],
    )
    op.create_unique_constraint(
        "uq_oferente_bidder",
        "oferente",
        ["compra_id", "nombre_comercial"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_oferente_bidder", "oferente", type_="unique")
    op.drop_constraint("uq_adjudicacion_line_item", "adjudicacion", type_="unique")
