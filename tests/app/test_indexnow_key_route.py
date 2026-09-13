"""Tests for the IndexNow key file route.

IndexNow proves that the submitter controls the host by fetching the key from an
``https://<host>/<key>.txt`` file and comparing its contents with the submitted
``key``. The route is registered only when a key is configured, so an
unconfigured deployment 404s instead of serving an empty file that would fail
verification anyway.

The key file carries no ``X-Robots-Tag``: an IndexNow key is public by design
(IndexNow itself must be able to fetch it from any host), so indexing it discloses
nothing, and adding it to the crawler-directive predicate would be logic for no
risk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient

from app.database import get_db

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

KEY = "0123456789abcdef0123456789abcdef"


def _client_for_key(monkeypatch: Any, db_session: Session, key: str) -> TestClient:
    """Build an app with ``INDEXNOW_KEY`` set to ``key`` (empty means unset).

    ``create_app`` is imported inside this helper, not at module scope: ``app.main``
    builds a module-level app on import, and that build validates settings against
    the real host allowlist, which rejects the test host unless
    ``PYTEST_CURRENT_TEST`` is already set — true while a test runs, false during
    collection.
    """

    from app.main import create_app

    monkeypatch.setenv("INDEXNOW_KEY", key)
    app = create_app()

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def test_serves_the_configured_key_as_plain_text(
    monkeypatch: Any, db_session: Session
) -> None:
    """The key file returns exactly the key, so verification can match it."""

    with _client_for_key(monkeypatch, db_session, KEY) as client:
        response = client.get(f"/{KEY}.txt")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert response.text == KEY


def test_returns_404_when_no_key_is_configured(
    monkeypatch: Any, db_session: Session
) -> None:
    """An unconfigured deployment must not advertise a key file at all."""

    with _client_for_key(monkeypatch, db_session, "") as client:
        response = client.get(f"/{KEY}.txt")

    assert response.status_code == 404


def test_returns_404_for_a_different_key(monkeypatch: Any, db_session: Session) -> None:
    """Only the configured key is served: the path is not a wildcard."""

    with _client_for_key(monkeypatch, db_session, KEY) as client:
        response = client.get("/someotherkey.txt")

    assert response.status_code == 404
