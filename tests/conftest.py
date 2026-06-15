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
* Reusable XML / CompraRow factories used by both the scraper and the
  route tests.
"""

from __future__ import annotations

import os
from collections.abc import (
    Generator,  # noqa: TC003 — pytest evaluates fixture annotations at runtime
)
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import (
    Engine,  # noqa: TC002 — pytest evaluates fixture annotations at runtime
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Environment — must be set BEFORE the app modules are imported so that
# :class:`app.config.Settings` picks them up on first instantiation.
# ---------------------------------------------------------------------------

_TEST_ENV: dict[str, str] = {
    "DATABASE_URL": "sqlite:///:memory:",
    "SOURCE_A_BASE_URL": "https://example.test/xml",
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
def engine() -> Generator[Engine]:
    """A single in-memory SQLite engine shared across the test session.

    Using a shared connection (``StaticPool``) means the schema created
    by ``Base.metadata.create_all`` in one session is still visible in
    the next — without it, each new connection would see a fresh,
    empty database.
    """

    from sqlalchemy import event

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    # SQLite does NOT enforce foreign keys by default. Enable the
    # PRAGMA on every new connection so ON DELETE CASCADE actually
    # cascades (it is a no-op on PostgreSQL in production).
    @event.listens_for(eng, "connect")
    def _enable_sqlite_fk(dbapi_connection, _connection_record):  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # Import models so the metadata is populated.
    from app.database import Base
    from app.models import (  # noqa: F401
        adjudicacion,
        compra,
        oferente,
    )

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Generator[Session]:
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
        try:  # noqa: SIM105 — already rolled back by IntegrityError path
            transaction.rollback()
        except Exception:  # noqa: BLE001, S110 — already rolled back by IntegrityError path
            pass
        connection.close()


# ---------------------------------------------------------------------------
# Web app fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient]:
    """A FastAPI ``TestClient`` bound to the test database.

    The app's :func:`get_db` dependency is overridden to return the
    transactional session, so route tests see exactly the rows the test
    inserted (and nothing else).
    """

    from app.database import get_db
    from app.main import create_app

    app = create_app()

    def _override_get_db() -> Generator[Session]:
        try:
            yield db_session
        finally:
            # Session lifetime is owned by the ``db_session`` fixture.
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Sample data factories — Compra + Adjudicacion, XmlCompra
# ---------------------------------------------------------------------------


# Field aliases — the old test suite used the flat ``adjudications``
# field names (``winning_company``, ``organism``, ``date``, ...). The
# factory below accepts both the legacy keys (translated to the new
# Compra/Adjudicacion columns) and the new keys directly, so existing
# tests can keep their old call shape while new tests can use the new
# field names without an indirection.
_LEGACY_FIELD_ALIASES: dict[str, tuple[str, str]] = {
    # legacy key          -> (target model, target column)
    "winning_company":    ("adjudicacion", "nombre_comercial"),
    "company_document":   ("adjudicacion", "nro_doc_prov"),
    "company_document_type": ("adjudicacion", "tipo_doc_prov"),
    "article":            ("adjudicacion", "desc_articulo"),
    "article_id":         ("adjudicacion", "id_articulo"),
    "article_quantity":   ("adjudicacion", "cant_adj"),
    "amount":             ("adjudicacion", "precio_tot_imp"),
    "currency":           ("adjudicacion", "__skip_currency__"),
    "date":               ("compra", "fecha_pub_adj"),
    "organism":           ("compra", "organismo"),
    "license_type":       ("compra", "id_tipocompra"),
    "license_link":       ("compra", "__skip_license_link__"),
    "source_url":         ("compra", "source_url"),
}


@pytest.fixture
def make_adjudication(db_session: Session):
    """Factory: persist a Compra + Adjudicacion pair with sensible defaults.

    Returns the :class:`Adjudicacion` instance — the row the web layer
    reads from. The factory varies ``id_compra`` per call so two inserts
    in the same test do not trip the unique constraint on the natural
    key. Both the legacy kwargs (``winning_company``, ``organism``,
    ``date``, ...) and the new direct field names
    (``nombre_comercial``, ``fecha_pub_adj`` via ``compra_overrides``,
    ...) are accepted.
    """

    from app.models.adjudicacion import Adjudicacion
    from app.models.compra import Compra

    counter = {"n": 0}

    def _factory(**overrides: Any) -> Adjudicacion:
        counter["n"] += 1
        n = counter["n"]

        # ----------------------------------------------------------------
        # Translate legacy kwargs to the new schema's columns.
        # ----------------------------------------------------------------
        adj_payload: dict[str, Any] = {
            "nombre_comercial": f"Empresa {n}",
            "nro_doc_prov": f"2100000000{n:02d}",
            "tipo_doc_prov": "RUT",
            "cant_adj": Decimal("10.00"),
            "precio_tot_imp": Decimal("1000.00"),
            "desc_articulo": "Laptop Dell Latitude",
            "id_moneda": 0,
            "id_articulo": f"{40000 + n}",
            "amount_uyu": Decimal("1000.00"),
        }
        compra_payload: dict[str, Any] = {
            "id_compra": f"compra-{n}",
            "fecha_pub_adj": date(2024, 1, 15),
            "id_tipocompra": "CD",
            "id_inciso": 4,
            "id_ue": 1,
            "organismo": "Ministerio de Interior",
            "source_url": "https://example.test/xml",
        }

        # Caller can also pass ``compra_overrides`` / ``adj_overrides``
        # to target one side explicitly. These take precedence over the
        # legacy-key translation below.
        compra_payload.update(overrides.pop("compra_overrides", {}))
        adj_payload.update(overrides.pop("adj_overrides", {}))

        # Legacy-key translation. Apply each legacy key to the correct
        # side's payload. ``currency`` and ``license_link`` are
        # display-only in the new schema and have nowhere to go — drop
        # them silently.
        for legacy_key, value in list(overrides.items()):
            if legacy_key in _LEGACY_FIELD_ALIASES:
                target_model, target_col = _LEGACY_FIELD_ALIASES[legacy_key]
                if target_col.startswith("__skip_"):
                    continue
                if target_model == "compra":
                    compra_payload[target_col] = value
                else:
                    adj_payload[target_col] = value

        # Anything left in ``overrides`` is treated as a direct
        # ``Adjudicacion`` field. This lets new tests opt into the new
        # names without changing the factory call shape. ``update`` (not
        # ``setdefault``) so the caller's override wins over the default.
        for key, value in overrides.items():
            if key in _LEGACY_FIELD_ALIASES:
                continue
            adj_payload[key] = value

        compra = Compra(**compra_payload)
        db_session.add(compra)
        db_session.flush()  # assigns Compra.id without committing

        adj = Adjudicacion(compra_id=compra.id, **adj_payload)
        db_session.add(adj)
        db_session.commit()
        db_session.refresh(adj)
        return adj

    return _factory


@pytest.fixture
def make_xml_compra():
    """Factory: build an :class:`XmlCompra` for parser tests."""

    from scraper.xml_report import XmlAdjudicacion, XmlCompra, XmlOferente

    defaults_company: dict[str, Any] = {
        "nombre_comercial": "Empresa SA",
        "nro_doc_prov": "210000000018",
        "tipo_doc_prov": "RUT",
        "cant_adj": Decimal("10.00"),
        "precio_tot_imp": Decimal("1000.00"),
        "desc_articulo": "Laptop",
        "id_moneda": 20,
        "id_articulo": "42851",
    }

    defaults_oferente: dict[str, Any] = {
        "nombre_comercial": "Bidder SA",
        "nro_doc_prov": "210000000050",
        "tipo_doc_prov": "RUT",
        "cant_ofertada": Decimal("10.00"),
        "precio_unit_ofertado": Decimal("80.00"),
        "id_moneda": 20,
        "variacion": None,
        "alternativas": None,
    }

    defaults_compra: dict[str, Any] = {
        "id_compra": "1319278",
        "fecha_pub_adj": date(2024, 1, 15),
        "id_tipocompra": "CD",
        "id_moneda_monto_adj": 20,
        "objeto": "Adquisición",
        "monto_adj": Decimal("1000.00"),
        "num_compra": "86825",
        "anio_compra": "2024",
        "subtipo_compra": None,
        "id_inciso": 3,
        "id_ue": 15,
        "adjudicaciones": [
            XmlAdjudicacion(id_compra="1319278", **defaults_company),
        ],
        "oferentes": [
            XmlOferente(id_compra="1319278", **defaults_oferente),
        ],
    }

    def _factory(**overrides: Any) -> XmlCompra:
        merged = {**defaults_compra, **overrides}
        return XmlCompra(**merged)

    return _factory
