"""Shared rendering, validation, pagination, and CSV route helpers."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import TYPE_CHECKING, Any, cast

from fastapi.responses import HTMLResponse, Response, StreamingResponse
from markupsafe import escape

from app.database import get_session_factory
from app.services.filters import (
    AdjudicationFilters,
    filters_from_query_params,
)
from app.services.listing import (
    MAX_EXPORT_ROWS,
    count_adjudications,
    iter_adjudications,
)

if TYPE_CHECKING:
    from fastapi import Request

PAGE_SIZE = 10
RANKING_LIMIT = 10
ORGANISM_SUGGEST_LIMIT = 200

_ERROR_FRAGMENT_TEMPLATE = (
    '<div class="bg-red-50 border border-red-300 rounded-lg p-4" role="alert">'
    '<p class="text-red-800 text-sm">{message}</p>'
    "</div>"
)


def _inject_default_year_params(params: dict[str, str | None]) -> None:
    """Default the date range to the current calendar year when absent.

    Mutates ``params`` in place. Only fires when BOTH ``date_from`` and
    ``date_to`` are missing or empty — a single explicit param is left
    alone (see the spec, "Only one date param provided — no default
    injection" scenario).
    """

    if not params.get("date_from") and not params.get("date_to"):
        today = date.today()
        params["date_from"] = f"{today.year}-01-01"
        params["date_to"] = f"{today.year}-12-31"


def _validation_error_response(message: str) -> HTMLResponse:
    """Build a 422 response with the inline error fragment.

    The message is escaped before interpolation so that any future code
    path that passes user input here cannot inject HTML/JS into the
    fragment.
    """

    return HTMLResponse(
        _ERROR_FRAGMENT_TEMPLATE.format(message=escape(message)),
        status_code=422,
    )


def _validation_error_context(
    message: str,
    *,
    raw_params: dict[str, str | None] | None = None,
    organism_name: str | None = None,
    company_identity: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Build the minimal context for a full-page 422 render.

    The full-page routes (``GET /`` and ``GET /organism/{name}``) catch
    ``ValidationError`` *before* running any DB queries, so the
    template only needs the bare minimum to render the chrome (filter
    form + error fragment) without raising ``UndefinedError`` on
    ``filters`` / ``organisms`` / etc. The error is stored in
    ``validation_error``; both ``index.html`` and
    ``organism_detail.html`` look for that key to swap the
    ``#results`` / ``#organism-body`` body for the alert fragment.

    Parameters
    ----------
    message
        Spanish error string from :class:`ValidationError`.
    raw_params
        Echoed back into ``filters`` so the filter form preserves the
        user's input (``date_from``, ``date_to``, etc.) when the page
        reloads with the error — the user can correct the date and
        resubmit without retyping. Defaults to an empty dict.
    organism_name
        Required only for ``organism_detail.html`` so the ``<h1>`` and
        ``<title>`` render the requested organism even on the error
        path. ``index.html`` ignores it.
    """

    params = raw_params if raw_params is not None else {}
    filters = filters_from_query_params(params)
    context: dict[str, Any] = {
        "filters": filters,
        "organisms": [],
        "validation_error": message,
    }
    if organism_name is not None:
        context["organism_name"] = organism_name
    if company_identity is not None:
        context["company_type"], context["company_number"] = company_identity
        context["company_name"] = None
    return context


def _full_page_validation_error(
    template_name: str,
    request: Request,
    message: str,
    *,
    raw_params: dict[str, str | None] | None = None,
    organism_name: str | None = None,
    company_identity: tuple[str, str] | None = None,
) -> HTMLResponse:
    """Build a full-page 422 response: render ``template_name`` with the error.

    Full-page counterpart of :func:`_validation_error_response`. The
    HTMX partials use that helper (the page chrome is already on screen
    and HTMX swaps the fragment into ``#results`` / ``#organism-body``);
    the full-page routes use this one so a direct navigation to
    ``GET /`` or ``GET /organism/{name}`` renders the page layout
    around the error.
    """

    return HTMLResponse(
        _render_str(
            template_name,
            request,
            _validation_error_context(
                message,
                raw_params=raw_params,
                organism_name=organism_name,
                company_identity=company_identity,
            ),
        ),
        status_code=422,
    )


def _coerce_page(raw: str | None) -> int:
    """Parse a raw ``page`` query param, returning 1 for missing/garbage values.

    The route never returns a 4xx for a malformed ``page`` value — we
    silently fall back to the first page. The caller is responsible for
    clamping to ``>= 1`` (negative values map to 1 too, but a positive
    integer stays as-is at this layer so the caller can distinguish
    ``page=0`` from ``page=1`` if it wants to).
    """

    if raw is None:
        return 1
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return 1


def _render_str(template_name: str, request: Request, context: dict[str, Any]) -> str:
    """Render ``template_name`` with ``context`` to an HTML string.

    Used by routes that need to set a non-default status code on the
    response (e.g. the full-page 422 path). The default-status variant
    :func:`_render` wraps this in an :class:`HTMLResponse`.
    """

    templates = request.app.state.templates
    template = templates.get_template(template_name)
    return cast("str", template.render({**context, "request": request}))


def _render(
    template_name: str, request: Request, context: dict[str, Any]
) -> HTMLResponse:
    """Render ``template_name`` with ``context`` using the app's Jinja env."""

    return HTMLResponse(_render_str(template_name, request, context))


_CSV_COLUMNS = [
    "fecha",
    "organismo",
    "empresa_adjudicataria",
    "articulo",
    "monto",
    "moneda",
    "monto_uyu",
    "tipo_compra",
    "documento_empresa",
    "tipo_documento",
    "id_articulo",
    "id_compra",
    "link_licitacion",
]


def _row_to_csv_fields(row: Any) -> list[str]:
    """Map an :class:`AdjudicationRow` to the CSV column values.

    Raw values only — no es-UY formatting. Dates are ISO, decimals are
    unformatted, NULLs become empty strings.
    """

    return [
        row.date.isoformat(),
        row.organism or "",
        row.winning_company or "",
        row.article or "",
        str(row.amount) if row.amount is not None else "",
        row.currency or "",
        str(row.amount_uyu) if row.amount_uyu is not None else "",
        row.license_type or "",
        row.company_document or "",
        row.company_document_type or "",
        row.article_id or "",
        row.id_compra or "",
        row.license_link or "",
    ]


def _stream_csv_response(filters: AdjudicationFilters) -> Response:
    """Stream the standard CSV response for a prepared filter set.

    The generator owns a manual session because ``StreamingResponse`` begins
    iterating after the route returns. Keeping this shared by the global and
    company exports guarantees identical columns, limits, and lifecycle.
    """

    # Manual session — the generator closes it.
    session = get_session_factory()()
    try:
        total = count_adjudications(session, filters)
    except Exception:
        session.close()
        raise

    if total > MAX_EXPORT_ROWS:
        session.close()
        return Response(
            "La exportación supera el límite de 500.000 filas. Ajuste los filtros.",
            status_code=400,
            media_type="text/plain",
        )

    def _csv_generator() -> Any:
        """Yield CSV bytes: BOM → header → data rows."""

        try:
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\r\n")

            # UTF-8 BOM for Excel compatibility.
            yield "\ufeff"

            # Header row.
            writer.writerow(_CSV_COLUMNS)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

            # Data rows.
            for row in iter_adjudications(session, filters):
                writer.writerow(_row_to_csv_fields(row))
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        finally:
            session.close()

    return StreamingResponse(
        _csv_generator(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="adjudicaciones.csv"',
        },
    )


__all__ = [
    "ORGANISM_SUGGEST_LIMIT",
    "PAGE_SIZE",
    "RANKING_LIMIT",
    "_CSV_COLUMNS",
    "_ERROR_FRAGMENT_TEMPLATE",
    "_coerce_page",
    "_full_page_validation_error",
    "_inject_default_year_params",
    "_render",
    "_render_str",
    "_row_to_csv_fields",
    "_stream_csv_response",
    "_validation_error_context",
    "_validation_error_response",
    "get_session_factory",
    "MAX_EXPORT_ROWS",
]
