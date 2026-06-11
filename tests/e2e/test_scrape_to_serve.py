"""End-to-end smoke test: scrape → store → query → serve.

This test wires the full pipeline together **in-process**:

1. ``fetch_xml_report`` and ``fetch_rss_feed`` are pointed at canned
   XML/RSS payloads.
2. The BCU SOAP endpoint is mocked.
3. ``scraper.run_scrape`` walks the records through the joiner, the
   normalizer, and the bulk insert — writing into the in-memory SQLite
   engine shared with the FastAPI app via the ``client`` fixture from
   :mod:`conftest`.
4. The FastAPI ``TestClient`` issues a real HTTP request against the
   route, and the page must contain the inserted data.

The test mirrors the four boundaries the production ``docker-compose``
stack crosses: scraper → database → service → web app. It runs in any
environment (no Docker required) and serves as the smoke check CI
executes before deploying.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import Base, get_session_factory
from app.models.adjudication import Adjudication
from scraper import main as scraper_main


# ---------------------------------------------------------------------------
# Canned payloads
# ---------------------------------------------------------------------------

XML_REPORT = """<?xml version="1.0" encoding="UTF-8"?>
<reporte>
  <compra id_compra="1001"
          fecha_pub_adj="2024-03-15"
          id_moneda_monto_adj="20"
          id_tipocompra="CD">
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
          id_tipocompra="CD">
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
          id_tipocompra="LP">
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

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Adjudicaciones</title>
    <item>
      <title>Compra Directa - E2E Organismo A | E2E Organismo A</title>
      <link>http://www.comprasestatales.gub.uy/consultas/detalle/id/1001</link>
    </item>
    <item>
      <title>Compra Directa - E2E Organismo A | E2E Organismo A</title>
      <link>http://www.comprasestatales.gub.uy/consultas/detalle/id/1002</link>
    </item>
    <item>
      <title>Licitacion Publica - E2E Organismo B | E2E Organismo B</title>
      <link>http://www.comprasestatales.gub.uy/consultas/detalle/id/1003</link>
    </item>
  </channel>
</rss>
"""


def _make_bcu_transport() -> httpx.MockTransport:
    """Return a transport that always returns a TCC of 40.00."""

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<root><datos><TCC>40.00</TCC></datos></root>"
    ).encode("utf-8")

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
    from app.database import _engine as _prod_engine  # noqa: F401

    # Easier: use the conftest's db_session to create the test factory
    # by borrowing its engine. We do this by reading from the active
    # session in the dependency override closure.
    from tests.conftest import _TEST_ENV  # noqa: F401  (used as anchor only)

    # We don't have a direct handle on the test engine, so we build
    # a session factory from the same in-memory URL used by the
    # conftest. The schema was already created on it.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

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

        def _fake_fetch_rss(url, *, client=None, timeout=30.0):  # noqa: ARG001
            return RSS_FEED

        monkeypatch.setattr(scraper_main, "fetch_xml_report", _fake_fetch_xml)
        monkeypatch.setattr(scraper_main, "fetch_rss_feed", _fake_fetch_rss)

        # Replace BcuClient with one that uses a mock transport.
        from scraper import bcu_client as bcu_module
        from scraper.bcu_client import BcuClient

        original_bcu_client = bcu_module.BcuClient
        transport = _make_bcu_transport()

        def _patched_bcu_client(base_url, **kwargs):
            http_client = httpx.Client(transport=transport, timeout=10.0)
            return original_bcu_client(base_url, client=http_client, **kwargs)

        monkeypatch.setattr(bcu_module, "BcuClient", _patched_bcu_client)
        monkeypatch.setattr(scraper_main, "BcuClient", _patched_bcu_client)

        # --------------------------------------------------------------
        # 3. Run the scraper. The session uses our test engine.
        # --------------------------------------------------------------
        with TestSessionLocal() as session:
            inserted = scraper_main.run_scrape(session=session)

        assert inserted == 3  # 2 USD + 1 UYU

        with TestSessionLocal() as session:
            rows = session.execute(select(Adjudication)).scalars().all()

        assert len(rows) == 3
        # Ordering is by date desc, so the latest insertion (Mar 25) is
        # at index 0. The test asserts the set of companies is the
        # expected one, regardless of insertion order.
        companies = sorted(r.winning_company for r in rows)
        assert companies == sorted(
            [
                "E2E-COMPANY-Limpieza",
                "E2E-COMPANY-Laptop",
                "E2E-COMPANY-Monitor",
            ]
        )

        # USD rows have amount_uyu populated; UYU row passes through.
        by_company = {r.winning_company: r for r in rows}
        assert by_company["E2E-COMPANY-Laptop"].amount_uyu == Decimal("40000.00")
        assert by_company["E2E-COMPANY-Monitor"].amount_uyu == Decimal("20000.00")
        assert by_company["E2E-COMPANY-Limpieza"].amount_uyu == Decimal("20000.00")
        # Organism enrichment came from the RSS feed.
        organisms = {r.organism for r in rows}
        assert "E2E Organismo A" in organisms
        assert "E2E Organismo B" in organisms

        # Dates round-tripped through the database untouched.
        dates = sorted(r.date.isoformat() for r in rows)
        assert dates == ["2024-03-15", "2024-03-20", "2024-03-25"]

        # --------------------------------------------------------------
        # 4. Serve: the FastAPI app must return the inserted data.
        # --------------------------------------------------------------
        response = client.get("/")
        assert response.status_code == 200
        body = response.text
        for company in ("E2E-COMPANY-Laptop", "E2E-COMPANY-Monitor", "E2E-COMPANY-Limpieza"):
            assert company in body, f"Expected {company!r} in response body"
        assert 'id="chart-ranking"' in body
        assert 'id="chart-temporal"' in body

        # A filtered request also works.
        filtered = client.get("/adjudications?company=Laptop")
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


def test_serve_against_pre_populated_db(
    client: TestClient, make_adjudication
) -> None:
    """Insert a row directly, then verify GET /adjudications returns it.

    This is the cheapest smoke check for the "store → serve" boundary
    — it skips the scraper entirely and uses the conftest's `client`
    fixture so the FastAPI app and the inserted row share an engine.
    """

    make_adjudication(
        winning_company="DIRECT-INSERT-CO",
        organism="DIRECT-INSERT-ORG",
        article="DIRECT-INSERT-ARTICLE",
    )

    response = client.get("/adjudications")
    assert response.status_code == 200
    body = response.text
    assert "DIRECT-INSERT-CO" in body
    assert "DIRECT-INSERT-ORG" in body
    assert "DIRECT-INSERT-ARTICLE" in body
