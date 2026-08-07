"""Aggregate route modules for the AdjudicaUY application."""

from fastapi import APIRouter

from app.main import HeadAwareAPIRoute

from app.routes.about import router as about_router
from app.routes.company import router as company_router
from app.routes.dashboard import router as dashboard_router
from app.routes.organism import router as organism_router

router = APIRouter(route_class=HeadAwareAPIRoute)
router.include_router(dashboard_router)
router.include_router(organism_router)
router.include_router(company_router)
router.include_router(about_router)

__all__ = ["router"]
