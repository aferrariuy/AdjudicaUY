"""Contract test for the worker observability bits of ``docker-compose.yml``.

Repository-level Compose contract: the worker service must expose the two
marker environment variables with their pinned defaults, declare the exec-form
heartbeat healthcheck with the exact ``30s/5s/3/60s`` parameters, and keep the
hardened filesystem settings (``read_only`` + writable ``/tmp`` tmpfs). No
Docker daemon is required — this validates the deployed configuration itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yml"

EXPECTED_HEARTBEAT_ENV = "${WORKER_HEARTBEAT_FILE:-/tmp/worker.heartbeat}"  # noqa: S108 — pinned default
EXPECTED_LAST_RUN_ENV = "${WORKER_LAST_RUN_FILE:-/tmp/worker.last-run.json}"  # noqa: S108 — pinned default
EXPECTED_HEALTHCHECK_TEST = [
    "CMD",
    "python",
    "-c",
    (
        "import os; from scraper.scheduler import _heartbeat_is_fresh; "
        "raise SystemExit(0 if _heartbeat_is_fresh("
        "os.environ['WORKER_HEARTBEAT_FILE'], 300) else 1)"
    ),
]


def _worker_config() -> dict[str, Any]:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    return cast("dict[str, Any]", compose["services"]["worker"])


def test_worker_exposes_marker_env_defaults() -> None:
    """Both marker paths are exposed with the pinned defaults."""

    environment = _worker_config()["environment"]
    assert environment["WORKER_HEARTBEAT_FILE"] == EXPECTED_HEARTBEAT_ENV
    assert environment["WORKER_LAST_RUN_FILE"] == EXPECTED_LAST_RUN_ENV


def test_worker_healthcheck_parameters() -> None:
    """The healthcheck uses the exact 30s/5s/3/60s contract."""

    healthcheck = _worker_config()["healthcheck"]
    assert healthcheck["interval"] == "30s"
    assert healthcheck["timeout"] == "5s"
    assert healthcheck["retries"] == 3
    assert healthcheck["start_period"] == "60s"


def test_worker_healthcheck_is_exec_form_and_uses_env_path() -> None:
    """The check is an exec-form CMD list reading ``WORKER_HEARTBEAT_FILE``.

    The predicate runs in Python inside the container via the shipped
    scheduler code; the path must come from the environment lookup, never
    from a hard-coded default path.
    """

    test = _worker_config()["healthcheck"]["test"]
    assert test == EXPECTED_HEALTHCHECK_TEST
    assert test[0] == "CMD"
    assert "CMD-SHELL" not in test
    command = " ".join(test)
    assert "WORKER_HEARTBEAT_FILE" in command
    assert "os.environ['WORKER_HEARTBEAT_FILE']" in command
    assert "_heartbeat_is_fresh" in command
    assert "300" in command
    assert "/tmp/worker.heartbeat" not in command  # noqa: S108 — pinned default


def test_worker_keeps_hardened_filesystem_settings() -> None:
    """``read_only`` stays on and the writable ``/tmp`` tmpfs remains."""

    worker = _worker_config()
    assert worker["read_only"] is True
    assert "/tmp:rw,noexec,nosuid,nodev" in worker["tmpfs"]  # noqa: S108 — pinned mount
