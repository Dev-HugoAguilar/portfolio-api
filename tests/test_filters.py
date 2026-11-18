"""Pruebas de normalización y validación de filtros (sin base de datos)."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from app.models.filters import SalesFilters, SalesQuery, to_utc_datetime


def _filtros(**cambios) -> SalesFilters:
    base = {
        "dataset_id": "test_ds",
        "date_from": date(2024, 2, 1),
        "date_to": date(2024, 2, 2),
    }
    base.update(cambios)
    return SalesFilters(**base)


def test_las_listas_se_normalizan_deduplican_y_ordenan():
    filtros = _filtros(regions=["Norte", " norte ", "SUR", ""])
    assert filtros.regions == ("norte", "sur")


def test_dataset_id_se_normaliza():
    assert _filtros(dataset_id="  Test_DS ").dataset_id == "test_ds"


@pytest.mark.parametrize(
    "valor",
    ["con espacio", "-guion", "MAYUS!culas", "acentué", "a" * 65],
)
def test_valores_de_filtro_invalidos(valor):
    with pytest.raises(ValidationError):
        _filtros(regions=[valor])


def test_fecha_invertida_es_invalida():
    with pytest.raises(ValidationError):
        _filtros(date_from=date(2024, 2, 2), date_to=date(2024, 2, 1))


def test_rango_de_un_dia_es_valido():
    filtros = _filtros(date_from=date(2024, 2, 1), date_to=date(2024, 2, 1))
    assert filtros.days == 1


def test_limites_temporales_en_utc():
    filtros = _filtros(date_from=date(2024, 2, 1), date_to=date(2024, 2, 2))
    assert filtros.since == datetime(2024, 2, 1, 0, 0, 0)
    # El extremo superior es exclusivo: incluye el día 2 completo.
    assert filtros.until_exclusive == datetime(2024, 2, 3, 0, 0, 0)
    assert to_utc_datetime(date(2024, 2, 2)) == datetime(2024, 2, 2, 0, 0, 0)


def test_mongo_match_incluye_dataset_y_rango_de_fechas():
    filtros = _filtros()
    assert filtros.mongo_match() == {
        "dataset_id": "test_ds",
        "fecha": {"$gte": datetime(2024, 2, 1), "$lt": datetime(2024, 2, 3)},
    }


def test_mongo_match_omite_la_dimension_indicada():
    filtros = _filtros(regions=["norte"], products=["prd_uno"])
    completo = filtros.mongo_match()
    assert completo["region_slug"] == {"$in": ["norte"]}
    assert completo["producto_id"] == {"$in": ["prd_uno"]}

    sin_region = filtros.mongo_match(exclude="regions")
    assert "region_slug" not in sin_region
    assert sin_region["producto_id"] == {"$in": ["prd_uno"]}
    assert sin_region["fecha"] == completo["fecha"]
    assert sin_region["dataset_id"] == "test_ds"


def test_con_dates_conserva_los_filtros_de_negocio():
    filtros = _filtros(regions=["norte"], channels=["retail"], date_from=date(2024, 2, 1))
    otro = filtros.with_dates(date(2024, 1, 30), date(2024, 1, 31))
    assert otro.regions == filtros.regions
    assert otro.channels == filtros.channels
    assert otro.date_from == date(2024, 1, 30)
    assert otro.date_to == date(2024, 1, 31)
    assert otro.days == filtros.days


def test_normalized_devuelve_fechas_iso_y_listas():
    datos = _filtros(regions=["sur", "norte"]).normalized()
    assert datos["date_from"] == "2024-02-01"
    assert datos["date_to"] == "2024-02-02"
    assert datos["regions"] == ["norte", "sur"]
    assert datos["channels"] == []


def test_sales_query_valida_orden_y_direccion():
    assert SalesQuery(page=2, page_size=10, sort="margen_cent", order="asc").sort == "margen_cent"
    with pytest.raises(ValidationError):
        SalesQuery(sort="password")
    with pytest.raises(ValidationError):
        SalesQuery(order="aleatorio")
    with pytest.raises(ValidationError):
        SalesQuery(page=0)
