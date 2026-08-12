"""Shared route infrastructure for the FastAPI application.

Dependency-neutral: only FastAPI routing primitives. Route modules and
``app.main`` import :class:`HeadAwareAPIRoute` from here so the import
graph stays acyclic (routes never import ``app.main``).
"""

from __future__ import annotations

from fastapi.routing import APIRoute


class HeadAwareAPIRoute(APIRoute):
    """Make every GET route accept HEAD without duplicating decorators."""

    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        methods = kwargs.get("methods")
        if methods and "GET" in methods:
            kwargs["methods"] = set(methods) | {"HEAD"}
        super().__init__(*args, **kwargs)

    def get_route_handler(self):  # noqa: ANN201
        handler = super().get_route_handler()

        async def route_handler(request):  # noqa: ANN001
            response = await handler(request)
            if "GET" in self.methods:
                response.headers["Allow"] = ", ".join(sorted(self.methods))
            return response

        return route_handler
