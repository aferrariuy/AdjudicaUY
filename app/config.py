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
    source_a_url: str = Field(..., description="URL of XML adjudication source A.")
    source_b_url: str = Field(..., description="URL of XML adjudication source B.")

    # External services
    bcu_api_url: str = Field(
        ...,
        description="Endpoint of the BCU (Banco Central del Uruguay) exchange rate API.",
    )


def get_settings() -> Settings:
    """FastAPI dependency that returns a cached Settings instance."""

    return Settings()  # type: ignore[call-arg]
