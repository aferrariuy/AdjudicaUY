"""Application settings loaded from environment variables.

The web app and the scraper worker share the same settings object. Values are
validated at import time so that a missing or malformed env var fails fast at
startup rather than producing a confusing runtime error later.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Project-wide configuration sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        ...,
        description=(
            "SQLAlchemy URL for the PostgreSQL database. "
            "Example: postgresql+psycopg2://user:pass@host:5432/adjudicauy"
        ),
    )

    # Scraping sources
    # Both sources expose date-dependent endpoints; we store the BASE URL
    # (without any date parameters) and have the scraper append the date
    # range on every run. See :mod:`scraper.main` for the URL builders.
    source_a_base_url: str = Field(
        ...,
        description=(
            "Base URL of the XML adjudication source A, without date "
            "query parameters. Example: "
            "http://www.comprasestatales.gub.uy/comprasenlinea/jboss/generarReporte"
        ),
    )
    source_b_base_url: str = Field(
        ...,
        description=(
            "Base URL of the RSS adjudication source B, without the "
            "``rango-fecha/<start>_<end>`` path segment. Example: "
            "https://www.comprasestatales.gub.uy/consultas/rss/tipo-pub/ADJ"
            "/tipo-doc/C/tipo-fecha/PUB/rango-fecha"
        ),
    )
    source_b_rss_base: str | None = Field(
        default=None,
        description=(
            "Base URL of the per-compra RSS feed (source B), used as fallback "
            "enrichment for XML records missing from the day-RSS. The per-compra "
            "URL appends ``/tipo-pub/ADJ/nro-compra/<num_compra>/"
            "anio-compra/<anio_compra>``. When ``None`` (the default), it is "
            "derived from ``source_b_base_url`` by truncating at "
            "``/consultas/rss``. "
            "Example: https://www.comprasestatales.gub.uy/consultas/rss"
        ),
    )

    # External services
    bcu_api_url: str = Field(
        ...,
        description=(
            "Endpoint of the BCU (Banco Central del Uruguay) exchange rate API."
        ),
    )


def get_settings() -> Settings:
    """FastAPI dependency that returns a cached Settings instance."""

    return Settings()  # type: ignore[call-arg]
