"""Regression tests for HEAD support on GET routes."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("path", ["/", "/healthz"])
def test_head_get_routes_return_empty_success(
    client: TestClient, path: str
) -> None:
    response = client.head(path)

    assert response.status_code == 200
    assert response.content == b""
    assert "HEAD" in response.headers["allow"]


def test_head_organism_route_returns_empty_success(
    client: TestClient, make_adjudication: Any
) -> None:
    make_adjudication(organism="HEAD-ORGANISM", date=date(2024, 3, 1))

    response = client.head("/organism/HEAD-ORGANISM")

    assert response.status_code == 200
    assert response.content == b""
    assert "HEAD" in response.headers["allow"]


def test_head_csv_export_returns_empty_success(
    client: TestClient, make_adjudication: Any
) -> None:
    make_adjudication(date=date(2024, 3, 1))

    response = client.head("/adjudications/export?date_from=2024-01-01")

    assert response.status_code == 200
    assert response.content == b""
    assert "HEAD" in response.headers["allow"]


def test_post_only_request_does_not_gain_head() -> None:
    from fastapi import APIRouter, FastAPI

    from app.main import HeadAwareAPIRoute

    app = FastAPI()
    router = APIRouter(route_class=HeadAwareAPIRoute)

    @router.post("/post-only")
    def post_only() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    local_client = TestClient(app)
    response = local_client.head("/post-only")

    assert response.status_code == 405
    assert "HEAD" not in response.headers.get("allow", "")
    local_client.close()
