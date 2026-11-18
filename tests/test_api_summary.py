"""Pruebas de integración del resumen contra un MongoDB real.

Todos los valores esperados están calculados a mano en
:mod:`tests.dataset_fixture`.
"""

from __future__ import annotations

import pytest

from app.db.mongo import SALES_COLLECTION
from app.services.metrics import margin_pct
from tests.dataset_fixture import (
    ACTUAL,
    ANTERIOR,
    RANGO_COMPLETO,
    VARIACION,
    linea_cero,
)

pytestmark = pytest.mark.integracion

RESUMEN = "/api/v1/dashboards/profitability/summary"
DETALLE = "/api/v1/dashboards/profitability/sales"


def resumen(client, **params) -> dict:
    respuesta = client.get(RESUMEN, params=params)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def test_kpis_del_periodo_actual_calculados_a_mano(client):
    datos = resumen(client, date_from="2024-02-01", date_to="2024-02-02")
    kpis = datos["kpis"]
    for campo, esperado in ACTUAL.items():
        assert kpis[campo] == esperado, f"KPI {campo}"


def test_el_periodo_devuelto_es_el_solicitado(client):
    datos = resumen(client, date_from="2024-02-01", date_to="2024-02-02")
    assert datos["periodo"] == {
        "desde": "2024-02-01",
        "hasta": "2024-02-02",
        "dias": 2,
    }
    assert datos["filtros"]["date_from"] == "2024-02-01"
    assert datos["filtros"]["regions"] == []
    assert datos["dataset"]["dataset_id"] == "test_ds"
    assert datos["dataset_version"] == 1


def test_comparacion_con_el_periodo_anterior_equivalente(client):
    datos = resumen(client, date_from="2024-02-01", date_to="2024-02-02")
    comparacion = datos["comparacion"]

    assert comparacion["periodo"] == {
        "desde": "2024-01-30",
        "hasta": "2024-01-31",
        "dias": 2,
    }
    assert comparacion["motivo"] is None
    for campo, esperado in ANTERIOR.items():
        assert comparacion["kpis"][campo] == esperado, f"KPI anterior {campo}"

    assert comparacion["variacion_venta_neta_pct"] == VARIACION["venta_neta_pct"]
    assert comparacion["variacion_margen_pct"] == VARIACION["margen_pct"]
    assert comparacion["variacion_margen_pt"] == VARIACION["margen_pt"]


def test_periodo_por_defecto_es_el_del_dataset(client):
    datos = resumen(client)
    assert datos["periodo"] == {"desde": "2024-01-30", "hasta": "2024-02-02", "dias": 4}
    for campo, esperado in RANGO_COMPLETO.items():
        assert datos["kpis"][campo] == esperado, f"KPI {campo}"


def test_sin_cobertura_del_periodo_anterior_no_se_inventan_variaciones(client):
    datos = resumen(client)
    comparacion = datos["comparacion"]
    assert comparacion["motivo"] == "sin_cobertura_periodo_anterior"
    assert comparacion["kpis"] is None
    assert comparacion["variacion_venta_neta_pct"] is None
    assert comparacion["variacion_margen_pt"] is None
    assert any("periodo anterior" in aviso for aviso in datos["meta"]["advertencias"])


def test_serie_mensual(client):
    serie = resumen(client)["serie"]
    assert [punto["periodo"] for punto in serie] == ["2024-01", "2024-02"]

    enero, febrero = serie
    assert enero["lineas"] == 2
    assert enero["venta_neta_cent"] == 110_000
    assert enero["margen_cent"] == 58_000
    assert enero["margen_pct"] == 52.73
    assert enero["desde"] == "2024-01-30"
    assert enero["hasta"] == "2024-01-31"

    assert febrero["lineas"] == 4
    assert febrero["venta_neta_cent"] == 202_500
    assert febrero["margen_cent"] == 50_500
    assert febrero["margen_pct"] == 24.94


