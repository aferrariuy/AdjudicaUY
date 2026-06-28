"""Application settings loaded from environment variables.

The web app and the scraper worker share the same settings object. Values are
validated at import time so that a missing or malformed env var fails fast at
startup rather than producing a confusing runtime error later.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_test_mode() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _validate_url(
    value: str,
    *,
    allowed_hosts: set[str],
    allow_http: bool = False,
) -> str:
    """Validate an external URL used by the scraper.

    Enforces HTTPS in production and restricts the host to a known
    allowlist. Test mode (``PYTEST_CURRENT_TEST``) also permits
    ``example.test`` hosts so the test suite can use mocked upstreams.
    """

    parsed = urlparse(value)
    test_hosts = {"example.test"} if _is_test_mode() else set()

    allowed_schemes = {"https"}
    if allow_http:
        allowed_schemes.add("http")
    if parsed.scheme not in allowed_schemes and parsed.netloc not in test_hosts:
        scheme_list = ", ".join(sorted(allowed_schemes))
        raise ValueError(f"URL must use one of {scheme_list}: {value}")

    if parsed.netloc not in allowed_hosts | test_hosts:
        raise ValueError(
            f"URL host {parsed.netloc!r} is not allowed. "
            f"Allowed hosts: {', '.join(sorted(allowed_hosts))}"
        )
    return value


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

    # Scraping source
    # The XML report endpoint is date-dependent; we store the BASE URL
    # (without any date parameters) and have the scraper append the date
    # range on every run. See :mod:`scraper.main` for the URL builder.
    source_a_base_url: str = Field(
        ...,
        description=(
            "Base URL of the XML adjudication source A, without date "
            "query parameters. Example: "
            "http://www.comprasestatales.gub.uy/comprasenlinea/jboss/generarReporte"
        ),
    )

    # External services
    bcu_api_url: str = Field(
        ...,
        description=(
            "Endpoint of the BCU (Banco Central del Uruguay) exchange rate API."
        ),
    )

    allow_http_source_url: bool = Field(
        default=False,
        description=(
            "Allow the Source A scraper URL to use plain HTTP. "
            "Defaults to False; set to True only if the upstream endpoint "
            "genuinely does not support HTTPS."
        ),
    )

    @field_validator("source_a_base_url", mode="before")
    @classmethod
    def _validate_source_a_url(cls, value: str) -> str:
        # ``allow_http`` is read from the raw env during Settings construction;
        # accessing os.environ here avoids Pydantic ordering surprises.
        allow_http = os.environ.get("ALLOW_HTTP_SOURCE_URL", "").lower() in {
            "1",
            "true",
            "yes",
        }
        return _validate_url(
            value,
            allowed_hosts={"comprasestatales.gub.uy", "www.comprasestatales.gub.uy"},
            allow_http=allow_http,
        )

    @field_validator("bcu_api_url", mode="before")
    @classmethod
    def _validate_bcu_url(cls, value: str) -> str:
        return _validate_url(
            value,
            allowed_hosts={"cotizaciones.bcu.gub.uy"},
            allow_http=False,
        )


def get_settings() -> Settings:
    """FastAPI dependency that returns a cached Settings instance."""

    return Settings()  # type: ignore[call-arg]
