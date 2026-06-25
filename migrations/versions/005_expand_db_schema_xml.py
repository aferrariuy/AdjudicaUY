"""add compra / adjudicacion / oferente tables for full XML hierarchy

Revision ID: 005_expand_db_schema_xml
Revises: 004_add_inciso_ue
Create Date: 2026-06-15 06:00:00

Adds three new tables that mirror the upstream XML report's hierarchy
alongside the existing ``adjudications`` table. The new tables are
additive — they do not replace ``adjudications`` yet. Subsequent
migrations will switch the scraper and web layer to read/write them
and drop the old table.

* ``compra`` — one row per ``<compra>`` element. Natural key
  ``id_compra`` is unique; every other XML attribute is a nullable
  column. Indexes on ``fecha_pub_adj``, ``id_inciso``, ``id_ue``,
  and ``id_tipocompra`` cover the filter and ordering queries the
  web app issues (data-storage spec, "Indexes for filter columns"
  scenario).
* ``adjudicacion`` — one row per ``<adjudicacion>`` child, linked to
  its parent ``compra`` via ``compra_id`` with ``ON DELETE CASCADE``.
  Indexes on ``compra_id`` and ``id_articulo`` (data-storage spec,
  "Adjudicacion Table" requirement).
* ``oferente`` — one row per ``<oferente>`` child, linked to its
  parent ``compra`` via ``compra_id`` with ``ON DELETE CASCADE``.
  Index on ``compra_id`` (data-storage spec, "Oferente Table"
  requirement).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "005_expand_db_schema_xml"
down_revision: str | Sequence[str] | None = "004_add_inciso_ue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Create ``compra`` — the new top-level table.
    # ------------------------------------------------------------------
    op.create_table(
        "compra",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("id_compra", sa.String(length=50), nullable=False),
        sa.Column("objeto", sa.String(length=1000), nullable=True),
        sa.Column("monto_adj", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("id_moneda_monto_adj", sa.Integer(), nullable=True),
        sa.Column("fecha_pub_adj", sa.Date(), nullable=False),
        sa.Column("num_compra", sa.String(length=50), nullable=True),
        sa.Column("anio_compra", sa.String(length=10), nullable=True),
        sa.Column("id_tipocompra", sa.String(length=10), nullable=True),
        sa.Column("subtipo_compra", sa.String(length=10), nullable=True),
        sa.Column("id_inciso", sa.Integer(), nullable=True),
        sa.Column("id_ue", sa.Integer(), nullable=True),
        sa.Column("organismo", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.String(length=512), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("id_compra", name="uq_compra_id_compra"),
    )
    op.create_index("ix_compra_id_compra", "compra", ["id_compra"], unique=True)
    op.create_index("ix_compra_fecha_pub_adj", "compra", ["fecha_pub_adj"])
    op.create_index("ix_compra_id_inciso", "compra", ["id_inciso"])
    op.create_index("ix_compra_id_ue", "compra", ["id_ue"])
    op.create_index("ix_compra_id_tipocompra", "compra", ["id_tipocompra"])

    # ------------------------------------------------------------------
    # 2. Create ``adjudicacion`` — the line-item table.
    # ------------------------------------------------------------------
    op.create_table(
        "adjudicacion",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("compra_id", sa.Integer(), nullable=False),
        sa.Column("nombre_comercial", sa.String(length=255), nullable=False),
        sa.Column("nro_doc_prov", sa.String(length=50), nullable=True),
        sa.Column("tipo_doc_prov", sa.String(length=10), nullable=True),
        sa.Column("cant_adj", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("precio_unit", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("precio_tot_imp", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("id_moneda", sa.Integer(), nullable=False),
        sa.Column("desc_articulo", sa.String(length=500), nullable=False),
        sa.Column("id_articulo", sa.String(length=50), nullable=True),
        sa.Column("amount_uyu", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.ForeignKeyConstraint(
            ["compra_id"],
            ["compra.id"],
            name="fk_adjudicacion_compra_id_compra",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_adjudicacion_compra_id", "adjudicacion", ["compra_id"])
    op.create_index("ix_adjudicacion_id_articulo", "adjudicacion", ["id_articulo"])

    # ------------------------------------------------------------------
    # 3. Create ``oferente`` — the bidder table.
    # ------------------------------------------------------------------
    op.create_table(
        "oferente",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("compra_id", sa.Integer(), nullable=False),
        sa.Column("nombre_comercial", sa.String(length=255), nullable=True),
        sa.Column("nro_doc_prov", sa.String(length=50), nullable=True),
        sa.Column("tipo_doc_prov", sa.String(length=10), nullable=True),
        sa.Column("cant_ofertada", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column(
            "precio_unit_ofertado",
            sa.Numeric(precision=14, scale=2),
            nullable=True,
        ),
        sa.Column("id_moneda", sa.Integer(), nullable=True),
        sa.Column("variacion", sa.String(length=255), nullable=True),
        sa.Column("alternativas", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ["compra_id"],
            ["compra.id"],
            name="fk_oferente_compra_id_compra",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_oferente_compra_id", "oferente", ["compra_id"])


def downgrade() -> None:
    op.drop_index("ix_oferente_compra_id", table_name="oferente")
    op.drop_table("oferente")

    op.drop_index("ix_adjudicacion_id_articulo", table_name="adjudicacion")
    op.drop_index("ix_adjudicacion_compra_id", table_name="adjudicacion")
    op.drop_table("adjudicacion")

    op.drop_index("ix_compra_id_tipocompra", table_name="compra")
    op.drop_index("ix_compra_id_ue", table_name="compra")
    op.drop_index("ix_compra_id_inciso", table_name="compra")
    op.drop_index("ix_compra_fecha_pub_adj", table_name="compra")
    op.drop_index("ix_compra_id_compra", table_name="compra")
    op.drop_table("compra")
