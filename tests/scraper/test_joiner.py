"""Unit tests for :mod:`scraper.joiner`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from scraper.joiner import JoinedRecord, join_records

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_join_matches_records_by_id_compra(make_xml_record, make_rss_item) -> None:
    xml = [
        make_xml_record(id_compra="1", nombre_comercial="A"),
        make_xml_record(id_compra="2", nombre_comercial="B"),
    ]
    rss = [
        make_rss_item(id_compra="1", organism="Org1"),
        make_rss_item(id_compra="2", organism="Org2"),
    ]

    joined = join_records(xml, rss, source_url="https://example.test/xml")

    assert len(joined) == 2
    assert joined[0].id_compra == "1"
    assert joined[0].organism == "Org1"
    assert joined[1].id_compra == "2"
    assert joined[1].organism == "Org2"


def test_join_attaches_source_url_to_every_record(
    make_xml_record, make_rss_item
) -> None:
    xml = [make_xml_record(id_compra="1")]
    rss = [make_rss_item(id_compra="1")]

    joined = join_records(xml, rss, source_url="https://example.test/source-A")

    assert joined[0].source_url == "https://example.test/source-A"


def test_join_preserves_xml_field_values(make_xml_record, make_rss_item) -> None:
    xml = [
        make_xml_record(
            id_compra="1",
            fecha_pub_adj=date(2024, 5, 1),
            id_tipocompra="CD",
            id_moneda_monto_adj=20,
            nombre_comercial="Empresa SA",
            nro_doc_prov="210000000018",
            tipo_doc_prov="RUT",
            cant_adj=Decimal("3.00"),
            precio_tot_imp=Decimal("1500.00"),
            desc_articulo="Silla ergonomica",
            id_moneda=20,
        ),
    ]
    rss = [make_rss_item(id_compra="1", organism="OSE")]

    joined = join_records(xml, rss, source_url="https://example.test/xml")

    assert len(joined) == 1
    record = joined[0]
    assert record.fecha_pub_adj == date(2024, 5, 1)
    assert record.id_tipocompra == "CD"
    assert record.id_moneda_monto_adj == 20
    assert record.nombre_comercial == "Empresa SA"
    assert record.nro_doc_prov == "210000000018"
    assert record.tipo_doc_prov == "RUT"
    assert record.cant_adj == Decimal("3.00")
    assert record.precio_tot_imp == Decimal("1500.00")
    assert record.desc_articulo == "Silla ergonomica"
    assert record.id_moneda == 20


def test_join_preserves_license_link_from_rss(make_xml_record, make_rss_item) -> None:
    xml = [make_xml_record(id_compra="1")]
    rss = [
        make_rss_item(
            id_compra="1",
            organism="Org",
            license_link="https://example.test/consultas/detalle/id/1",
        )
    ]

    joined = join_records(xml, rss, source_url="https://example.test/xml")

    assert joined[0].license_link == ("https://example.test/consultas/detalle/id/1")


# ---------------------------------------------------------------------------
# Unmatched records — both sides are logged
# ---------------------------------------------------------------------------


def test_join_drops_xml_records_without_rss_match(
    make_xml_record, make_rss_item, caplog
) -> None:
    xml = [make_xml_record(id_compra="orphan-xml")]
    rss = [make_rss_item(id_compra="present-in-rss")]

    with caplog.at_level("WARNING", logger="scraper.joiner"):
        joined = join_records(xml, rss, source_url="https://example.test/xml")

    assert joined == []
    # The XML side is logged.
    assert any("orphan-xml" in record.message for record in caplog.records)


def test_join_logs_rss_items_without_xml_match(
    make_xml_record, make_rss_item, caplog
) -> None:
    xml = [make_xml_record(id_compra="present-in-xml")]
    rss = [
        make_rss_item(id_compra="present-in-xml"),
        make_rss_item(id_compra="orphan-rss", organism="Lone"),
    ]

    with caplog.at_level("WARNING", logger="scraper.joiner"):
        joined = join_records(xml, rss, source_url="https://example.test/xml")

    assert len(joined) == 1
    assert any("orphan-rss" in record.message for record in caplog.records)


def test_join_with_empty_inputs_returns_empty_list() -> None:
    assert join_records([], [], source_url="https://example.test/xml") == []


def test_join_returns_empty_when_no_ids_overlap(make_xml_record, make_rss_item) -> None:
    xml = [make_xml_record(id_compra="xml-1")]
    rss = [make_rss_item(id_compra="rss-1")]

    joined = join_records(xml, rss, source_url="https://example.test/xml")
    assert joined == []


def test_join_preserves_iterable_order_of_xml_records(
    make_xml_record, make_rss_item
) -> None:
    # The joiner MUST walk the XML records in input order, not
    # alphabetical, so callers get a stable join result.
    xml = [
        make_xml_record(id_compra="z-third", nombre_comercial="Z"),
        make_xml_record(id_compra="a-first", nombre_comercial="A"),
        make_xml_record(id_compra="m-second", nombre_comercial="M"),
    ]
    rss = [
        make_rss_item(id_compra="z-third"),
        make_rss_item(id_compra="a-first"),
        make_rss_item(id_compra="m-second"),
    ]

    joined = join_records(xml, rss, source_url="https://example.test/xml")

    assert [r.nombre_comercial for r in joined] == ["Z", "A", "M"]


def test_join_returns_joined_record_dataclass_instances(
    make_xml_record, make_rss_item
) -> None:
    xml = [make_xml_record(id_compra="1")]
    rss = [make_rss_item(id_compra="1")]

    joined = join_records(xml, rss, source_url="https://example.test/xml")

    assert isinstance(joined[0], JoinedRecord)
