"""Contract test for the worker observability bits of ``docker-compose.yml``.

Repository-level Compose contract: the worker service must expose the two
marker environment variables with their pinned defaults, declare the exec-form
heartbeat healthcheck with the exact ``30s/5s/3/60s`` parameters, and keep the
hardened filesystem settings (``read_only`` + writable ``/tmp`` tmpfs). No
Docker daemon is required — this validates the deployed configuration itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yml"
ENV_EXAMPLE_PATH = Path(__file__).resolve().parents[1] / ".env.example"

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


def _service_config(service: str) -> dict[str, Any]:
    """Return one service's Compose definition."""

    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    return cast("dict[str, Any]", compose["services"][service])


def _worker_config() -> dict[str, Any]:
    return _service_config("worker")


def _documented_settings() -> set[str]:
    """Return the ``Settings`` fields ``.env.example`` tells an operator to set.

    ``.env.example`` is the operator-facing contract: it is where someone looks to
    learn which knobs the deployment exposes. Commented-out lines are excluded,
    since they document intent rather than an active invitation to set the value.
    """

    from app.config import Settings

    documented = set(
        re.findall(r"^([A-Z0-9_]+)=", ENV_EXAMPLE_PATH.read_text(), re.MULTILINE)
    )
    return documented & {name.upper() for name in Settings.model_fields}


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


# ---------------------------------------------------------------------------
# Settings reachability
# ---------------------------------------------------------------------------


def test_every_documented_setting_is_wired_into_both_containers() -> None:
    """A setting documented in ``.env.example`` must reach both containers.

    Compose passes the environment by explicit allowlist rather than
    ``env_file``, so a variable present in ``.env`` never enters a container
    unless that service names it. That is a silent failure mode: the operator sets
    the variable, the deployment looks configured, and the process quietly reads
    its default instead. Tying the assertion to ``.env.example`` makes the
    operator-facing contract and the container wiring impossible to drift apart.
    """

    documented = _documented_settings()
    assert documented, "nothing parsed: .env.example or Settings changed shape"

    for service in ("app", "worker"):
        environment = _service_config(service)["environment"]
        missing = sorted(name for name in documented if name not in environment)
        assert missing == [], f"the {service} container cannot receive {missing}"


def test_indexnow_key_is_wired_with_a_blank_safe_default() -> None:
    """Both containers receive the key, defaulting to empty when unset.

    The explicit empty default matters: without it Compose warns about an unset
    variable, and with it an unconfigured deployment passes ``""``, which the
    settings validator treats as "off".
    """

    for service in ("app", "worker"):
        environment = _service_config(service)["environment"]
        assert environment["INDEXNOW_KEY"] == "${INDEXNOW_KEY:-}"
