"""SQLAlchemy model for the ``compra`` table.

One row per ``<compra>`` element in the upstream XML report. The natural key
``id_compra`` (a string supplied by the procurement system) is unique; the
row carries every other XML attribute as a nullable column so the schema
tolerates XML changes without DDL churn. Indexes on ``fecha_pub_adj``,
``id_inciso``, ``id_ue``, and ``id_tipocompra`` cover the filter and
ordering queries the web app issues (see data-storage spec, "Indexes
for filter columns" scenario).
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)

from app.database import Base


class Compra(Base):
    __tablename__ = "compra"

    id = Column(Integer, primary_key=True)

    # Natural key — unique across the whole procurement system.
    id_compra = Column(String(50), nullable=False)

    # Publication-level metadata
    objeto = Column(String(1000), nullable=True)
    monto_adj = Column(Numeric(14, 2), nullable=True)
    id_moneda_monto_adj = Column(Integer, nullable=True)
    fecha_pub_adj = Column(Date, nullable=False)

    # Procurement-system bookkeeping
    num_compra = Column(String(50), nullable=True)
    anio_compra = Column(String(10), nullable=True)
    id_tipocompra = Column(String(10), nullable=True)
    subtipo_compra = Column(String(10), nullable=True)
    id_inciso = Column(Integer, nullable=True)
    id_ue = Column(Integer, nullable=True)
    # Alternative organism identifier carried by some <compra> blocks
    # instead of the (id_inciso, id_ue) pair. Resolved via the static
    # UCC codiguera in ``scraper.ucc_lookup``. Nullable: most purchases
    # carry (id_inciso, id_ue) only.
    id_ucc = Column(Integer, nullable=True)

    # Resolved at ingest time from the static (id_inciso, id_ue) lookup
    # (see ``scraper.organism_lookup``). Nullable because the lookup may
    # not cover every pair — the pipeline logs a warning and stores NULL.
    organismo = Column(String(255), nullable=True)

    # Provenance
    source_url = Column(String(512), nullable=True)
    ingested_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("id_compra", name="uq_compra_id_compra"),
        Index("ix_compra_id_compra", "id_compra", unique=True),
        Index("ix_compra_fecha_pub_adj", "fecha_pub_adj"),
        Index("ix_compra_id_inciso", "id_inciso"),
        Index("ix_compra_id_ue", "id_ue"),
        Index("ix_compra_id_tipocompra", "id_tipocompra"),
        Index("ix_compra_id_ucc", "id_ucc"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"<Compra id={self.id} id_compra={self.id_compra!r} "
            f"fecha_pub_adj={self.fecha_pub_adj}>"
        )
