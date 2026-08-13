"""Tests for the production SQLAlchemy engine configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import app.database as database

if TYPE_CHECKING:
    import pytest


def _reset_singletons() -> tuple[Any, Any]:
    """Return the previous engine/session singletons after forcing a rebuild."""

    previous_engine = database._engine
    previous_session_local = database._SessionLocal
    database._engine = None
    database._SessionLocal = None
    return previous_engine, previous_session_local


def _restore_singletons(previous_engine: Any, previous_session_local: Any) -> None:
    """Restore the process-wide singletons in teardown."""

    database._engine = previous_engine
    database._SessionLocal = previous_session_local


def test_get_engine_passes_explicit_postgres_pool_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lazy production engine must use the approved Postgres pool kwargs."""

    previous = _reset_singletons()
    fake_engine = object()
    create_engine_mock = Mock(return_value=fake_engine)
    settings = Mock(database_url="postgresql+psycopg://user:pw@db:5432/adjudica")

    monkeypatch.setattr(database, "create_engine", create_engine_mock)
    monkeypatch.setattr(database, "get_settings", Mock(return_value=settings))

    try:
        assert database.get_engine() is fake_engine
        create_engine_mock.assert_called_once_with(
            settings.database_url,
            pool_size=10,
            max_overflow=20,
            pool_recycle=1800,
            pool_pre_ping=True,
            future=True,
        )
    finally:
        _restore_singletons(*previous)


def test_get_engine_skips_postgres_pool_kwargs_for_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Postgres URLs must not receive Postgres-specific pool arguments.

    The in-memory SQLite test engine uses a ``SingletonThreadPool`` that
    rejects ``max_overflow``; gating the pool kwargs on the URL dialect keeps
    the production behavior explicit without breaking other backends.
    """

    previous = _reset_singletons()
    fake_engine = object()
    create_engine_mock = Mock(return_value=fake_engine)
    settings = Mock(database_url="sqlite:///:memory:")

    monkeypatch.setattr(database, "create_engine", create_engine_mock)
    monkeypatch.setattr(database, "get_settings", Mock(return_value=settings))

    try:
        assert database.get_engine() is fake_engine
        create_engine_mock.assert_called_once_with(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
        )
    finally:
        _restore_singletons(*previous)
