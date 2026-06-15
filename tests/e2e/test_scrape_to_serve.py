"""End-to-end smoke test: scrape → store → query → serve.

This test wires the full pipeline together **in-process**:

1. ``fetch_xml_report`` is pointed at a canned XML payload.
2. The BCU SOAP endpoint is mocked.
3. ``scraper.run_scrape`` walks the records through the normalizer and
   the bulk insert — writing into the in-memory SQLite engine shared
   with the FastAPI app via the ``client`` fixture from :mod:`conftest`.
4. The FastAPI ``TestClient`` issues a real HTTP request against the
   route, and the page must contain the inserted data.

The test mirrors the four boundaries the production ``docker-compose``
stack crosses: scraper → database → service → web app. It runs in any
environment (no Docker required) and serves as the smoke check CI
executes before deploying.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select

if TYPE_CHECKING:
    import pytest
    from fastapi.testclient import TestClient

from app.database import Base
from app.models.adjudicacion import Adjudicacion
from app.models.compra import Compra
from scraper import main as scraper_main

# ---------------------------------------------------------------------------
# Canned payloads
# ---------------------------------------------------------------------------

XML_REPORT = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1001"
          fecha_pub_adj="2024-03-15"
          id_moneda_monto_adj="20"
          id_tipocompra="CD"
          id_inciso="4"
          id_ue="1">
    <adjudicaciones>
      <adjudicacion nombre_comercial="E2E-COMPANY-Laptop"
                    nro_doc_prov="210000000101"
                    tipo_doc_prov="RUT"
                    cant_adj="5.00"
                    precio_tot_imp="1000.00"
                    desc_articulo="E2E-ARTICLE-Laptop"
                    id_moneda="20" />
    </adjudicaciones>
  </compra>
  <compra id_compra="1002"
          fecha_pub_adj="2024-03-20"
          id_moneda_monto_adj="20"
          id_tipocompra="CD"
          id_inciso="4"
          id_ue="1">
    <adjudicaciones>
      <adjudicacion nombre_comercial="E2E-COMPANY-Monitor"
                    nro_doc_prov="210000000102"
                    tipo_doc_prov="RUT"
                    cant_adj="2.00"
                    precio_tot_imp="500.00"
                    desc_articulo="E2E-ARTICLE-Monitor"
                    id_moneda="20" />
    </adjudicaciones>
  </compra>
  <compra id_compra="1003"
          fecha_pub_adj="2024-03-25"
          id_moneda_monto_adj="0"
          id_tipocompra="LP"
          id_inciso="4"
          id_ue="1">
    <adjudicaciones>
      <adjudicacion nombre_comercial="E2E-COMPANY-Limpieza"
                    nro_doc_prov="210000000103"
                    tipo_doc_prov="RUT"
                    cant_adj="1.00"
                    precio_tot_imp="20000.00"
                    desc_articulo="E2E-ARTICLE-Limpieza"
                    id_moneda="0" />
    </adjudicaciones>
  </compra>
</reporte>
"""


def _make_bcu_transport() -> httpx.MockTransport:
    """Return a transport that always returns a TCC of 40.00."""

    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b"<root><datos><TCC>40.00</TCC></datos></root>"
    )

    def _handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=body)

    return httpx.MockTransport(_handler)


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


