"""Join XML adjudication records to RSS feed entries by ``id_compra``.

This module is **legacy**: the live pipeline (see :mod:`scraper.main`)
no longer fetches RSS feeds, and :class:`scraper.normalizer.JoinedRecord`
now lives in the normalizer module (its natural home — see
``Decision: JoinedRecord disposition`` in the design). The class is
re-exported here so any out-of-tree consumer that imported
``scraper.joiner.JoinedRecord`` keeps working until the cleanup PR
deletes this file.

The joiner function itself is kept here (also unused by production) so
the joiner tests can keep exercising the historical logic without
modification. New code should construct :class:`scraper.normalizer.JoinedRecord`
directly from the XML record and the resolved organism/license_link.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# Re-export :class:`JoinedRecord` from its new home so legacy imports
# (``from scraper.joiner import JoinedRecord``) keep resolving. The
# cleanup PR removes this re-export together with the rest of the file.
from scraper.normalizer import JoinedRecord  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import Iterable

    from scraper.rss_feed import RssItem
    from scraper.xml_report import XmlAdjudication

logger = logging.getLogger(__name__)


def join_records(
    xml_records: Iterable[XmlAdjudication],
    rss_items: Iterable[RssItem],
    *,
    source_url: str,
) -> list[JoinedRecord]:
    """Join XML and RSS records by ``id_compra`` (legacy — see module docstring).

    Parameters
    ----------
    xml_records:
        Records produced by :func:`scraper.xml_report.parse_xml_report`.
    rss_items:
        Records produced by :func:`scraper.rss_feed.parse_rss_feed`.
    source_url:
        The URL the XML report was fetched from. Stored on every joined
        record so the unique constraint in the database can dedupe per
        source.

    Returns
    -------
    list[JoinedRecord]
        One record per matched pair, in the order yielded by
        ``xml_records``. Unmatched XML records are logged and dropped —
        the database requires a non-null ``organism``, so we cannot insert
        records that lack an RSS counterpart. Unmatched RSS items are
        logged as well.
    """

    rss_by_id: dict[str, RssItem] = {item.id_compra: item for item in rss_items}

    joined: list[JoinedRecord] = []
    seen_rss_ids: set[str] = set()

    for xml_record in xml_records:
        rss_match = rss_by_id.get(xml_record.id_compra)
        if rss_match is None:
            logger.warning(
                "XML record id_compra=%s has no RSS match; "
                "skipping (organism required)",
                xml_record.id_compra,
            )
            continue

        seen_rss_ids.add(rss_match.id_compra)
        joined.append(
            JoinedRecord(
                id_compra=xml_record.id_compra,
                fecha_pub_adj=xml_record.fecha_pub_adj,
                id_tipocompra=xml_record.id_tipocompra,
                id_moneda_monto_adj=xml_record.id_moneda_monto_adj,
                nombre_comercial=xml_record.nombre_comercial,
                nro_doc_prov=xml_record.nro_doc_prov,
                tipo_doc_prov=xml_record.tipo_doc_prov,
                cant_adj=xml_record.cant_adj,
                precio_tot_imp=xml_record.precio_tot_imp,
                desc_articulo=xml_record.desc_articulo,
                id_moneda=xml_record.id_moneda,
                organism=rss_match.organism,
                license_link=rss_match.license_link,
                source_url=source_url,
                id_articulo=xml_record.id_articulo,
                num_compra=xml_record.num_compra,
                anio_compra=xml_record.anio_compra,
            )
        )

    for rss_id, rss_item in rss_by_id.items():
        if rss_id not in seen_rss_ids:
            logger.warning(
                "RSS item id_compra=%s has no matching XML compra; skipping",
                rss_item.id_compra,
            )

    logger.info(
        "Join complete: %d XML records, %d RSS items, %d joined",
        sum(1 for _ in xml_records)
        if not isinstance(xml_records, list)
        else len(xml_records),
        len(rss_by_id),
        len(joined),
    )
    return joined


__all__ = ["JoinedRecord", "join_records"]
