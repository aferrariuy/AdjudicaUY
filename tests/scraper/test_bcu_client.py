"""Unit tests for :mod:`scraper.bcu_client`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from scraper.bcu_client import BcuClient, BcuError


# ---------------------------------------------------------------------------
# Test doubles — in-memory httpx transports
# ---------------------------------------------------------------------------


def _envelope_response(tcc: str | None) -> bytes:
    """Build a fake BCU cotizaciones response with the given TCC value.

    An empty ``<datos>`` block is the BCU's signal for "no publication
    for that day"; the parser must return ``None`` in that case.
    """

    if tcc is None:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<root>"
            "<datos></datos>"
            "</root>"
        ).encode("utf-8")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<root>"
        "<datos>"
        f"<TCC>{tcc}</TCC>"
        "<TCV>0</TCV>"
        "</datos>"
        "</root>"
    ).encode("utf-8")


def _make_transport(handler):
    """Return a callable that builds an ``httpx.MockTransport`` for the given handler."""

    def _factory():
        return httpx.MockTransport(handler)

    return _factory


def _ok_handler(tcc: str = "38.50", *, call_log: list[str] | None = None):
    """Return a request handler that responds 200 with ``tcc``.

    If ``call_log`` is provided, every requested URL is appended to it so
    tests can assert how many times the BCU was called.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if call_log is not None:
            call_log.append(str(request.url))
        return httpx.Response(200, content=_envelope_response(tcc))

    return _handler


def _503_then_ok_handler(tcc: str, *, fail_count: int = 1, call_log: list[int] | None = None):
    """Return a handler that fails the first ``fail_count`` calls with 503, then succeeds."""

    state = {"calls": 0}

    def _handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        state["calls"] += 1
        if call_log is not None:
            call_log.append(state["calls"])
        if state["calls"] <= fail_count:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(200, content=_envelope_response(tcc))

    return _handler


def _always_503_handler(*, call_log: list[int] | None = None):
    state = {"calls": 0}

    def _handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        state["calls"] += 1
        if call_log is not None:
            call_log.append(state["calls"])
        return httpx.Response(503, text="service unavailable")

    return _handler


# ---------------------------------------------------------------------------
# Successful fetch
# ---------------------------------------------------------------------------


def test_get_tcc_returns_decimal_on_success() -> None:
    transport = httpx.MockTransport(_ok_handler(tcc="38.50"))
    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)
        rate = bcu.get_tcc(2224, date(2024, 1, 15))

    assert rate == Decimal("38.50")


def test_get_tcc_posts_soap_envelope_with_currency_and_date() -> None:
    """The SOAP envelope must include the BCU code and the requested date."""

    captured: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        captured["ct"] = request.headers.get("content-type", "")
        return httpx.Response(200, content=_envelope_response("40.00"))

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)
        bcu.get_tcc(2224, date(2024, 3, 10))

    assert captured["url"] == "https://example.test/wsbcucotizaciones"
    assert "text/xml" in captured["ct"]
    assert "<item>2224</item>" in captured["body"]
    assert "2024-03-10" in captured["body"]
    assert "<Grupo>0</Grupo>" in captured["body"]


# ---------------------------------------------------------------------------
# Empty response — no publication for the requested date
# ---------------------------------------------------------------------------


def test_get_tcc_returns_none_when_response_has_no_datos() -> None:
    transport = httpx.MockTransport(_ok_handler(tcc=None))
    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)
        rate = bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=0)

    assert rate is None


def test_get_tcc_falls_back_to_previous_day() -> None:
    """When the requested date has no rate, the client steps back 1 day (max 7)."""

    call_log: list[str] = []
    # First call (the requested date) returns empty; second call (date-1) returns 39.10.
    state = {"calls": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        call_log.append(request.content.decode("utf-8"))
        if state["calls"] == 1:
            return httpx.Response(200, content=_envelope_response(None))
        return httpx.Response(200, content=_envelope_response("39.10"))

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)
        rate = bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=3)

    assert rate == Decimal("39.10")
    # Two SOAP envelopes were POSTed: the original date and date-1.
    assert state["calls"] == 2
    assert "2024-01-15" in call_log[0]
    assert "2024-01-14" in call_log[1]


def test_get_tcc_returns_none_after_exhausting_lookback() -> None:
    """If no rate is found within ``max_lookback_days + 1`` calls, return ``None``."""

    transport = httpx.MockTransport(_ok_handler(tcc=None))
    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)
        rate = bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=3)

    assert rate is None


# ---------------------------------------------------------------------------
# Retry on transient failures
# ---------------------------------------------------------------------------


def test_get_tcc_retries_on_503_then_succeeds(monkeypatch) -> None:
    """A 503 on the first attempt is retried with backoff and then succeeds."""

    call_log: list[int] = []
    transport = httpx.MockTransport(_503_then_ok_handler("38.00", fail_count=1, call_log=call_log))

    # Patch ``time.sleep`` so the test runs instantly — the production
    # backoff is 1s → 3s → 9s and we do not want to wait that long.
    import scraper.bcu_client as bcu_module

    monkeypatch.setattr(bcu_module.time, "sleep", lambda _seconds: None)

    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)
        rate = bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=0)

    assert rate == Decimal("38.00")
    # Two calls: the failing 503 + the successful retry.
    assert call_log == [1, 2]


