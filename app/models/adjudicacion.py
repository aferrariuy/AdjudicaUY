"""SQLAlchemy model for the ``adjudicacion`` table.

One row per ``<adjudicacion>`` child element of a ``<compra>`` in the
upstream XML report. The row is linked to its parent :class:`Compra` via
``compra_id`` with ``ON DELETE CASCADE`` so dropping a compra cleans up
its adjudicaciones in a single statement. Every XML attribute is stored
as a nullable column so the table tolerates XML changes without DDL
churn. ``amount_uyu`` is populated at ingest time by the normalizer;
non-convertible currencies store ``NULL`` there (see data-ingestion
spec, "Non-convertible adjudicated line item" scenario).
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class Adjudicacion(Base):
    __tablename__ = "adjudicacion"

    id = Column(Integer, primary_key=True)

    compra_id = Column(
        Integer,
        ForeignKey("compra.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Winning-company identification
    nombre_comercial = Column(String(255), nullable=False)
    nro_doc_prov = Column(String(50), nullable=True)
    tipo_doc_prov = Column(String(10), nullable=True)

    # Pricing
    cant_adj = Column(Numeric(14, 2), nullable=True)
    precio_unit = Column(Numeric(14, 2), nullable=True)
    precio_tot_imp = Column(Numeric(14, 2), nullable=False)
    id_moneda = Column(Integer, nullable=False)

    # Article description (one adjudication = one line item)
    desc_articulo = Column(String(500), nullable=False)
    id_articulo = Column(String(50), nullable=True)

    # Populated at ingest by the normalizer (BCU rate for id_moneda on
    # fecha_pub_adj). NULL when the currency is non-convertible.
    amount_uyu = Column(Numeric(14, 2), nullable=True)

    # Back-ref kept on the child side for ORM lookups by id; the
    # web layer does not use the relationship — it joins Compra →
    # Adjudicacion directly when needed.
    compra = relationship("Compra", lazy="raise")

    __table_args__ = (
        # One adjudication = one line item awarded to one company on
        # one purchase. The triple (compra, company, article) is the
        # natural key — re-runs of the scraper hit ON CONFLICT
        # DO NOTHING and stay idempotent.
        UniqueConstraint(
            "compra_id",
            "nombre_comercial",
            "desc_articulo",
            name="uq_adjudicacion_line_item",
        ),
        Index("ix_adjudicacion_compra_id", "compra_id"),
        Index("ix_adjudicacion_id_articulo", "id_articulo"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"<Adjudicacion id={self.id} compra_id={self.compra_id} "
            f"company={self.nombre_comercial!r}>"
        )
