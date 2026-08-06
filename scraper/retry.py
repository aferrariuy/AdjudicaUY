"""Generic retry helper with exponential backoff and jitter.

Extracted from :mod:`scraper.bcu_client` so both the BCU SOAP client and
the XML report fetcher can share the same retry logic without duplicating
the schedule, jitter, and logging boilerplate.

The helper is transport-agnostic: it accepts any callable and any
exception tuple, making it reusable for future HTTP-based integrations.
"""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_with_backoff(
    label: str,
    operation: Callable[[], T],
    *,
    retryable: tuple[type[Exception], ...] = (httpx.HTTPError,),
    backoff_schedule: tuple[float, ...] = (1.0, 3.0, 9.0),
    jitter: float = 1.0,
) -> T:
    """Execute ``operation`` with exponential backoff on transient failures.

    Parameters
    ----------
    label:
        Human-readable label for log messages (e.g. ``"XML fetch"``).
    operation:
        A zero-argument callable to attempt.
    retryable:
        Exception types that trigger a retry. Any other exception
        propagates immediately.
    backoff_schedule:
        Delay in seconds between successive attempts. The first attempt
        is immediate (no delay before it); each subsequent attempt waits
        ``schedule[i] + uniform(0, jitter)`` seconds.
    jitter:
        Maximum random jitter in seconds added to each delay so multiple
        workers that restart simultaneously do not retry in lockstep.

    Returns
    -------
    T
        The return value of ``operation()`` on success.

    Raises
    ------
    Exception
        The last exception raised by ``operation()`` after all attempts
        are exhausted. The exception is re-raised directly (not wrapped)
        so callers can decide whether to add their own error type.
    """

    last_exc: Exception | None = None
    for attempt, delay in enumerate((0.0,) + tuple(backoff_schedule)):
        if delay:
            jittered_delay = delay + random.uniform(0, jitter)  # noqa: S311
            logger.info(
                "%s retry %d after %.2fs backoff",
                label,
                attempt,
                jittered_delay,
            )
            time.sleep(jittered_delay)
        try:
            return operation()
        except retryable as exc:
            last_exc = exc
            logger.warning("%s attempt %d failed: %s", label, attempt + 1, exc)

    # ``last_exc`` is always set here because the loop makes at least one
    # attempt and every failed attempt assigns it.
    if last_exc is None:  # pragma: no cover - unreachable in practice
        raise RuntimeError("retry loop always makes at least one attempt")
    raise last_exc


__all__ = ["retry_with_backoff"]
