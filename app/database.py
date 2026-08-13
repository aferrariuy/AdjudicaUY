"""SQLAlchemy engine and session factory.

The engine and ``SessionLocal`` are created lazily so that merely importing
this module does not require the database driver to be installed (e.g. when
Alembic is run from an environment that imports ``app.models`` to register
metadata without touching the DB).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.engine import Engine


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in the project."""


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the process-wide engine, building it on first call."""

    global _engine
    if _engine is None:
        settings = get_settings()
        engine_kwargs: dict[str, Any] = {
            "pool_pre_ping": True,
            "future": True,
        }
        # Explicit pool settings for the production Postgres engine:
        # pool_pre_ping guards against stale connections after a Postgres
        # restart, pool_recycle bounds connection age, and pool_size /
        # max_overflow cap the connection budget. Gated on the dialect
        # because non-Postgres URLs (e.g. the in-memory SQLite test
        # engine, whose SingletonThreadPool rejects max_overflow) must not
        # receive Postgres-specific pool arguments.
        if make_url(settings.database_url).get_backend_name() == "postgresql":
            engine_kwargs.update(
                pool_size=10,
                max_overflow=20,
                pool_recycle=1800,
            )
        _engine = create_engine(settings.database_url, **engine_kwargs)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory, building it on first call."""

    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _SessionLocal


def get_db() -> Generator[Session]:
    """FastAPI dependency yielding a request-scoped ``Session``."""

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
