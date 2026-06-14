"""increase company_document to varchar(50)

Revision ID: 003_increase_company_document
Revises: 002_add_article_id
Create Date: 2026-06-14 00:00:00

Some Brazilian CNPJ documents (e.g. BRA11.252.642/0010-95) are 21
characters long and were being rejected by the VARCHAR(20) constraint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "003_increase_company_document"
down_revision: str | Sequence[str] | None = "002_add_article_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "adjudications",
        "company_document",
        existing_type=sa.String(length=20),
        type_=sa.String(length=50),
    )


def downgrade() -> None:
    op.alter_column(
        "adjudications",
        "company_document",
        existing_type=sa.String(length=50),
        type_=sa.String(length=20),
    )
