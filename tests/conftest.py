"""Pytest configuration and shared fixtures for the AdjudicaUY test suite.

This conftest provides:

* A pytest fixture that points :class:`app.config.Settings` at a throwaway
  SQLite database. The fixture is autouse so every test sees a valid
  ``DATABASE_URL`` without having to remember to set it.
* A session-scoped engine bound to a shared in-memory SQLite connection
  (so the schema created in one session is visible in the next) and a
  function-scoped session wrapper.
* A ``client`` fixture returning a FastAPI :class:`TestClient` bound to
  the test database.
* Reusable XML / RSS / record fixtures used by both the scraper and the
  route tests.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Environment — must be set BEFORE the app modules are imported so that
# :class:`app.config.Settings` picks them up on first instantiation.
# ---------------------------------------------------------------------------

_TEST_ENV: dict[str, str] = {
    "DATABASE_URL": "sqlite:///:memory:",
    "SOURCE_A_BASE_URL": "https://example.test/xml",
    "SOURCE_B_BASE_URL": "https://example.test/rss",
    "BCU_API_URL": (
        "https://cotizaciones.bcu.gub.uy/wscotizaciones/servlet/awsbcucotizaciones"
    ),
}

# Apply once at import time. Individual tests can override a variable by
# calling ``monkeypatch.setenv`` and then re-invoking ``get_settings``
# (the production app deliberately re-reads env on every call only if
# the cached value is missing — see :mod:`app.config`).
for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Generator[Engine, None, None]:
    """A single in-memory SQLite engine shared across the test session.

    Using a shared connection (``StaticPool``) means the schema created
    by ``Base.metadata.create_all`` in one session is still visible in
    the next — without it, each new connection would see a fresh,
    empty database.
    """

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # Import models so the metadata is populated.
    from app.database import Base
    from app.models import adjudication  # noqa: F401

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Generator[Session, None, None]:
    """Yield a session and roll back any changes after the test.

    The in-memory engine is shared (see :func:`engine`), so wrapping
    each test in a transaction keeps tests isolated from each other
    while still seeing the shared schema. We use a SAVEPOINT
    (nested transaction) so an ``IntegrityError`` mid-test does not
    poison the outer transaction.
    """

    connection = engine.connect()
    transaction = connection.begin_nested()
    SessionLocal = sessionmaker(
        bind=connection,
        autoflush=False,
        autocommit=False,
        future=True,
        join_transaction_mode="create_savepoint",
    )
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        try:
            transaction.rollback()
        except Exception:  # noqa: BLE001 - already rolled back by an IntegrityError path
            pass
        connection.close()


# ---------------------------------------------------------------------------
# Web app fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """A FastAPI ``TestClient`` bound to the test database.

    The app's :func:`get_db` dependency is overridden to return the
    transactional session, so route tests see exactly the rows the test
    inserted (and nothing else).
    """

    from app.database import get_db
    from app.main import create_app

    app = create_app()

    def _override_get_db() -> Generator[Session, None, None]:
        try:
            yield db_session
        finally:
            # Session lifetime is owned by the ``db_session`` fixture.
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Sample data fixtures — adjudications, RSS items, joined records
# ---------------------------------------------------------------------------


@pytest.fixture
def make_adjudication(db_session: Session):
    """Factory: persist a single :class:`Adjudication` with sensible defaults.

    Tests call this with keyword overrides for the fields they care
    about. The factory returns the persisted ORM instance so the test
    can read back auto-populated values (e.g. ``id``, ``ingested_at``).

    Each call automatically varies the unique-key triple
    ``(source_url, license_link, date)`` so multiple inserts in the
    same test do not trip the database's unique constraint. Tests that
    need explicit control over those fields pass them as overrides.
    """

    from app.models.adjudication import Adjudication

    counter = {"n": 0}

    def _factory(**overrides: Any) -> Adjudication:
        counter["n"] += 1
        n = counter["n"]
        defaults: dict[str, Any] = {
            "amount": Decimal("1000.00"),
            "currency": "UYU",
            "amount_uyu": Decimal("1000.00"),
            "winning_company": f"Empresa {n}",
            "company_document": f"2100000000{n:02d}",
            "company_document_type": "RUT",
            "organism": "Ministerio de Interior",
            "date": date(2024, 1, 15),
            "license_type": "CD",
            "article": "Laptop Dell Latitude",
            "article_quantity": Decimal("10.00"),
            "license_link": f"https://example.test/licitacion/{n}",
            "source_url": "https://example.test/xml",
        }
        payload = {**defaults, **overrides}
        instance = Adjudication(**payload)
        db_session.add(instance)
        db_session.commit()
        db_session.refresh(instance)
        return instance

    return _factory


@pytest.fixture
def make_xml_record():
    """Factory: build an :class:`XmlAdjudication` for parser/joiner tests."""

    from scraper.xml_report import XmlAdjudication

    defaults: dict[str, Any] = {
        "id_compra": "1319278",
        "fecha_pub_adj": date(2024, 1, 15),
        "id_tipocompra": "CD",
        "id_moneda_monto_adj": 20,
        "nombre_comercial": "Empresa SA",
        "nro_doc_prov": "210000000018",
        "tipo_doc_prov": "RUT",
        "cant_adj": Decimal("10.00"),
        "precio_tot_imp": Decimal("1000.00"),
        "desc_articulo": "Laptop",
        "id_moneda": 20,
    }

    def _factory(**overrides: Any) -> XmlAdjudication:
        return XmlAdjudication(**{**defaults, **overrides})

    return _factory


@pytest.fixture
def make_rss_item():
    """Factory: build an :class:`RssItem` for parser/joiner tests."""

    from scraper.rss_feed import RssItem

    defaults: dict[str, Any] = {
        "id_compra": "1319278",
        "organism": "Ministerio de Interior",
        "license_link": "https://example.test/consultas/detalle/id/1319278",
    }

    def _factory(**overrides: Any) -> RssItem:
        return RssItem(**{**defaults, **overrides})

    return _factory


@pytest.fixture
def make_joined_record():
    """Factory: build a :class:`JoinedRecord` for normalizer tests."""

    from scraper.joiner import JoinedRecord

    defaults: dict[str, Any] = {
        "id_compra": "1319278",
        "fecha_pub_adj": date(2024, 1, 15),
        "id_tipocompra": "CD",
        "id_moneda_monto_adj": 20,
        "nombre_comercial": "Empresa SA",
        "nro_doc_prov": "210000000018",
        "tipo_doc_prov": "RUT",
        "cant_adj": Decimal("10.00"),
        "precio_tot_imp": Decimal("1000.00"),
        "desc_articulo": "Laptop",
        "id_moneda": 20,
        "organism": "Ministerio de Interior",
        "license_link": "https://example.test/consultas/detalle/id/1319278",
        "source_url": "https://example.test/xml",
    }

    def _factory(**overrides: Any) -> JoinedRecord:
        return JoinedRecord(**{**defaults, **overrides})

    return _factory