def test_scrape_store_query_serve_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """Full pipeline: ingest the canned sources, persist, query, serve.

    The ``client`` fixture binds the FastAPI app to a fresh in-memory
    SQLite engine. We override the production session factory so the
    scraper writes to the same engine.
    """

    # ------------------------------------------------------------------
    # 1. Replace the production session factory with one bound to the
    #    test engine. The conftest already created the schema on the
    #    test engine (``Base.metadata.create_all``).
    # ------------------------------------------------------------------
    test_engine = client.app.dependency_overrides  # ensure app built
    # Get the actual test engine by accessing the override's session.
    # We don't have a direct handle on the test engine, so we build
    # a session factory from the same in-memory URL used by the
    # conftest. The schema was already created on it.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import _engine as _prod_engine  # noqa: F401

    # Easier: use the conftest's db_session to create the test factory
    # by borrowing its engine. We do this by reading from the active
    # session in the dependency override closure.
    from tests.conftest import _TEST_ENV  # noqa: F401  (used as anchor only)

    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(test_engine)
    TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, future=True)

    # Patch the production app's session factory so the scraper writes
    # to the test engine.
    import app.database as app_database

    app_database._engine = test_engine  # type: ignore[attr-defined]
    app_database._SessionLocal = TestSessionLocal  # type: ignore[attr-defined]

    # Also rebind the FastAPI app's dependency override to use the
    # test engine — the fixture's session is on a different engine.
    from app.database import get_db

    def _override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    client.app.dependency_overrides[get_db] = _override_get_db

    try:
        # --------------------------------------------------------------
        # 2. Patch the scraper's fetches to return canned payloads.
        # --------------------------------------------------------------
        def _fake_fetch_xml(url, *, client=None, timeout=30.0):  # noqa: ARG001
            return XML_REPORT

        monkeypatch.setattr(scraper_main, "fetch_xml_report", _fake_fetch_xml)

        # Replace BcuClient with one that uses a mock transport.
        from scraper import bcu_client as bcu_module

        original_bcu_client = bcu_module.BcuClient
        transport = _make_bcu_transport()

        def _patched_bcu_client(base_url, **kwargs):
            # The production scraper now shares its ``httpx.Client`` with
            # the BCU client for connection pooling. Always force the BCU
            # SOAP calls through the mock transport so the test does not
            # reach the real BCU server, regardless of the caller's
            # calling convention.
            kwargs["client"] = httpx.Client(transport=transport, timeout=10.0)
            return original_bcu_client(base_url, **kwargs)

        monkeypatch.setattr(bcu_module, "BcuClient", _patched_bcu_client)
        monkeypatch.setattr(scraper_main, "BcuClient", _patched_bcu_client)

        # --------------------------------------------------------------
        # 3. Run the scraper. The session uses our test engine.
        # --------------------------------------------------------------
        with TestSessionLocal() as session:
            inserted = scraper_main.run_scrape(session=session)

        assert inserted == 3  # 2 USD + 1 UYU

        with TestSessionLocal() as session:
            compras = session.execute(select(Compra)).scalars().all()
            adjs = session.execute(select(Adjudicacion)).scalars().all()

        assert len(compras) == 3
        assert len(adjs) == 3

        companies = sorted(a.nombre_comercial for a in adjs)
        assert companies == sorted(
            [
                "E2E-COMPANY-Limpieza",
                "E2E-COMPANY-Laptop",
                "E2E-COMPANY-Monitor",
            ]
        )

        by_company = {a.nombre_comercial: a for a in adjs}
        assert by_company["E2E-COMPANY-Laptop"].amount_uyu == Decimal("40000.00")
        assert by_company["E2E-COMPANY-Monitor"].amount_uyu == Decimal("20000.00")
        assert by_company["E2E-COMPANY-Limpieza"].amount_uyu == Decimal("20000.00")
        # Organism enrichment comes from the (id_inciso, id_ue) static lookup —
        # all three records use (4, 1) → "Secretaría del Ministerio del Interior".
        organisms = {c.organismo for c in compras}
        assert organisms == {"Secretaría del Ministerio del Interior"}

        # Dates round-tripped through the database untouched.
        dates = sorted(c.fecha_pub_adj.isoformat() for c in compras)
        assert dates == ["2024-03-15", "2024-03-20", "2024-03-25"]

        # --------------------------------------------------------------
        # 4. Serve: the FastAPI app must return the inserted data.
        # --------------------------------------------------------------
        # The canned XML carries 2024 dates; pass explicit date params
        # to bypass the route's current-year default filter.
        response = client.get("/?date_from=2024-01-01&date_to=2024-12-31")
        assert response.status_code == 200
        body = response.text
        for company in (
            "E2E-COMPANY-Laptop",
            "E2E-COMPANY-Monitor",
            "E2E-COMPANY-Limpieza",
        ):
            assert company in body, f"Expected {company!r} in response body"
        assert 'id="chart-ranking"' in body
        assert 'id="chart-organism-ranking"' in body

        # A filtered request also works.
        filtered = client.get(
            "/adjudications?company=Laptop&date_from=2024-01-01&date_to=2024-12-31"
        )
        assert filtered.status_code == 200
        assert "E2E-COMPANY-Laptop" in filtered.text
        assert "E2E-COMPANY-Monitor" not in filtered.text
        assert "E2E-COMPANY-Limpieza" not in filtered.text

    finally:
        # Reset the production cache so other tests are unaffected.
        app_database._engine = None  # type: ignore[attr-defined]
        app_database._SessionLocal = None  # type: ignore[attr-defined]
        test_engine.dispose()


# ---------------------------------------------------------------------------
# Simpler standalone smoke check — just the "store + serve" half
# ---------------------------------------------------------------------------


def test_serve_against_pre_populated_db(client: TestClient, make_adjudication) -> None:
    """Insert a row directly, then verify GET /adjudications returns it.

    This is the cheapest smoke check for the "store → serve" boundary
    — it skips the scraper entirely and uses the conftest's `client`
    fixture so the FastAPI app and the inserted row share an engine.
    """

    make_adjudication(
        winning_company="DIRECT-INSERT-CO",
        organism="DIRECT-INSERT-ORG",
        article="DIRECT-INSERT-ARTICLE",
        date=date(date.today().year, 3, 1),
    )

    response = client.get("/adjudications")
    assert response.status_code == 200
    body = response.text
    assert "DIRECT-INSERT-CO" in body
    assert "DIRECT-INSERT-ORG" in body
    assert "DIRECT-INSERT-ARTICLE" in body
