"""Unit tests for :mod:`scraper.scheduler`.

Covers the ``scraper-crash-safety`` schedule contracts and the
``worker-observability`` marker contracts:

1. Schedule env parsing is tolerant: a missing, non-numeric, or
   out-of-range ``SCRAPE_HOUR`` / ``SCRAPE_MINUTE`` falls back to the
   default (02:00) with ONE logged error naming the variable, the raw
   value, the accepted range, and the fallback. It never raises and never
   exits the process.
2. ``main()`` registers the daily job with an explicit ``tz="UTC"`` — the
   string, never ``datetime.timezone.utc`` — defaulting to 02:00 UTC.
3. Worker observability: the loop touches ``WORKER_HEARTBEAT_FILE`` before
   pending jobs run, ``_heartbeat_is_fresh`` enforces the strict 300-second
   boundary, and ``WORKER_LAST_RUN_FILE`` is atomically replaced only after
   a successful scrape.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Worker observability — heartbeat and last-run markers
# ---------------------------------------------------------------------------


def test_main_loop_touches_heartbeat_before_run_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The heartbeat file exists BEFORE ``schedule.run_pending`` runs.

    The assertion must live inside the mocked ``run_pending``: asserting after
    ``main()`` returns would not prove the touch precedes pending processing.
    Only the override path is touched — the default path is never written.
    """

    heartbeat = tmp_path / "heartbeat"
    default_heartbeat = tmp_path / "default.heartbeat"
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(heartbeat))
    monkeypatch.setattr(scheduler, "_HEARTBEAT_FILE_DEFAULT", str(default_heartbeat))
    monkeypatch.setattr(scheduler.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(scheduler, "_shutdown", False)

    def run_pending_asserts_heartbeat_exists() -> None:
        assert heartbeat.exists()
        assert not default_heartbeat.exists()
        scheduler._shutdown = True

    monkeypatch.setattr(
        scheduler.schedule, "run_pending", run_pending_asserts_heartbeat_exists
    )

    scheduler.main()

    # A later touch after a stale mtime must advance the file's mtime.
    old = heartbeat.stat().st_mtime_ns - 1_000_000_000
    os.utime(heartbeat, ns=(old, old))
    scheduler._touch_heartbeat(str(heartbeat))
    assert heartbeat.stat().st_mtime_ns > old


def test_main_loop_continues_when_heartbeat_touch_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A heartbeat I/O error is logged and does not skip ``run_pending``."""

    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(heartbeat))
    monkeypatch.setattr(scheduler.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(scheduler, "_shutdown", False)

    def failing_touch(_path: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(scheduler, "_touch_heartbeat", failing_touch)

    pending_calls: list[str] = []

    def run_pending_records_call() -> None:
        pending_calls.append("called")
        scheduler._shutdown = True

    monkeypatch.setattr(scheduler.schedule, "run_pending", run_pending_records_call)

    with caplog.at_level(logging.ERROR):
        scheduler.main()

    assert pending_calls == ["called"]
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert any(
        "Heartbeat touch failed at" in record.message
        and str(heartbeat) in record.message
        for record in errors
    )


def test_marker_path_defaults_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both marker paths fall back to the pinned ``/tmp`` defaults."""

    monkeypatch.delenv("WORKER_HEARTBEAT_FILE", raising=False)
    monkeypatch.delenv("WORKER_LAST_RUN_FILE", raising=False)
    assert scheduler._heartbeat_path() == "/tmp/worker.heartbeat"  # noqa: S108 — pinned default
    assert scheduler._last_run_path() == "/tmp/worker.last-run.json"  # noqa: S108 — pinned default


def test_marker_path_overrides_used_and_defaults_never_written(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured overrides are honored and no hard-coded default is written."""

    heartbeat = tmp_path / "heartbeat"
    last_run = tmp_path / "last-run.json"
    default_heartbeat = tmp_path / "default.heartbeat"
    default_last_run = tmp_path / "default.last-run.json"
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(heartbeat))
    monkeypatch.setenv("WORKER_LAST_RUN_FILE", str(last_run))
    monkeypatch.setattr(scheduler, "_HEARTBEAT_FILE_DEFAULT", str(default_heartbeat))
    monkeypatch.setattr(scheduler, "_LAST_RUN_FILE_DEFAULT", str(default_last_run))

    assert scheduler._heartbeat_path() == str(heartbeat)
    assert scheduler._last_run_path() == str(last_run)

    monkeypatch.setattr(scheduler, "run_scrape", lambda: 1234)
    scheduler._run()

    assert last_run.exists()
    assert not default_last_run.exists()
    assert not heartbeat.exists()
    assert not default_heartbeat.exists()


def test_run_success_writes_utc_marker_with_exact_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A successful scrape writes exactly the two-field UTC marker."""

    last_run = tmp_path / "last-run.json"
    monkeypatch.setenv("WORKER_LAST_RUN_FILE", str(last_run))
    monkeypatch.setattr(scheduler, "run_scrape", lambda: 1234)

    scheduler._run()

    payload = json.loads(last_run.read_text())
    assert set(payload) == {"completed_at", "record_count"}
    assert payload["record_count"] == 1234
    completed_at = payload["completed_at"]
    assert completed_at.endswith("Z")
    parsed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)


def test_last_run_marker_replacement_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The marker is replaced via a same-directory temp + atomic rename.

    The spy runs at the ENTRY of ``os.replace``: the destination must still
    hold the prior complete document and the source must be a different
    same-parent path holding a complete two-field JSON document.
    """

    last_run = tmp_path / "last-run.json"
    prior = {"completed_at": "2020-01-01T00:00:00Z", "record_count": 7}
    last_run.write_text(json.dumps(prior, separators=(",", ":")))
    prior_bytes = last_run.read_bytes()

    real_replace = os.replace
    captured_source = ""
    captured_destination_bytes = b""
    captured_source_bytes = b""

    def replace_spy(source: str, destination: str) -> None:
        nonlocal captured_source, captured_destination_bytes, captured_source_bytes
        captured_destination_bytes = Path(destination).read_bytes()
        captured_source = source
        captured_source_bytes = Path(source).read_bytes()
        real_replace(source, destination)

    monkeypatch.setattr(scheduler.os, "replace", replace_spy)

    scheduler._write_last_run(str(last_run), 1234)

    assert captured_destination_bytes == prior_bytes
    assert captured_source != str(last_run)
    assert os.path.dirname(captured_source) == os.path.dirname(str(last_run))
    assert os.path.basename(captured_source).startswith(f".{last_run.name}.")
    source_payload = json.loads(captured_source_bytes)
    assert set(source_payload) == {"completed_at", "record_count"}
    assert source_payload["record_count"] == 1234

    final_payload = json.loads(last_run.read_text())
    assert set(final_payload) == {"completed_at", "record_count"}
    assert final_payload["record_count"] == 1234
    assert [path.name for path in tmp_path.iterdir()] == [last_run.name]


def test_run_failure_leaves_no_marker(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A first failed run logs the failure and creates no marker."""

    last_run = tmp_path / "last-run.json"
    monkeypatch.setenv("WORKER_LAST_RUN_FILE", str(last_run))

    def failing_scrape() -> int:
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(scheduler, "run_scrape", failing_scrape)
    write_last_run = Mock()
    monkeypatch.setattr(scheduler, "_write_last_run", write_last_run)

    with caplog.at_level(logging.ERROR):
        scheduler._run()

    assert not last_run.exists()
    write_last_run.assert_not_called()
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert any("Scheduled scrape failed" in record.message for record in errors)


def test_run_failure_preserves_prior_marker(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A later failed run leaves the prior marker bytes untouched."""

    last_run = tmp_path / "last-run.json"
    prior = {"completed_at": "2020-01-01T00:00:00Z", "record_count": 7}
    last_run.write_text(json.dumps(prior, separators=(",", ":")))
    prior_bytes = last_run.read_bytes()
    monkeypatch.setenv("WORKER_LAST_RUN_FILE", str(last_run))

    def failing_scrape() -> int:
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(scheduler, "run_scrape", failing_scrape)
    write_last_run = Mock()
    monkeypatch.setattr(scheduler, "_write_last_run", write_last_run)

    with caplog.at_level(logging.ERROR):
        scheduler._run()

    assert last_run.read_bytes() == prior_bytes
    write_last_run.assert_not_called()


def test_marker_write_failure_does_not_fail_scrape(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A marker-write error is logged distinctly and never fails the scrape."""

    last_run = tmp_path / "last-run.json"
    monkeypatch.setenv("WORKER_LAST_RUN_FILE", str(last_run))
    monkeypatch.setattr(scheduler, "run_scrape", lambda: 1234)

    def failing_marker_write(_path: str, _count: int) -> None:
        raise OSError("no space left")

    monkeypatch.setattr(scheduler, "_write_last_run", failing_marker_write)

    with caplog.at_level(logging.ERROR):
        scheduler._run()

    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert any(
        "Last-run marker write failed at" in record.message
        and str(last_run) in record.message
        for record in errors
    )
    assert not any("Scheduled scrape failed" in record.message for record in errors)


def test_heartbeat_is_fresh_strict_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Freshness is strict ``<``: age exactly 300 seconds is unhealthy.

    A fixed ``time.time`` and explicit mtimes keep the boundary deterministic
    — no sleeps, no wall-clock races.
    """

    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text("")
    now = 1_000_000.0
    monkeypatch.setattr(scheduler.time, "time", lambda: now)

    os.utime(heartbeat, (now - 100.0, now - 100.0))
    assert scheduler._heartbeat_is_fresh(str(heartbeat)) is True

    os.utime(heartbeat, (now - 300.0, now - 300.0))
    assert scheduler._heartbeat_is_fresh(str(heartbeat)) is False

    os.utime(heartbeat, (now - 301.0, now - 301.0))
    assert scheduler._heartbeat_is_fresh(str(heartbeat)) is False

    assert scheduler._heartbeat_is_fresh(str(tmp_path / "missing")) is False
    assert scheduler._heartbeat_is_fresh(str(tmp_path)) is False

    # A custom threshold keeps the same strict boundary.
    os.utime(heartbeat, (now - 100.0, now - 100.0))
    assert scheduler._heartbeat_is_fresh(str(heartbeat), threshold=100.0) is False
