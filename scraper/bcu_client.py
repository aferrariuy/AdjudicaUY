"""SOAP client for the BCU (Banco Central del Uruguay) exchange rate API.

The BCU publishes historical buy (``TCC``) and sell (``TCV``) rates for every
currency it tracks. The relevant endpoint is the SOAP ``wsbcucotizaciones``basirey
servlet; the sibling ``awsbcumonedas`` servlet returns the currency catalogue.

This client wraps both endpoints with:

* a **per-(bcu_code, date) in-memory cache** so a scraping run hitting
  thousands of adjudications for the same currency on the same date only
  reaches the network once per cell (confirmed-empty results cached as
  ``None``; permanent failures never cached),
* **exponential backoff retry** (1s → 3s → 9s) for transient transport or
  HTTP-level failures only — SOAP faults, malformed XML, and non-numeric
  rates are permanent (:class:`BcuPermanentError`) and never retried,
* a **per-client currency-catalogue cache** so repeated ``list_monedas()``
  calls reuse one parsed catalogue, and
* a **lookback window** so an adjudication date with no published rate
  (weekends, holidays) falls back to the previous business day, up to
  ``max_lookback_days``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, TypeVar

import httpx
from lxml import etree

from scraper.retry import retry_with_backoff
from scraper.xml_report import _SAFE_PARSER, _read_with_limit

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# BCU SOAP responses are tiny (a few KB). Anything dramatically larger is
# suspicious and is rejected to protect the worker from memory exhaustion.
_MAX_SOAP_SIZE_BYTES = 16 * 1024 * 1024  # 16 MiB


class BcuError(Exception):
    """Raised when the BCU endpoint returns a non-recoverable failure."""


class BcuPermanentError(BcuError):
    """A BCU response is permanently invalid or reports a SOAP fault.

    Raised for malformed XML, non-numeric/non-finite rates, SOAP Fault
    envelopes, and malformed catalogue entries. These failures are never
    retried and never cached; callers catching :class:`BcuError` remain
    compatible.
    """


@dataclass(frozen=True)
class BcuCurrency:
    """One currency entry as returned by the ``awsbcumonedas`` endpoint."""

    codigo: int
    nombre: str
    codigo_iso: str | None


# Backoff delays in seconds, applied between successive attempts.
# The first attempt is immediate; the subsequent attempts wait
# ``1s``, then ``3s``, then ``9s`` before retrying.
_BCU_BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 3.0, 9.0)

# Jitter window in seconds. A random value in ``[0, jitter)`` is
# added to each scheduled delay so multiple workers that restart at the
# same time do not retry in lockstep against the BCU API.
_BCU_BACKOFF_JITTER = 1.0

# Exceptions treated as transient by the shared retry helper. Only
# transport/HTTP errors are retried (4 total attempts with the schedule
# below); permanent response failures (SOAP faults, parse errors) raise
# immediately as :class:`BcuPermanentError` and consume no retries.
_BCU_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (httpx.HTTPError,)

_SOAP_NAMESPACE = "Cotiza"
_SOAP_ACTION = "Cotizaaction/AWSBCUCOTIZACIONES.Execute"
_DEFAULT_TIMEOUT_SECONDS = 30.0

T = TypeVar("T")


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
        "<soapenv:Envelope "
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
    ).encode()


def _first_text(root: etree._Element, suffix: str) -> str | None:
    """Return the text of the first descendant element whose tag ends in ``suffix``.

    Namespaces vary across BCU response versions; matching the local tag
    name is more robust than a hard-coded XPath.
    """

    for element in root.iter():
        if element.tag.endswith(suffix) and element.text is not None:
            text: str = element.text
            return text.strip()
    return None


# Upper bound on the SOAP Fault context included in error messages, so a
# pathological fault body cannot bloat logs or exceptions.
_MAX_FAULT_CONTEXT_CHARS = 512


def _local_name(tag: str) -> str:
    """Return the lower-cased local part of an lxml tag.

    lxml uses Clark notation (``{uri}LocalName``) for namespaced elements;
    unnamespaced tags are returned lower-cased unchanged.
    """

    return tag.rsplit("}", 1)[-1].lower()


def _soap_fault_text(root: etree._Element) -> str | None:
    """Return bounded fault context when the response contains a SOAP Fault.

    Scans all descendants for a local name of ``fault`` (namespaced or
    unnamespaced) and collects the non-empty ``itertext()`` of that
    element, truncated to a safe length for error messages.
    """

    for element in root.iter():
        if _local_name(element.tag) == "fault":
            parts = [
                text.strip()
                for text in element.itertext()
                if isinstance(text, str) and text.strip()
            ]
            return " ".join(parts)[:_MAX_FAULT_CONTEXT_CHARS]
    return None


def _parse_tcc(response_xml: bytes) -> Decimal | None:
    """Extract the first ``TCC`` value from a BCU response.

    Returns ``None`` when the response has no data series for the requested
    date (an empty ``<datos>`` block, which is the BCU's signal for "no
    publication for that day").

    Raises
    ------
    BcuPermanentError
        When the response is malformed XML, contains a SOAP Fault, carries
        a non-numeric or non-finite ``TCC``, or omits both a ``TCC`` and a
        ``<datos>`` element (a structurally invalid response).
    """

    try:
        root = etree.fromstring(response_xml, parser=_SAFE_PARSER)
    except etree.XMLSyntaxError as exc:
        raise BcuPermanentError(f"Malformed BCU response: {exc}") from exc

    fault_text = _soap_fault_text(root)
    if fault_text is not None:
        raise BcuPermanentError(f"BCU SOAP Fault: {fault_text}")

    raw = _first_text(root, "TCC")
    if raw is not None:
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError) as exc:
            raise BcuPermanentError(f"BCU returned non-numeric TCC={raw!r}") from exc
        if not value.is_finite():
            raise BcuPermanentError(f"BCU returned non-finite TCC={raw!r}")
        return value

    # No TCC anywhere: this is the legitimate "no publication" signal only
    # when the response carries a ``<datos>`` element. Anything else is a
    # structurally invalid response, not a confirmed-empty result.
    for element in root.iter():
        if _local_name(element.tag) == "datos":
            return None
    raise BcuPermanentError("BCU response missing TCC and <datos> elements")


def _parse_monedas_response(response_xml: bytes) -> list[BcuCurrency]:
    """Extract the currency catalogue from a ``awsbcumonedas`` response.

    A well-formed response with no entries is a valid empty catalogue
    (``[]``).

    Raises
    ------
    BcuPermanentError
        When the response is malformed XML, contains a SOAP Fault, a
        recognized ``<moneda>``/``<item>`` entry is missing required
        fields, or an entry code is not an integer.
    """

    try:
        root = etree.fromstring(response_xml, parser=_SAFE_PARSER)
    except etree.XMLSyntaxError as exc:
        raise BcuPermanentError(f"Malformed BCU monedas response: {exc}") from exc

    fault_text = _soap_fault_text(root)
    if fault_text is not None:
        raise BcuPermanentError(f"BCU SOAP Fault: {fault_text}")

    currencies: list[BcuCurrency] = []
    for item in root.iter():
        # The ``monedas`` servlet emits ``<moneda>`` elements; older
        # versions used ``<item>``. Match both.
        if not item.tag.endswith("item") and not item.tag.endswith("moneda"):
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
            if (
                tag.endswith("Codigo")
                or tag.endswith("Moneda")
                or tag.endswith("CodigoBCU")
            ):
                codigo_raw = text
            elif tag.endswith("Nombre") or tag.endswith("Descripcion"):
                nombre = text
            elif tag.endswith("CodigoISO") or tag.endswith("ISO"):
                codigo_iso = text
        if codigo_raw is None or nombre is None:
            raise BcuPermanentError(
                "BCU monedas entry missing required code/nombre fields"
            )
        try:
            codigo = int(codigo_raw)
        except ValueError as exc:
            raise BcuPermanentError(
                f"BCU monedas entry has non-integer code {codigo_raw!r}"
            ) from exc
        currencies.append(
            BcuCurrency(codigo=codigo, nombre=nombre, codigo_iso=codigo_iso)
        )

    return currencies


def _bcu_retry(label: str, operation: Callable[[], T]) -> T:
    """Execute ``operation`` with BCU-specific retry and error wrapping.

    Only transport/HTTP errors (``httpx.HTTPError``) are retried, using the
    standard backoff schedule and jitter. Permanent response failures
    (:class:`BcuPermanentError`) and other :class:`BcuError` propagate
    immediately; a final exhausted transport exception is wrapped in the
    base :class:`BcuError` message to preserve the public contract for
    callers.
    """

    try:
        return retry_with_backoff(
            label,
            operation,
            retryable=_BCU_RETRYABLE_EXCEPTIONS,
            backoff_schedule=_BCU_BACKOFF_SCHEDULE,
            jitter=_BCU_BACKOFF_JITTER,
        )
    except BcuError:
        raise
    except Exception as exc:
        raise BcuError(f"{label} failed after retries: {exc}") from exc


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
        # Cells are only set after a response is successfully interpreted;
        # permanent failures leave them absent so a later call can retry.
        self._cache: dict[tuple[int, date], Decimal | None] = {}
        # ``None`` means "the catalogue has not been fetched yet"; ``[]``
        # is a valid cached empty catalogue. Only successfully parsed
        # results are stored here.
        self._monedas_cache: list[BcuCurrency] | None = None

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
        the BCU on weekends and holidays. Permanent failures (SOAP faults,
        malformed responses) and exhausted transport errors are never cached,
        so a later call can retry the BCU.
        """

        for days_back in range(max_lookback_days + 1):
            target_date = on_date - timedelta(days=days_back)
            rate = self._rate_for_date(bcu_code, target_date)
            if rate is not None:
                if days_back > 0:
                    logger.info(
                        "BCU fallback: using %s (TCC=%s) %d day(s) before %s",
                        bcu_code,
                        rate,
                        days_back,
                        on_date,
                    )
                return rate
        logger.warning(
            "BCU rate unavailable: bcu_code=%s on_date=%s within %d-day lookback",
            bcu_code,
            on_date,
            max_lookback_days,
        )
        return None

    def list_monedas(self) -> list[BcuCurrency]:
        """Fetch and cache the full BCU currency catalogue for this client.

        The parsed catalogue is cached for the lifetime of this client
        instance and reused by later calls (e.g. multiple normalizer lines
        resolving unknown currencies). ``None`` means "not fetched yet",
        while a well-formed empty catalogue is cached as ``[]``. Permanent
        failures and exhausted transport errors leave the cache unset so a
        later call can issue a new request.
        """

        if self._monedas_cache is not None:
            return self._monedas_cache

        # Replace the cotizaciones servlet name with the monedas servlet.
        # The base URL always ends in ``.../servlet/awsbcucotizaciones``.
        monedas_url = self._base_url.replace("awsbcucotizaciones", "awsbcumonedas")
        if monedas_url == self._base_url:
            # Fallback: bare host or unexpected path — use sibling path.
            monedas_url = f"{self._base_url}/awsbcumonedas"

        def _fetch() -> list[BcuCurrency]:
            with self.client.stream(
                "GET", monedas_url, timeout=self._timeout
            ) as response:
                response.raise_for_status()
                body = _read_with_limit(response, _MAX_SOAP_SIZE_BYTES)
            return _parse_monedas_response(body)

        catalogue = _bcu_retry("BCU monedas", _fetch)
        # Assign only after the fetch+parse succeeded; exceptions above
        # leave the cache as ``None`` so a later call can retry.
        self._monedas_cache = catalogue
        return catalogue

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rate_for_date(self, bcu_code: int, target_date: date) -> Decimal | None:
        """Return the cached or freshly fetched rate for an exact date.

        The ``(bcu_code, date)`` cell is cached only after the response is
        successfully interpreted: ``None`` is stored for a confirmed empty
        result (no rate published), while SOAP faults, parse failures, and
        exhausted transport errors leave the cell uncached so a later call
        can retry.
        """

        cache_key = (bcu_code, target_date)
        if cache_key in self._cache:
            return self._cache[cache_key]

        rate = self._fetch_tcc_with_retry(bcu_code, target_date)
        # Cache both successful and confirmed-empty results. ``None`` here
        # means "BCU returned an empty <datos> block" — i.e. the date has
        # no rate published. Permanent failures raise above, so the cell
        # stays absent (uncached) rather than being stored as ``None``.
        self._cache[cache_key] = rate
        return rate

    def _fetch_tcc_with_retry(self, bcu_code: int, target_date: date) -> Decimal | None:
        """POST the SOAP envelope, retrying on transient failures."""

        envelope = _build_cotizaciones_envelope(bcu_code, target_date)

        def _fetch() -> Decimal | None:
            with self.client.stream(
                "POST",
                self._base_url,
                content=envelope,
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": _SOAP_ACTION,
                },
            ) as response:
                response.raise_for_status()
                body = _read_with_limit(response, _MAX_SOAP_SIZE_BYTES)
            return _parse_tcc(body)

        return _bcu_retry(
            f"BCU cotizaciones code={bcu_code} date={target_date}",
            _fetch,
        )


__all__ = ["BcuClient", "BcuCurrency", "BcuError", "BcuPermanentError"]