def test_get_tcc_raises_bcu_error_when_all_retries_exhausted(monkeypatch) -> None:
    import scraper.bcu_client as bcu_module

    monkeypatch.setattr(bcu_module.time, "sleep", lambda _seconds: None)

    transport = httpx.MockTransport(_always_503_handler())
    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)
        with pytest.raises(BcuError):
            bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=0)


def test_get_tcc_retries_3_times_before_giving_up(monkeypatch) -> None:
    """The retry schedule is (1s, 3s, 9s) — exactly 4 attempts in total."""

    import scraper.bcu_client as bcu_module

    monkeypatch.setattr(bcu_module.time, "sleep", lambda _seconds: None)

    call_log: list[int] = []
    transport = httpx.MockTransport(_always_503_handler(call_log=call_log))

    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)
        with pytest.raises(BcuError):
            bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=0)

    # Initial attempt + 3 retries = 4 calls.
    assert len(call_log) == 4


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_get_tcc_caches_successful_results() -> None:
    """A second call for the same (code, date) MUST NOT hit the network."""

    call_log: list[str] = []
    transport = httpx.MockTransport(_ok_handler(tcc="38.50", call_log=call_log))

    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)

        first = bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=0)
        second = bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=0)

    assert first == second == Decimal("38.50")
    # Cache hit on the second call — exactly one network request.
    assert len(call_log) == 1


def test_get_tcc_caches_empty_responses() -> None:
    """A confirmed-empty response is cached to avoid hammering the BCU on holidays."""

    call_log: list[str] = []
    transport = httpx.MockTransport(_ok_handler(tcc=None, call_log=call_log))

    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)

        first = bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=0)
        second = bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=0)

    assert first is None
    assert second is None
    # Both calls share the same (code, date) cell; only the first hits the network.
    assert len(call_log) == 1


def test_cache_is_keyed_on_both_code_and_date() -> None:
    """Different (code, date) pairs hit the network independently."""

    call_log: list[str] = []
    transport = httpx.MockTransport(_ok_handler(tcc="38.50", call_log=call_log))

    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)

        bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=0)
        bcu.get_tcc(1111, date(2024, 1, 15), max_lookback_days=0)  # different code
        bcu.get_tcc(2224, date(2024, 1, 16), max_lookback_days=0)  # different date

    assert len(call_log) == 3


# ---------------------------------------------------------------------------
# Context manager / lifecycle
# ---------------------------------------------------------------------------


def test_client_works_as_context_manager() -> None:
    transport = httpx.MockTransport(_ok_handler(tcc="40.00"))
    # The BCUClient accepts an externally-owned client, so the test
    # can also drive the context manager.
    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)
        rate = bcu.get_tcc(2224, date(2024, 1, 15), max_lookback_days=0)

    assert rate == Decimal("40.00")


# ---------------------------------------------------------------------------
# Monedas (currency catalogue) endpoint
# ---------------------------------------------------------------------------


MONEDAS_PAYLOAD = """<?xml version="1.0" encoding="UTF-8"?>
<root>
  <moneda>
    <Codigo>2224</Codigo>
    <Nombre>DOLAR USA</Nombre>
    <CodigoISO>USD</CodigoISO>
  </moneda>
  <moneda>
    <Codigo>1111</Codigo>
    <Nombre>EURO</Nombre>
    <CodigoISO>EUR</CodigoISO>
  </moneda>
  <moneda>
    <Codigo>5050</Codigo>
    <Nombre>BITCOIN</Nombre>
  </moneda>
</root>
"""


def test_list_monedas_parses_currency_catalogue() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        # The client must point at the ``awsbcumonedas`` sibling servlet.
        assert "awsbcumonedas" in str(request.url)
        return httpx.Response(200, content=MONEDAS_PAYLOAD.encode("utf-8"))

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient(
            "https://example.test/wscotizaciones/servlet/wsbcucotizaciones",
            client=http_client,
        )
        currencies = bcu.list_monedas()

    assert len(currencies) == 3
    assert (currencies[0].codigo, currencies[0].nombre, currencies[0].codigo_iso) == (2224, "DOLAR USA", "USD")
    assert currencies[2].codigo_iso is None  # Bitcoin entry has no ISO code


def test_list_monedas_retries_on_503(monkeypatch) -> None:
    """The monedas endpoint also uses the standard retry schedule."""

    import scraper.bcu_client as bcu_module

    monkeypatch.setattr(bcu_module.time, "sleep", lambda _seconds: None)

    call_log: list[int] = []
    transport = httpx.MockTransport(_always_503_handler(call_log=call_log))

    with httpx.Client(transport=transport) as http_client:
        bcu = BcuClient("https://example.test/wsbcucotizaciones", client=http_client)
        # The public contract is that the client raises ``BcuError``
        # when the monedas endpoint is unreachable after retries.
        with pytest.raises(BcuError):
            bcu.list_monedas()

    # 1 initial attempt + 3 retries = 4 calls before giving up.
    assert len(call_log) == 4