def test_desglose_por_region(client):
    regiones = resumen(client)["por_region"]
    assert [region["valor"] for region in regiones] == ["sur", "norte"]

    sur, norte = regiones
    assert sur["etiqueta"] == "Sur"
    assert sur["lineas"] == 3
    assert sur["venta_neta_cent"] == 227_500
    assert sur["margen_cent"] == 67_500
    assert sur["margen_pct"] == 29.67

    assert norte["lineas"] == 3
    assert norte["venta_neta_cent"] == 85_000
    assert norte["margen_cent"] == 41_000
    assert norte["margen_pct"] == 48.24


def test_desglose_por_canal(client):
    canales = resumen(client)["por_canal"]
    assert [canal["valor"] for canal in canales] == ["mayorista", "retail"]
    assert canales[0]["venta_neta_cent"] == 227_500
    assert canales[1]["venta_neta_cent"] == 85_000


def test_productos_destacados_y_dispersion(client):
    datos = resumen(client)
    productos = datos["productos_destacados"]
    assert [producto["producto_id"] for producto in productos] == ["prd_dos", "prd_uno"]
    assert productos[0]["venta_neta_cent"] == 225_000
    assert productos[0]["categoria"] == "Categoría B"
    assert productos[1]["venta_neta_cent"] == 87_500

    clientes = datos["dispersion"]
    assert [punto["cliente_id"] for punto in clientes] == ["cli_dos", "cli_uno"]
    assert clientes[0]["venta_neta_cent"] == 227_500
    assert clientes[1]["margen_pct"] == 48.24


def test_filtro_por_region(client):
    datos = resumen(client, region="norte")
    assert datos["kpis"]["lineas"] == 3
    assert datos["kpis"]["venta_neta_cent"] == 85_000
    assert datos["kpis"]["margen_cent"] == 41_000
    assert datos["kpis"]["margen_pct"] == 48.24
    assert datos["filtros"]["regions"] == ["norte"]


def test_filtro_combinado_region_y_categoria(client):
    datos = resumen(client, region="norte", category="cat_a")
    assert datos["kpis"]["lineas"] == 2
    assert datos["kpis"]["venta_neta_cent"] == 40_000
    assert datos["kpis"]["coste_cent"] == 24_000
    assert datos["kpis"]["margen_cent"] == 16_000
    assert datos["kpis"]["margen_pct"] == 40.0
    assert datos["filtros"] == {
        "dataset_id": "test_ds",
        "date_from": "2024-01-30",
        "date_to": "2024-02-02",
        "regions": ["norte"],
        "channels": [],
        "categories": ["cat_a"],
        "products": [],
        "customers": [],
    }


def test_varias_regiones_a_la_vez(client):
    datos = resumen(client, region=["norte", "sur"])
    assert datos["filtros"]["regions"] == ["norte", "sur"]
    assert datos["kpis"]["lineas"] == RANGO_COMPLETO["lineas"]


def test_opciones_de_filtro_dependientes(client):
    # Sin filtros: cada dimensión ve todo el dataset.
    datos = resumen(client)
    assert {opcion["valor"]: opcion["conteo"] for opcion in datos["opciones_filtros"]["regiones"]} == {
        "norte": 3,
        "sur": 3,
    }

    # Con región elegida, las opciones de canal solo incluyen canales de esa región.
    datos = resumen(client, region="norte")
    canales = datos["opciones_filtros"]["canales"]
    assert [opcion["valor"] for opcion in canales] == ["retail"]
    assert canales[0]["conteo"] == 3

    # Las opciones de región NO se restringen por el filtro de región activo.
    regiones = datos["opciones_filtros"]["regiones"]
    assert [opcion["valor"] for opcion in regiones] == ["norte", "sur"]
    assert [opcion["conteo"] for opcion in regiones] == [3, 3]
    assert regiones[0]["seleccionado"] is True
    assert regiones[1]["seleccionado"] is False

    # Con región y canal, las opciones de categoría aplican ambos filtros.
    datos = resumen(client, region="norte", channel="retail")
    assert {opcion["valor"]: opcion["conteo"] for opcion in datos["opciones_filtros"]["categorias"]} == {
        "cat_a": 2,
        "cat_b": 1,
    }


