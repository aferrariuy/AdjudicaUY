"""Tests for the root ``/favicon.ico`` route.

Browsers and crawlers probe ``/favicon.ico`` by convention, without reading the
``<link rel="icon">`` tags first, so a site that only serves its icon from
``/static/`` answers those probes with a 404. Google treats a successful
favicon fetch as one of the signals that tie a search result to the site, which
is why the icon is also served at the root.

The route deliberately does not join the immutable ``/static/`` cache rule:
that rule is justified by content-hashed filenames and ``favicon.ico`` is not
hashed.

``HEAD`` comes from the shared route class and must answer without streaming the
icon bytes. Starlette enforces that inside ``FileResponse``, which sends a
header-only body message when the request method is HEAD, so the test below
asserts what this client can see — that the probe is answered — rather than
restating a guarantee the in-process client cannot observe, because it discards
whatever body a HEAD response carries.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

FAVICON_PATH = Path(__file__).resolve().parents[2] / "app" / "static" / "favicon.ico"


def test_get_favicon_serves_the_static_file(client: TestClient) -> None:
    """GET /favicon.ico returns the icon bytes with the ICO media type.

    Byte-equality with ``app/static/favicon.ico`` is the assertion that matters:
    any other body (a redirect target, an HTML error page, a placeholder) would
    satisfy a bare 200 while leaving the crawler probe unresolved.
    """

    response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/vnd.microsoft.icon"
    assert response.content == FAVICON_PATH.read_bytes()
    assert response.content


def test_head_favicon_is_answered(client: TestClient) -> None:
    """HEAD /favicon.ico is answered instead of refused.

    The empty body asserted here is the client-side convention the shared HEAD
    test already applies to every GET route (``tests/test_head_support.py``). It
    is not evidence about the wire: this client drops whatever a HEAD response
    carries. That no icon bytes are streamed is ``FileResponse``'s behavior.
    """

    response = client.head("/favicon.ico")

    assert response.status_code == 200
    assert response.content == b""


def test_missing_favicon_answers_404(
    client: TestClient, monkeypatch: Any, tmp_path: Path
) -> None:
    """An image without the icon answers 404 instead of failing the request.

    This route exists to answer a probe, so it owes the prober a definite
    answer: a ``FileResponse`` built for a path that is not there raises, which
    would turn every favicon probe of a checkout without the static asset into
    a 500. The handler reads the module-level path when it runs, which is why
    patching that name here is what exercises the branch.
    """

    from app import main

    monkeypatch.setattr(main, "FAVICON_PATH", tmp_path / "absent.ico")

    response = client.get("/favicon.ico")

    assert response.status_code == 404
