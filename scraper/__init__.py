"""Scraper worker package.

This package fetches adjudications from public Uruguayan procurement sources,
normalizes foreign-currency amounts to UYU via the BCU exchange rate API, and
inserts the results into the PostgreSQL database used by the web app.

The entry point is :func:`scraper.main.run_scrape`, invoked by the worker
container (or by a developer running ``python -m scraper.main``).
"""

from __future__ import annotations
