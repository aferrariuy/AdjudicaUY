"""Unit tests for :mod:`scraper.scheduler`.

Checkpoint 4 of the ``scraper-crash-safety`` SDD change. Two contracts are
verified here:

1. Schedule env parsing is tolerant: a missing, non-numeric, or
   out-of-range ``SCRAPE_HOUR`` / ``SCRAPE_MINUTE`` falls back to the
   default (02:00) with ONE logged error naming the variable, the raw
   value, the accepted range, and the fallback. It never raises and never
   exits the process.
2. ``main()`` registers the daily job with an explicit ``tz="UTC"`` — the
   string, never ``datetime.timezone.utc`` — defaulting to 02:00 UTC.
"""

from __future__ import annotations

import logging
from datetime import timezone
from unittest.mock import Mock

import pytest

import scraper.scheduler as scheduler

# ---------------------------------------------------------------------------
# _parse_schedule_component — absent, non-numeric, out-of-range → fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "default", "minimum", "maximum", "fallback"),
    [
        ("SCRAPE_HOUR", 2, 0, 23, 2),
        ("SCRAPE_MINUTE", 0, 0, 59, 0),
    ],
)
def test_parse_component_absent_env_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    fallback: int,
) -> None:
    """A missing env var falls back to the default and logs ONE error."""

    monkeypatch.delenv(name, raising=False)

    with caplog.at_level(logging.ERROR):
        value = scheduler._parse_schedule_component(
            name, default=default, minimum=minimum, maximum=maximum
        )

    assert value == fallback
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].message
    assert name in message
    assert "<absent>" in message
    assert f"{minimum}-{maximum}" in message
    assert f"falling back to {fallback}" in message


@pytest.mark.parametrize(
    ("name", "default", "minimum", "maximum", "fallback", "raw"),
    [
        ("SCRAPE_HOUR", 2, 0, 23, 2, "abc"),
        ("SCRAPE_MINUTE", 0, 0, 59, 0, "12x"),
    ],
)
def test_parse_component_non_numeric_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    fallback: int,
    raw: str,
) -> None:
    """A non-numeric env var falls back and logs ONE error naming the raw value."""

    monkeypatch.setenv(name, raw)

    with caplog.at_level(logging.ERROR):
        value = scheduler._parse_schedule_component(
            name, default=default, minimum=minimum, maximum=maximum
        )

    assert value == fallback
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].message
    assert name in message
    assert raw in message
    assert f"{minimum}-{maximum}" in message
    assert f"falling back to {fallback}" in message


def test_parse_component_negative_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A negative hour falls back to the default and logs ONE error."""

    monkeypatch.setenv("SCRAPE_HOUR", "-1")

    with caplog.at_level(logging.ERROR):
        value = scheduler._parse_schedule_component(
            "SCRAPE_HOUR", default=2, minimum=0, maximum=23
        )

    assert value == 2
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].message
    assert "SCRAPE_HOUR" in message
    assert "-1" in message
    assert "0-23" in message
    assert "falling back to 2" in message


@pytest.mark.parametrize(
    ("name", "default", "minimum", "maximum", "fallback", "raw"),
    [
        ("SCRAPE_HOUR", 2, 0, 23, 2, "24"),
        ("SCRAPE_MINUTE", 0, 0, 59, 0, "60"),
    ],
)
def test_parse_component_above_maximum_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    fallback: int,
    raw: str,
) -> None:
    """Values at/above the upper bound (24 / 60) fall back and log ONE error."""

    monkeypatch.setenv(name, raw)

    with caplog.at_level(logging.ERROR):
        value = scheduler._parse_schedule_component(
            name, default=default, minimum=minimum, maximum=maximum
        )

    assert value == fallback
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].message
    assert name in message
    assert raw in message
    assert f"{minimum}-{maximum}" in message
    assert f"falling back to {fallback}" in message


@pytest.mark.parametrize(
    ("name", "default", "minimum", "maximum", "raw", "expected"),
    [
        ("SCRAPE_HOUR", 2, 0, 23, "0", 0),
        ("SCRAPE_HOUR", 2, 0, 23, "23", 23),
        ("SCRAPE_MINUTE", 0, 0, 59, "59", 59),
    ],
)
def test_parse_component_valid_value_returns_int_without_logging(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    raw: str,
    expected: int,
) -> None:
    """Valid bounds (0, 23, 59) parse to ints with NO fallback logging."""

    monkeypatch.setenv(name, raw)

    with caplog.at_level(logging.ERROR):
        value = scheduler._parse_schedule_component(
            name, default=default, minimum=minimum, maximum=maximum
        )

    assert value == expected
    assert isinstance(value, int)
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert errors == []


# ---------------------------------------------------------------------------
# main() — daily job registration (explicit UTC, default 02:00)
# ---------------------------------------------------------------------------


def test_main_registers_daily_job_at_utc(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The daily job is registered at 02:00 with the STRING ``tz="UTC"``.

    With no schedule env vars set, ``main()`` must call
    ``schedule.every().day.at("02:00", tz="UTC")`` — the timezone is the
    string ``"UTC"``, never ``datetime.timezone.utc`` — and log the
    effective time as ``02:00 UTC``.
    """

    at = Mock()
    monkeypatch.setattr(scheduler.schedule.Job, "at", at)
    # Skip the 30-second pending loop: registration happens before it.
    monkeypatch.setattr(scheduler, "_shutdown", True)

    with caplog.at_level(logging.INFO):
        scheduler.main()

    at.assert_called_once_with("02:00", tz="UTC")
    called_tz = at.call_args.kwargs["tz"]
    assert called_tz == "UTC"
    assert not isinstance(called_tz, timezone)

    messages = [record.message for record in caplog.records]
    assert any("02:00 UTC" in message for message in messages)


def test_main_registers_daily_job_with_configured_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured env values flow into the daily job registration."""

    monkeypatch.setenv("SCRAPE_HOUR", "7")
    monkeypatch.setenv("SCRAPE_MINUTE", "30")

    at = Mock()
    monkeypatch.setattr(scheduler.schedule.Job, "at", at)
    monkeypatch.setattr(scheduler, "_shutdown", True)

    scheduler.main()

    at.assert_called_once_with("07:30", tz="UTC")
