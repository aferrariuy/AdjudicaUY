"""Alembic environment.

Resolves the database URL from the application's Settings so migrations are
always applied against the same connection string the rest of the app uses.
"""

from __future__ import annotations

from contextlib import suppress
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

# Advisory lock key used to serialize concurrent ``alembic upgrade head``
# runs. The app and worker containers both run migrations on startup
# (see scripts/entrypoint.sh); without this lock they race on the
# ``alembic_version`` table during a deploy. The key is an arbitrary
# fixed integer scoped to this project.
_MIGRATION_LOCK_KEY = 828374628

from app.config import get_settings
from app.database import Base

# Import models so their metadata is registered on Base before autogenerate.
from app.models import (  # noqa: F401
    adjudicacion,
    compra,
    oferente,
)

config = context.config

# Override sqlalchemy.url with the value resolved from environment / .env.
# (alembic.ini leaves this blank to keep secrets out of source control.)
config.set_main_option("sqlalchemy.url", get_settings().database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live connection)."""

    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (apply against a live DB connection)."""

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Serialize concurrent migration runs (app + worker containers both
        # call ``alembic upgrade head`` on startup). PostgreSQL advisory
        # locks are session-scoped and independent of transactions, so the
        # lock survives the migration transaction below. SQLite (tests) has
        # no advisory locks — nothing to serialize there.
        is_postgres = connection.dialect.name == "postgresql"
        if is_postgres:
            connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
            )

            with context.begin_transaction():
                context.run_migrations()
        finally:
            if is_postgres:
                # If a migration failed, the connection's transaction is
                # aborted and ANY further statement fails with
                # InFailedSqlTransaction. Roll back to clear the aborted
                # state before releasing the advisory lock, and never let
                # an unlock failure mask the original migration error.
                with suppress(Exception):
                    connection.rollback()
                with suppress(Exception):
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": _MIGRATION_LOCK_KEY},
                    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
