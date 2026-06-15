"""One-time script: backfill id_inciso and id_ue for existing records.

Re-fetches the XML reports for each date that has records in the
database, parses them to extract (id_inciso, id_ue) per id_compra,
and updates rows where id_inciso IS NULL.

The script writes against the ``compra`` table — the old flat
``adjudications`` table no longer exists (see migration
``006_drop_legacy_adjudications``). The backfill updates Compra
rows directly; child ``adjudicacion`` rows do not need to be
touched because they share the same compra_id.

Usage::

    PYTHONPATH=. python scripts/backfill_inciso_ue.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import time

import httpx
from sqlalchemy import select, update

from app.config import get_settings
from app.database import get_session_factory
from app.models.compra import Compra
from scraper.main import build_source_a_url
from scraper.xml_report import fetch_xml_report, parse_xml_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("backfill_inciso_ue")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill id_inciso and id_ue")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without writing",
    )
    args = parser.parse_args()

    settings = get_settings()
    session = get_session_factory()()
    client = httpx.Client(
        timeout=30.0,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        },
    )

    try:
        # 1. Get all distinct dates that have records missing id_inciso.
        # The natural join via Compra.fecha_pub_adj (since the new
        # table has one row per id_compra) keeps the lookup small.
        result = session.execute(
            select(Compra.fecha_pub_adj)
            .where(Compra.id_inciso.is_(None))
            .distinct()
            .order_by(Compra.fecha_pub_adj)
        )
        dates = [row[0] for row in result]
        logger.info("Found %d dates with missing id_inciso/id_ue", len(dates))

        if not dates:
            logger.info("Nothing to backfill — all records already have id_inciso")
            return

        total_updated = 0
        total_skipped = 0

        for d in dates:
            logger.info("Processing %s", d)

            # 2. Fetch XML for this date
            url = build_source_a_url(settings.source_a_base_url, d)
            try:
                xml_text = fetch_xml_report(url, client=client)
            except httpx.HTTPError as exc:
                logger.warning("XML fetch failed for %s: %s — skipping", d, exc)
                continue

            # 3. Parse XML and build id_compra → (id_inciso, id_ue) lookup
            lookup: dict[str, tuple[int | None, int | None]] = {}
            for xml_compra in parse_xml_report(xml_text):
                lookup[xml_compra.id_compra] = (
                    xml_compra.id_inciso,
                    xml_compra.id_ue,
                )

            logger.info("  Parsed %d records from XML", len(lookup))

            # 4. Get all compra rows for this date that need updating.
            rows = session.execute(
                select(Compra)
                .where(Compra.fecha_pub_adj == d)
                .where(Compra.id_inciso.is_(None))
            ).scalars().all()

            updated = 0
            skipped = 0

            for row in rows:
                if row.id_compra not in lookup:
                    skipped += 1
                    continue
                id_inciso, id_ue = lookup[row.id_compra]
                if id_inciso is None or id_ue is None:
                    skipped += 1
                    continue
                if not args.dry_run:
                    session.execute(
                        update(Compra)
                        .where(Compra.id == row.id)
                        .values(id_inciso=id_inciso, id_ue=id_ue)
                    )
                updated += 1

            total_updated += updated
            total_skipped += skipped
            logger.info("  %s: %d updated, %d skipped", d, updated, skipped)

            if not args.dry_run:
                session.commit()

            # Be polite to the government server
            time.sleep(0.5)

        logger.info(
            "═══ DONE: %d updated, %d skipped ═══", total_updated, total_skipped
        )

    finally:
        client.close()
        session.close()


if __name__ == "__main__":
    main()
