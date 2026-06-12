"""Join XML adjudication records to RSS feed entries by ``id_compra``.

The two sources are independent: the XML report carries the financial detail,
the RSS feed carries the organism name and the public detail URL. Joining
them by ``id_compra`` produces a complete record ready for normalization and
persistence.

When the RSS feed is unavailable, the caller is expected to short-circuit
and skip the join (see :mod:`scraper.main`). When the RSS feed is available
but a particular ``id_compra`` is missing from one of the two sources, the
mismatched side is logged and the matched side proceeds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date
    from decimal import Decimal

    from scraper.rss_feed import RssItem
    from scraper.xml_report import XmlAdjudication

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JoinedRecord:
    """A fully-enriched adjudication, ready for normalization and insertion.

    ``source_url`` is the URL the XML report was fetched from — the same for
    every record in a run. It is the first column of the unique constraint
    defined on the ``adjudications`` table, so re-scraping the same source
    is naturally idempotent.
    """

    id_compra: str
    fecha_pub_adj: date
    id_tipocompra: str
    id_moneda_monto_adj: int
    nombre_comercial: str
    nro_doc_prov: str | None
    tipo_doc_prov: str | None
    cant_adj: Decimal | None
    precio_tot_imp: Decimal
    desc_articulo: str
    id_moneda: int
    organism: str
    license_link: str
    source_url: str
    id_articulo: str | None


def join_records(
    xml_records: Iterable[XmlAdjudication],
    rss_items: Iterable[RssItem],
    *,
    source_url: str,
) -> list[JoinedRecord]:
    """Join XML and RSS records by ``id_compra``.

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
