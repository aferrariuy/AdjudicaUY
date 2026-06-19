"""SQLAlchemy model for the ``oferente`` table.

One row per ``<oferente>`` child element of a ``<compra>`` in the
upstream XML report. The row is linked to its parent :class:`Compra`
via ``compra_id`` with ``ON DELETE CASCADE``. The oferente record
describes one bidder on the purchase; unlike the ``adjudicacion``
table, this is a flat row with no children of its own and no
normalized-currency field (currencies stay in their raw ``id_moneda``
form on the row, since the web app does not aggregate oferentes by
amount — see data-storage spec, "Oferente Table" requirement).
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


class Oferente(Base):
    __tablename__ = "oferente"

    id = Column(Integer, primary_key=True)

    compra_id = Column(
        Integer,
        ForeignKey("compra.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Bidder identification (mirrors the adjudicacion columns — same
    # XML element shape on the wire, different semantic role).
    nombre_comercial = Column(String(255), nullable=True)
    nro_doc_prov = Column(String(50), nullable=True)
    tipo_doc_prov = Column(String(10), nullable=True)

    # Pricing fields the XML commonly carries for oferentes. Stored
    # as nullable so the row tolerates a missing attribute and the
    # schema tolerates a future XML change.
    cant_ofertada = Column(Numeric(14, 2), nullable=True)
    precio_unit_ofertado = Column(Numeric(14, 2), nullable=True)
    id_moneda = Column(Integer, nullable=True)

    # Variant / alternative flags the XML may carry. Kept narrow on
    # purpose — new attributes are picked up by the parser's
    # "unknown attribute" warning so the team can decide whether to
    # add a column.
    variacion = Column(String(255), nullable=True)
    alternativas = Column(String(500), nullable=True)

    # Back-ref kept on the child side for ORM lookups by id; the
    # web layer does not use the relationship — it joins Compra →
    # Oferente directly when needed.
    compra = relationship("Compra", lazy="raise")

    __table_args__ = (
        # One bidder entry per (compra, company) pair. The pair is
        # the natural key — re-runs of the scraper hit ON CONFLICT
        # DO NOTHING and stay idempotent.
        UniqueConstraint(
            "compra_id",
            "nombre_comercial",
            name="uq_oferente_bidder",
        ),
        Index("ix_oferente_compra_id", "compra_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"<Oferente id={self.id} compra_id={self.compra_id} "
            f"nombre_comercial={self.nombre_comercial!r}>"
        )
