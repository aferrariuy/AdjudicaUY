"""Best-effort IndexNow notifier for the URLs a scrape touched.

IndexNow is a shared submission endpoint through which Bing, Yandex and Seznam
learn about changed URLs immediately instead of waiting for their next crawl.
The notification is a courtesy side channel on top of a **successful** scrape:
an unreachable search engine, a missing key, or a rejected payload must never
propagate into the worker. Every failure path therefore returns an
:class:`IndexNowResult` instead of raising, so callers can invoke
:func:`submit_urls` unconditionally at the end of a run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import httpx

from scraper.retry import retry_with_backoff

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"

# IndexNow documents a hard 10,000-URL ceiling per request. Going over it makes
# the whole batch invalid, so the surplus is reported through ``dropped_count``
# rather than truncated silently.
MAX_URLS_PER_REQUEST = 10_000

# Backoff shared with the BCU client so every outbound integration retries on
# the same rhythm (1s, 3s, 9s), with jitter to avoid lockstep retries.
_INDEXNOW_BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 3.0, 9.0)
_INDEXNOW_BACKOFF_JITTER = 1.0

# Only transport failures are retried. A non-success status is terminal: the
# endpoint answers 4xx for a malformed or over-quota submission, and repeating
# that request cannot change the outcome.
_INDEXNOW_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (httpx.HTTPError,)

# IndexNow acknowledges a valid submission with 200 (accepted) or 202
# (accepted, validation pending).
_SUCCESS_STATUS_CODES = frozenset({200, 202})


@dataclass(frozen=True)
class IndexNowResult:
    """Outcome of one notification attempt."""

    status: Literal["skipped", "submitted", "failed"]
    url_count: int
    dropped_count: int
    detail: str | None


class _IndexNowRejectedError(Exception):
    """IndexNow answered with a non-success status.

    Deliberately *not* an ``httpx.HTTPError``: the shared retry helper treats
    those as transient, and a rejected payload is not worth resending.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def submit_urls(
    urls: Iterable[str],
    *,
    site_url: str,
    key: str | None,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> IndexNowResult:
    """Announce ``urls`` to IndexNow, reporting rather than raising failures.

    Parameters
    ----------
    urls:
        Changed URLs. Duplicates collapse to their first occurrence and the
        list is truncated to :data:`MAX_URLS_PER_REQUEST`.
    site_url:
        Absolute site root used to derive the payload ``host`` and the
        ``keyLocation`` of the served key file.
    key:
        IndexNow key. ``None`` or blank disables the feature (a skip, not an
        error), which lets an unconfigured deployment run the scraper normally.
    client:
        Optional caller-owned HTTP client. When omitted, a short-lived client
        is created and closed here; an injected client is never closed.
    timeout:
        Per-request timeout in seconds.

    Returns
    -------
    IndexNowResult
        ``skipped`` when the feature is unconfigured or has nothing to send,
        ``submitted`` when the endpoint accepted the batch, ``failed`` when the
        attempt was rejected or the network fell over. Never raises.
    """

    # ``dropped_count`` stays visible to the failure handlers below, so a
    # truncated batch that then fails still reports its truncation.
    dropped_count = 0
    try:
        if not key or not key.strip():
            logger.debug("IndexNow skipped: no key configured")
            return IndexNowResult(
                status="skipped",
                url_count=0,
                dropped_count=0,
                detail="no IndexNow key configured",
            )

        site_root = site_url.strip()
        if not site_root:
            logger.debug("IndexNow skipped: no site_url configured")
            return IndexNowResult(
                status="skipped",
                url_count=0,
                dropped_count=0,
                detail="no site_url configured",
            )

        unique_urls = list(dict.fromkeys(urls))
        if not unique_urls:
            logger.debug("IndexNow skipped: no URLs to submit")
            return IndexNowResult(
                status="skipped",
                url_count=0,
                dropped_count=0,
                detail="no URLs to submit",
            )

        dropped_count = max(0, len(unique_urls) - MAX_URLS_PER_REQUEST)
        if dropped_count:
            logger.warning(
                "IndexNow per-request cap: dropping %d URL(s) beyond the %d maximum",
                dropped_count,
                MAX_URLS_PER_REQUEST,
            )
        batch = unique_urls[:MAX_URLS_PER_REQUEST]

        payload: dict[str, object] = {
            "host": urlsplit(site_root).netloc,
            "key": key,
            "keyLocation": f"{site_root.rstrip('/')}/{key}.txt",
            "urlList": batch,
        }
        response = _send(payload, client=client, timeout=timeout)
    except _IndexNowRejectedError as exc:
        logger.warning(
            "IndexNow submission failed: endpoint answered HTTP %s",
            exc.status_code,
        )
        return IndexNowResult(
            status="failed",
            url_count=0,
            dropped_count=dropped_count,
            detail=f"HTTP {exc.status_code}",
        )
    except Exception as exc:
        logger.warning("IndexNow submission failed: %s", exc)
        return IndexNowResult(
            status="failed",
            url_count=0,
            dropped_count=dropped_count,
            detail=str(exc),
        )

    logger.info(
        "IndexNow accepted %d URL(s): HTTP %s",
        len(batch),
        response.status_code,
    )
    return IndexNowResult(
        status="submitted",
        url_count=len(batch),
        dropped_count=dropped_count,
        detail=None,
    )


def _send(
    payload: dict[str, object],
    *,
    client: httpx.Client | None,
    timeout: float,
) -> httpx.Response:
    """POST ``payload``, retrying transport failures and owning no injected client."""

    if client is not None:
        return _post_with_retry(client, payload, timeout=timeout)

    with httpx.Client(timeout=timeout) as owned_client:
        return _post_with_retry(owned_client, payload, timeout=timeout)


def _post_with_retry(
    client: httpx.Client,
    payload: dict[str, object],
    *,
    timeout: float,
) -> httpx.Response:
    """POST once per attempt, retrying only transient transport errors."""

    def _post() -> httpx.Response:
        response = client.post(INDEXNOW_ENDPOINT, json=payload, timeout=timeout)
        if response.status_code not in _SUCCESS_STATUS_CODES:
            raise _IndexNowRejectedError(response.status_code)
        return response

    return retry_with_backoff(
        "IndexNow",
        _post,
        retryable=_INDEXNOW_RETRYABLE_EXCEPTIONS,
        backoff_schedule=_INDEXNOW_BACKOFF_SCHEDULE,
        jitter=_INDEXNOW_BACKOFF_JITTER,
    )


__all__ = [
    "INDEXNOW_ENDPOINT",
    "MAX_URLS_PER_REQUEST",
    "IndexNowResult",
    "submit_urls",
]
