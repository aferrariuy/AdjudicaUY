"""Unit tests for :mod:`scraper.indexnow`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

import scraper.retry as retry_module
from scraper.indexnow import INDEXNOW_ENDPOINT, MAX_URLS_PER_REQUEST, submit_urls

if TYPE_CHECKING:
    from collections.abc import Callable

_SITE_URL = "https://example.test"
_KEY = "abc123"


def _make_handler(
    call_log: list[httpx.Request] | None = None,
    status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a transport handler that records requests and answers ``status``."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if call_log is not None:
            call_log.append(request)
        return httpx.Response(status)

    return _handler


def _only_payload(call_log: list[httpx.Request]) -> dict[str, object]:
    """Return the decoded JSON body of the single request in ``call_log``."""

    assert len(call_log) == 1
    payload = json.loads(call_log[0].content)
    assert isinstance(payload, dict)
    return payload


def test_successful_submission_posts_exact_payload() -> None:
    """A successful submission reports the sent count and the documented payload."""

    call_log: list[httpx.Request] = []
    transport = httpx.MockTransport(_make_handler(call_log))
    with httpx.Client(transport=transport) as http_client:
        result = submit_urls(
            ["https://example.test/a", "https://example.test/b"],
            site_url=_SITE_URL,
            key=_KEY,
            client=http_client,
        )

    assert result.status == "submitted"
    assert result.url_count == 2
    assert result.dropped_count == 0
    assert result.detail is None

    assert len(call_log) == 1
    request = call_log[0]
    assert request.method == "POST"
    assert str(request.url) == INDEXNOW_ENDPOINT
    assert request.headers["content-type"].startswith("application/json")
    assert _only_payload(call_log) == {
        "host": "example.test",
        "key": _KEY,
        "keyLocation": "https://example.test/abc123.txt",
        "urlList": ["https://example.test/a", "https://example.test/b"],
    }


@pytest.mark.parametrize("missing_key", [None, "", "   "])
def test_missing_key_skips_without_request(missing_key: str | None) -> None:
    """A missing key disables the feature instead of failing a scrape."""

    call_log: list[httpx.Request] = []
    transport = httpx.MockTransport(_make_handler(call_log))
    with httpx.Client(transport=transport) as http_client:
        result = submit_urls(
            ["https://example.test/a"],
            site_url=_SITE_URL,
            key=missing_key,
            client=http_client,
        )

    assert result.status == "skipped"
    assert result.url_count == 0
    assert result.dropped_count == 0
    assert result.detail is not None
    assert "key" in result.detail
    assert call_log == []


def test_blank_site_url_skips_without_request() -> None:
    """An unconfigured site URL disables the feature instead of failing a scrape."""

    call_log: list[httpx.Request] = []
    transport = httpx.MockTransport(_make_handler(call_log))
    with httpx.Client(transport=transport) as http_client:
        result = submit_urls(
            ["https://example.test/a"],
            site_url="   ",
            key=_KEY,
            client=http_client,
        )

    assert result.status == "skipped"
    assert result.url_count == 0
    assert result.detail is not None
    assert "site_url" in result.detail
    assert call_log == []


def test_empty_url_iterable_skips_without_request() -> None:
    """Nothing to announce means nothing to send, not a failed submission."""

    call_log: list[httpx.Request] = []
    transport = httpx.MockTransport(_make_handler(call_log))
    with httpx.Client(transport=transport) as http_client:
        result = submit_urls([], site_url=_SITE_URL, key=_KEY, client=http_client)

    assert result.status == "skipped"
    assert result.url_count == 0
    assert result.detail is not None
    assert call_log == []


def test_duplicate_urls_collapse_in_first_seen_order() -> None:
    """One URL announced twice in a scrape is announced to IndexNow once."""

    call_log: list[httpx.Request] = []
    transport = httpx.MockTransport(_make_handler(call_log))
    with httpx.Client(transport=transport) as http_client:
        result = submit_urls(
            [
                "https://example.test/b",
                "https://example.test/a",
                "https://example.test/b",
                "https://example.test/a",
            ],
            site_url=_SITE_URL,
            key=_KEY,
            client=http_client,
        )

    assert result.status == "submitted"
    assert result.url_count == 2
    assert _only_payload(call_log)["urlList"] == [
        "https://example.test/b",
        "https://example.test/a",
    ]


def test_urls_above_the_per_request_cap_are_dropped_and_reported() -> None:
    """Excess URLs are capped and surfaced instead of silently lost or rejected."""

    urls = [
        f"https://example.test/p/{index}" for index in range(MAX_URLS_PER_REQUEST + 3)
    ]

    call_log: list[httpx.Request] = []
    transport = httpx.MockTransport(_make_handler(call_log))
    with httpx.Client(transport=transport) as http_client:
        result = submit_urls(urls, site_url=_SITE_URL, key=_KEY, client=http_client)

    assert result.status == "submitted"
    assert result.url_count == MAX_URLS_PER_REQUEST
    assert result.dropped_count == 3
    assert _only_payload(call_log)["urlList"] == urls[:MAX_URLS_PER_REQUEST]


def test_rejected_response_fails_without_raising() -> None:
    """A rejected payload is reported as failed and not retried."""

    call_log: list[httpx.Request] = []
    transport = httpx.MockTransport(_make_handler(call_log, status=422))
    with httpx.Client(transport=transport) as http_client:
        result = submit_urls(
            ["https://example.test/a"],
            site_url=_SITE_URL,
            key=_KEY,
            client=http_client,
        )

    assert result.status == "failed"
    assert result.url_count == 0
    assert result.detail is not None
    assert "422" in result.detail
    assert len(call_log) == 1


def test_transport_error_fails_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """A network error is reported, never propagated into the scrape."""

    monkeypatch.setattr(retry_module.time, "sleep", lambda _seconds: None)

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as http_client:
        result = submit_urls(
            ["https://example.test/a"],
            site_url=_SITE_URL,
            key=_KEY,
            client=http_client,
        )

    assert result.status == "failed"
    assert result.url_count == 0
    assert result.detail is not None


def test_key_location_normalizes_a_trailing_slash() -> None:
    """The key file URL is absolute whether or not site_url ends in a slash."""

    call_log: list[httpx.Request] = []
    transport = httpx.MockTransport(_make_handler(call_log))
    with httpx.Client(transport=transport) as http_client:
        result = submit_urls(
            ["https://example.test/a"],
            site_url="https://example.test/",
            key=_KEY,
            client=http_client,
        )

    assert result.status == "submitted"
    payload = _only_payload(call_log)
    assert payload["host"] == "example.test"
    assert payload["keyLocation"] == "https://example.test/abc123.txt"


def test_injected_client_is_not_closed() -> None:
    """The caller's connection pool survives the notification."""

    transport = httpx.MockTransport(_make_handler())
    with httpx.Client(transport=transport) as http_client:
        result = submit_urls(
            ["https://example.test/a"],
            site_url=_SITE_URL,
            key=_KEY,
            client=http_client,
        )
        assert result.status == "submitted"
        assert http_client.is_closed is False
