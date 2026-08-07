"""Informational about page route."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.presenters import _build_seo_context
from app.routes.common import _render

router = APIRouter()


@router.get("/about", response_class=HTMLResponse, include_in_schema=False)
def about(request: Request) -> HTMLResponse:
    """Render the informational about page with SEO context."""

    seo = _build_seo_context(
        meta_title="Sobre AdjudicaUY",
        meta_description=(
            "AdjudicaUY es una plataforma de búsqueda y visualización "
            "de adjudicaciones del Estado uruguayo. Datos públicos, "
            "abiertos y actualizados."
        ),
        og_type="website",
        path="/about",
    )
    return _render("pages/about.html", request, seo)


__all__ = ["router"]
