"""Contract test for the app container command in the ``Dockerfile``.

Repository-level Dockerfile contract: the runtime image must launch uvicorn
with proxy headers enabled and the reverse-proxy peer trusted. TLS terminates
at Traefik, so without these flags uvicorn only sees the plaintext internal
hop: ``request.url.scheme`` stays ``http`` and every absolute URL the app
builds from the request — starting with Starlette's trailing-slash redirect —
is published with the wrong scheme.

No Docker daemon is required; this validates the deployed configuration
itself, in the same spirit as :mod:`tests.test_compose_contract`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

DOCKERFILE_PATH = Path(__file__).resolve().parents[1] / "Dockerfile"

_CMD_PATTERN = re.compile(r"^CMD\s+(\[.*\])\s*$", flags=re.MULTILINE)


def _uvicorn_argv() -> list[str]:
    """Return the exec-form uvicorn CMD declared in the Dockerfile.

    Raises:
        AssertionError: if the Dockerfile declares no exec-form CMD, or none
            of them launches uvicorn.
    """

    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    declared = _CMD_PATTERN.findall(text)
    assert declared, "Dockerfile declares no exec-form CMD"

    for raw in declared:
        argv = cast("list[str]", json.loads(raw))
        if argv and argv[0] == "uvicorn":
            return argv

    raise AssertionError("no uvicorn CMD found in the Dockerfile")


def test_app_cmd_enables_proxy_headers() -> None:
    """``--proxy-headers`` makes uvicorn honour ``X-Forwarded-Proto``.

    Without it the trailing-slash redirect points at ``http://``, forcing a
    second, insecure hop before the client reaches the canonical ``https``.
    """

    assert "--proxy-headers" in _uvicorn_argv()


def test_app_cmd_trusts_the_reverse_proxy_peer() -> None:
    """``--forwarded-allow-ips`` must cover the proxy, which is a container.

    uvicorn trusts only ``127.0.0.1`` by default, so the flag is required for
    the forwarding headers sent by Traefik to be accepted at all.
    """

    argv = _uvicorn_argv()
    assert "--forwarded-allow-ips" in argv
    flag_index = argv.index("--forwarded-allow-ips")
    assert argv[flag_index + 1] == "*"


def test_app_cmd_still_binds_the_container_internal_port() -> None:
    """Guard the contract we already relied on: bind 0.0.0.0:8000."""

    argv = _uvicorn_argv()
    assert argv[1] == "app.main:app"
    assert argv[argv.index("--host") + 1] == "0.0.0.0"  # noqa: S104 — in-container bind
    assert argv[argv.index("--port") + 1] == "8000"
