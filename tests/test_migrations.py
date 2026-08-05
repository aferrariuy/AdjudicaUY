"""Smoke coverage for schema migrations used by the application."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect


def test_company_document_index_migration_is_sqlite_compatible() -> None:
    """The company document index can be upgraded and downgraded on SQLite."""

    alembic_migration = pytest.importorskip("alembic.migration")
    alembic_operations = pytest.importorskip("alembic.operations")
    from migrations.versions import company_document_index

    MigrationContext = alembic_migration.MigrationContext
    Operations = alembic_operations.Operations

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE adjudicacion (
                id INTEGER PRIMARY KEY,
                tipo_doc_prov VARCHAR(10),
                nro_doc_prov VARCHAR(50)
            )
            """
        )
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        original = company_document_index.op
        company_document_index.op = operations
        try:
            company_document_index.upgrade()
        finally:
            company_document_index.op = original

        index_names = {
            index["name"] for index in inspect(connection).get_indexes("adjudicacion")
        }
        assert "ix_adjudicacion_company_document" in index_names

        context = MigrationContext.configure(connection)
        operations = Operations(context)
        original = company_document_index.op
        company_document_index.op = operations
        try:
            company_document_index.downgrade()
        finally:
            company_document_index.op = original

        index_names = {
            index["name"] for index in inspect(connection).get_indexes("adjudicacion")
        }
        assert "ix_adjudicacion_company_document" not in index_names


def test_oferente_company_document_index_migration_is_sqlite_compatible() -> None:
    """The oferente document index can be upgraded and downgraded on SQLite."""

    alembic_migration = pytest.importorskip("alembic.migration")
    alembic_operations = pytest.importorskip("alembic.operations")
    from migrations.versions import oferente_company_document_index

    MigrationContext = alembic_migration.MigrationContext
    Operations = alembic_operations.Operations

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE oferente (
                id INTEGER PRIMARY KEY,
                tipo_doc_prov VARCHAR(10),
                nro_doc_prov VARCHAR(50)
            )
            """
        )
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        original = oferente_company_document_index.op
        oferente_company_document_index.op = operations
        try:
            oferente_company_document_index.upgrade()
        finally:
            oferente_company_document_index.op = original

        index_names = {
            index["name"] for index in inspect(connection).get_indexes("oferente")
        }
        assert "ix_oferente_company_document" in index_names

        context = MigrationContext.configure(connection)
        operations = Operations(context)
        original = oferente_company_document_index.op
        oferente_company_document_index.op = operations
        try:
            oferente_company_document_index.downgrade()
        finally:
            oferente_company_document_index.op = original

        index_names = {
            index["name"] for index in inspect(connection).get_indexes("oferente")
        }
        assert "ix_oferente_company_document" not in index_names