def test_la_seleccion_activa_se_mantiene_aunque_no_tenga_filas(client):
    datos = resumen(client, region="sureste")
    assert datos["kpis"]["lineas"] == 0
    assert datos["kpis"]["margen_pct"] is None
    assert datos["kpis"]["ticket_medio_cent"] is None

    regiones = {opcion["valor"]: opcion for opcion in datos["opciones_filtros"]["regiones"]}
    assert regiones["sureste"]["conteo"] == 0
    assert regiones["sureste"]["seleccionado"] is True
    assert any("Ningún dato coincide" in aviso for aviso in datos["meta"]["advertencias"])


def test_la_seleccion_sin_filas_conserva_su_etiqueta(client):
    # prd_dos solo tiene líneas de cat_b: con category=cat_a no aparece en las
    # opciones, pero la selección activa debe seguir viéndose con su nombre.
    datos = resumen(client, product="prd_dos", category="cat_a")
    assert datos["kpis"]["lineas"] == 0

    productos = {opcion["valor"]: opcion for opcion in datos["opciones_filtros"]["productos"]}
    assert "prd_uno" in productos
    assert productos["prd_dos"]["etiqueta"] == "Producto Dos"
    assert productos["prd_dos"]["conteo"] == 0
    assert productos["prd_dos"]["seleccionado"] is True


def test_base_cero_devuelve_margen_nulo(client, mongo_db):
    mongo_db[SALES_COLLECTION].insert_one(linea_cero())

    datos = resumen(
        client, product="prd_cero", date_from="2024-02-01", date_to="2024-02-02"
    )
    assert datos["kpis"]["lineas"] == 1
    assert datos["kpis"]["venta_neta_cent"] == 0
    assert datos["kpis"]["margen_cent"] == 0
    assert datos["kpis"]["margen_pct"] is None
    # No hay base en el periodo anterior: nunca se inventa un porcentaje.
    assert datos["comparacion"]["motivo"] == "base_cero"
    assert datos["comparacion"]["variacion_venta_neta_pct"] is None


def test_resumen_y_detalle_son_coherentes(client):
    datos = resumen(client)
    detalle = client.get(DETALLE, params={"page_size": 200}).json()

    assert detalle["total"] == datos["kpis"]["lineas"]
    assert sum(item["venta_neta_cent"] for item in detalle["items"]) == datos["kpis"]["venta_neta_cent"]
    assert sum(item["margen_cent"] for item in detalle["items"]) == datos["kpis"]["margen_cent"]
    assert sum(item["venta_bruta_cent"] for item in detalle["items"]) == datos["kpis"]["venta_bruta_cent"]
    assert sum(item["descuento_cent"] for item in detalle["items"]) == datos["kpis"]["descuento_cent"]
    assert sum(item["coste_cent"] for item in detalle["items"]) == datos["kpis"]["coste_cent"]
    assert sum(item["cantidad"] for item in detalle["items"]) == datos["kpis"]["unidades"]

    for item in detalle["items"]:
        assert item["margen_pct"] == margin_pct(item["venta_neta_cent"], item["margen_cent"])


def test_meta_documenta_el_origen_sintetico_y_la_moneda(client):
    meta = resumen(client)["meta"]
    assert meta["moneda"] == "MXN"
    assert meta["granularidad"] == "mes"
    assert "centavos" in meta["moneda_nota"]
    assert any("sintéticos" in aviso for aviso in meta["advertencias"])
    assert any("devoluciones" in aviso for aviso in meta["advertencias"])
    assert any("utilidad operativa" in aviso for aviso in meta["advertencias"])
