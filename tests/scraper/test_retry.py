"""Unit tests for :mod:`scraper.retry`.

The retry helper is transport-agnostic; these tests exercise the generic
contract (retryable exceptions, backoff schedule, jitter, final
exception propagation) without any HTTP or BCU specifics.
"""

from __future__ import annotations

import httpx
import pytest

import scraper.retry as retry_module

# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


def test_retry_succeeds_on_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the operation succeeds immediately, no retry delay is incurred."""

    monkeypatch.setattr(retry_module.time, "sleep", lambda _s: None)

    call_count = 0

    def _op() -> str:
        nonlocal call_count
        call_count += 1
        return "ok"

    result = retry_module.retry_with_backoff("test", _op)

    assert result == "ok"
    assert call_count == 1


def test_retry_succeeds_after_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient failures are retried; the eventual result is returned."""

    monkeypatch.setattr(retry_module.time, "sleep", lambda _s: None)

    call_count = 0

    def _op() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.HTTPError("transient")
        return "ok"

    result = retry_module.retry_with_backoff("test", _op)

    assert result == "ok"
    assert call_count == 3


# ---------------------------------------------------------------------------
# Exhaustion
# ---------------------------------------------------------------------------


def test_retry_raises_last_exception_after_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all attempts fail, the last exception is re-raised directly."""

    monkeypatch.setattr(retry_module.time, "sleep", lambda _s: None)

    call_count = 0

    def _op() -> str:
        nonlocal call_count
        call_count += 1
        raise httpx.HTTPError("always fails")

    with pytest.raises(httpx.HTTPError, match="always fails"):
        retry_module.retry_with_backoff("test", _op)

    # 1 immediate attempt + 3 backoff retries = 4 total attempts.
    assert call_count == 4


# ---------------------------------------------------------------------------
# Non-retryable exceptions
# ---------------------------------------------------------------------------


def test_retry_does_not_retry_non_retryable_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceptions not in the ``retryable`` tuple propagate immediately."""

    monkeypatch.setattr(retry_module.time, "sleep", lambda _s: None)

    call_count = 0

    def _op() -> str:
        nonlocal call_count
        call_count += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError, match="not retryable"):
        retry_module.retry_with_backoff("test", _op)

    # Only one attempt — no retry for ValueError.
    assert call_count == 1


def test_retry_honors_custom_retryable_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom ``retryable`` tuple is respected."""

    monkeypatch.setattr(retry_module.time, "sleep", lambda _s: None)

    call_count = 0

    def _op() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RuntimeError("transient")
        return "ok"

    # RuntimeError is NOT in the default retryable tuple, so we pass it explicitly.
    result = retry_module.retry_with_backoff(
        "test",
        _op,
        retryable=(RuntimeError,),
    )

    assert result == "ok"
    assert call_count == 2


# ---------------------------------------------------------------------------
# Backoff schedule + jitter
# ---------------------------------------------------------------------------


def test_retry_applies_backoff_schedule_with_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each backoff delay includes the scheduled base plus random jitter."""

    monkeypatch.setattr(retry_module.random, "uniform", lambda _a, _b: 0.5)

    sleep_calls: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleep_calls.append)

    call_count = 0

    def _op() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.HTTPError("transient")
        return "ok"

    result = retry_module.retry_with_backoff(
        "test",
        _op,
        backoff_schedule=(1.0, 3.0, 9.0),
        jitter=1.0,
    )

    assert result == "ok"
    assert call_count == 3
    assert len(sleep_calls) == 2
    # Base delays (1s, 3s) plus the fixed 0.5s jitter.
    assert sleep_calls == [1.5, 3.5]


def test_retry_default_schedule_has_four_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default schedule (1, 3, 9) yields exactly 4 total attempts."""

    monkeypatch.setattr(retry_module.time, "sleep", lambda _s: None)

    call_count = 0

    def _op() -> str:
        nonlocal call_count
        call_count += 1
        raise httpx.HTTPError("fail")

    with pytest.raises(httpx.HTTPError):
        retry_module.retry_with_backoff("test", _op)

    assert call_count == 4
