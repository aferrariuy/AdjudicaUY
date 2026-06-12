"""SOAP client for the BCU (Banco Central del Uruguay) exchange rate API.

The BCU publishes historical buy (``TCC``) and sell (``TCV``) rates for every
currency it tracks. The relevant endpoint is the SOAP ``wsbcucotizaciones``basirey
servlet; the sibling ``awsbcumonedas`` servlet returns the currency catalogue.

This client wraps both endpoints with:

* a **per-(bcu_code, date) in-memory cache** so a scraping run hitting
  thousands of adjudications for the same currency on the same date only
  reaches the network once per cell,
* **exponential backoff retry** (1s → 3s → 9s) for transient transport or
  HTTP-level failures, and
* a **lookback window** so an adjudication date with no published rate
  (weekends, holidays) falls back to the previous business day, up to
  ``max_lookback_days``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from lxml import etree

logger = logging.getLogger(__name__)


class BcuError(Exception):
    """Raised when the BCU endpoint returns a non-recoverable failure."""


@dataclass(frozen=True)
class BcuCurrency:
    """One currency entry as returned by the ``awsbcumonedas`` endpoint."""

    codigo: int
    nombre: str
    codigo_iso: str | None


# Backoff delays in seconds, applied between successive attempts.
# The first attempt is immediate; the subsequent attempts wait
# ``1s``, then ``3s``, then ``9s`` before retrying.
_BACKOFF_SCHEDULE: tuple[int, ...] = (1, 3, 9)

_SOAP_NAMESPACE = "Cotiza"
_SOAP_ACTION = "Cotizaaction/AWSBCUCOTIZACIONES.Execute"
_DEFAULT_TIMEOUT_SECONDS = 30.0


def _build_cotizaciones_envelope(bcu_code: int, on_date: date) -> bytes:
    """Build the SOAP request body for a cotizaciones lookup.

    The BCU servlet accepts a single ``<tns:item>{code}</tns:item>`` entry
    inside a ``<tns:Moneda>`` array, a date range, and a ``<tns:Grupo>0</tns:Grupo>``
    (all groups).  The WSDL (targetNamespace ``Cotiza``) expects the operation
    element ``tns:wsbcucotizaciones.Execute`` wrapping the input parameters.
    """

    date_str = on_date.isoformat()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope '
        'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:tns="{_SOAP_NAMESPACE}">'
        "<soapenv:Header/>"
        "<soapenv:Body>"
        "<tns:wsbcucotizaciones.Execute>"
        "<tns:Entrada>"
        "<tns:Moneda>"
        f"<tns:item>{int(bcu_code)}</tns:item>"
        "</tns:Moneda>"
        f"<tns:FechaDesde>{date_str}</tns:FechaDesde>"
        f"<tns:FechaHasta>{date_str}</tns:FechaHasta>"
        "<tns:Grupo>0</tns:Grupo>"
        "</tns:Entrada>"
        "</tns:wsbcucotizaciones.Execute>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    ).encode("utf-8")


def _first_text(root: etree._Element, suffix: str) -> str | None:
    """Return the text of the first descendant element whose tag ends in ``suffix``.

    Namespaces vary across BCU response versions; matching the local tag
    name is more robust than a hard-coded XPath.
    """

    for element in root.iter():
        if element.tag.endswith(suffix) and element.text is not None:
            return element.text.strip()
    return None


def _parse_tcc(response_xml: bytes) -> Decimal | None:
    """Extract the first ``TCC`` value from a BCU response.

    Returns ``None`` when the response has no data series for the requested
    date (an empty ``<datos>`` block, which is the BCU's signal for "no
    publication for that day").
    """

    try:
        root = etree.fromstring(response_xml)
    except etree.XMLSyntaxError as exc:
        raise BcuError(f"Malformed BCU response: {exc}") from exc

    raw = _first_text(root, "TCC")
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise BcuError(f"BCU returned non-numeric TCC={raw!r}") from exc


def _parse_monedas_response(response_xml: bytes) -> list[BcuCurrency]:
    """Extract the currency catalogue from a ``awsbcumonedas`` response."""

    try:
        root = etree.fromstring(response_xml)
    except etree.XMLSyntaxError as exc:
        raise BcuError(f"Malformed BCU monedas response: {exc}") from exc

    currencies: list[BcuCurrency] = []
    for item in root.iter():
        if not item.tag.endswith("item") and not item.tag.endswith("moneda"):
            # The ``monedas`` servlet emits ``<moneda>`` elements; older
            # versions used ``<item>``. Match both.
            if not item.tag.endswith("moneda"):
                continue
        codigo_raw = None
        nombre = None
        codigo_iso = None
        for child in item:
            if child.text is None:
                continue
            text = child.text.strip()
            if not text:
                continue
            tag = child.tag
            if tag.endswith("Codigo") or tag.endswith("Moneda") or tag.endswith("CodigoBCU"):
                codigo_raw = text
            elif tag.endswith("Nombre") or tag.endswith("Descripcion"):
                nombre = text
            elif tag.endswith("CodigoISO") or tag.endswith("ISO"):
                codigo_iso = text
        if codigo_raw is None or nombre is None:
            continue
        try:
            codigo = int(codigo_raw)
        except ValueError:
            continue
        currencies.append(BcuCurrency(codigo=codigo, nombre=nombre, codigo_iso=codigo_iso))

    return currencies


class BcuClient:
    """High-level BCU SOAP client with caching and retry.

    A single :class:`BcuClient` is safe to share across the scraper
    pipeline — the cache is keyed by ``(bcu_code, date)`` and accumulates
    per call. Constructing a new client for each call would defeat the
    cache and the underlying ``httpx`` connection pool.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client
        # ``None`` value means "the BCU confirmed there is no data for this
        # (bcu_code, date) cell" — distinct from "we have not asked yet".
        self._cache: dict[tuple[int, date], Decimal | None] = {}

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> BcuClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_tcc(
        self,
        bcu_code: int,
        on_date: date,
        *,
        max_lookback_days: int = 7,
    ) -> Decimal | None:
        """Return the ``TCC`` (buy rate) for ``bcu_code`` on or before ``on_date``.

        The lookup walks backward up to ``max_lookback_days`` days, returning
        the first non-null rate. Returns ``None`` if no rate is found within
        the window. Results are cached per ``(bcu_code, date)`` — both
        successful and confirmed-empty results are cached to avoid hammering
        the BCU on weekends and holidays.
        """

        for days_back in range(max_lookback_days + 1):
            target_date = on_date - timedelta(days=days_back)
            rate = self._rate_for_date(bcu_code, target_date)
            if rate is not None:
                if days_back > 0:
                    logger.info(
                        "BCU fallback: using %s (TCC=%s) %d day(s) before %s",
                        bcu_code, rate, days_back, on_date,
                    )
                return rate
        logger.warning(
            "BCU rate unavailable: bcu_code=%s on_date=%s within %d-day lookback",
            bcu_code, on_date, max_lookback_days,
        )
        return None

    def list_monedas(self) -> list[BcuCurrency]:
        """Fetch the full BCU currency catalogue.

        Results are not cached because the catalogue is small and changes
        rarely; the BCU's own caching upstream is what matters here.
        """

        # Replace the cotizaciones servlet name with the monedas servlet.
        # The base URL always ends in ``.../servlet/awsbcucotizaciones``.
        monedas_url = self._base_url.replace("awsbcucotizaciones", "awsbcumonedas")
        if monedas_url == self._base_url:
            # Fallback: bare host or unexpected path — use sibling path.
            monedas_url = f"{self._base_url}/awsbcumonedas"

        for attempt, delay in enumerate((0,) + _BACKOFF_SCHEDULE):
            if delay:
                logger.info("BCU monedas retry %d after %ds backoff", attempt, delay)
                time.sleep(delay)
            try:
                response = self.client.get(monedas_url, timeout=self._timeout)
                response.raise_for_status()
                return _parse_monedas_response(response.content)
            except (httpx.HTTPError, BcuError) as exc:
                logger.warning("BCU monedas attempt %d failed: %s", attempt + 1, exc)
                if attempt == len(_BACKOFF_SCHEDULE):
                    raise BcuError(f"BCU monedas endpoint failed after retries: {exc}") from exc

        return []  # unreachable; loop always returns or raises

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rate_for_date(self, bcu_code: int, target_date: date) -> Decimal | None:
        """Return the cached or freshly fetched rate for an exact date."""

        cache_key = (bcu_code, target_date)
        if cache_key in self._cache:
            return self._cache[cache_key]

        rate = self._fetch_tcc_with_retry(bcu_code, target_date)
        # Cache both successful and confirmed-empty results. ``None`` here
        # means "BCU returned an empty <datos> block" — i.e. the date has
        # no rate published.
        self._cache[cache_key] = rate
        return rate

    def _fetch_tcc_with_retry(self, bcu_code: int, target_date: date) -> Decimal | None:
        """POST the SOAP envelope, retrying on transient failures."""

        envelope = _build_cotizaciones_envelope(bcu_code, target_date)
        last_exc: Exception | None = None

        for attempt, delay in enumerate((0,) + _BACKOFF_SCHEDULE):
            if delay:
                logger.info(
                    "BCU cotizaciones retry %d for code=%s date=%s after %ds backoff",
                    attempt, bcu_code, target_date, delay,
                )
                time.sleep(delay)
            try:
                response = self.client.post(
                    self._base_url,
                    content=envelope,
                    headers={
                        "Content-Type": "text/xml; charset=utf-8",
                        "SOAPAction": _SOAP_ACTION,
                    },
                )
                response.raise_for_status()
                return _parse_tcc(response.content)
            except (httpx.HTTPError, BcuError) as exc:
                last_exc = exc
                logger.warning(
                    "BCU cotizaciones attempt %d failed for code=%s date=%s: %s",
                    attempt + 1, bcu_code, target_date, exc,
                )

        raise BcuError(
            f"BCU cotizaciones endpoint failed for code={bcu_code} date={target_date} "
            f"after retries: {last_exc}"
        )


__all__ = ["BcuClient", "BcuCurrency", "BcuError"]
