"""Add a partial lookup index for oferente document identities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "oferente_company_document_index"
down_revision: str | Sequence[str] | None = "company_document_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create a document-pair index, excluding incomplete identities."""

    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "ix_oferente_company_document",
            "oferente",
            ["tipo_doc_prov", "nro_doc_prov"],
            postgresql_where=("tipo_doc_prov IS NOT NULL AND nro_doc_prov IS NOT NULL"),
        )
    else:
        # SQLite's test path receives a portable composite index. The
        # PostgreSQL-only partial predicate is intentionally omitted.
        op.create_index(
            "ix_oferente_company_document",
            "oferente",
            ["tipo_doc_prov", "nro_doc_prov"],
        )


def downgrade() -> None:
    """Remove the oferente document-pair lookup index."""

    op.drop_index("ix_oferente_company_document", table_name="oferente")
