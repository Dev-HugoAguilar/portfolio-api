"""Pruebas de integración del detalle, la exportación CSV y los límites de la API."""

from __future__ import annotations

import csv
import io

import pytest

from app.config import get_settings
from app.db.mongo import SALES_COLLECTION
from tests.dataset_fixture import linea_peligrosa

pytestmark = pytest.mark.integracion

DETALLE = "/api/v1/dashboards/profitability/sales"
CSV_URL = "/api/v1/dashboards/profitability/sales.csv"


def detalle(client, **params) -> dict:
    respuesta = client.get(DETALLE, params=params)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def test_paginacion(client):
    primera = detalle(client, page=1, page_size=2)
    assert primera["total"] == 6
    assert primera["pages"] == 3
    assert primera["page"] == 1
    assert len(primera["items"]) == 2

    ultima = detalle(client, page=3, page_size=2)
    assert len(ultima["items"]) == 2

    fuera = detalle(client, page=4, page_size=2)
    assert fuera["items"] == []
    assert fuera["total"] == 6


def test_las_paginas_no_repiten_lineas(client):
    vistos = []
    for pagina in (1, 2, 3):
        vistos.extend(
            item["linea_id"] for item in detalle(client, page=pagina, page_size=2)["items"]
        )
    assert len(vistos) == len(set(vistos)) == 6


def test_orden_por_venta_neta_descendente(client):
    items = detalle(client, sort="venta_neta_cent", order="desc")["items"]
    importes = [item["venta_neta_cent"] for item in items]
    assert importes == sorted(importes, reverse=True)
    assert importes[0] == 90_000
    assert items[0]["linea_id"] == "ln00000004"


def test_orden_por_margen_ascendente(client):
    items = detalle(client, sort="margen_cent", order="asc")["items"]
    margenes = [item["margen_cent"] for item in items]
    assert margenes == sorted(margenes)
    assert margenes[0] == 0
    assert margenes[-1] == 50_000


def test_orden_estable_con_empates(client):
    items = detalle(client, sort="margen_cent", order="desc")["items"]
    # ln00000001 y ln00000005 empatan en 8.000: se desempata por linea_id.
    ids = [item["linea_id"] for item in items]
    assert ids.index("ln00000001") < ids.index("ln00000005")


def test_orden_no_permitido_devuelve_422(client):
    assert client.get(DETALLE, params={"sort": "password"}).status_code == 422
    assert client.get(DETALLE, params={"order": "arriba"}).status_code == 422
    assert client.get(DETALLE, params={"page": 0}).status_code == 422


def test_page_size_maximo(client, settings):
    limite = settings.max_page_size
    superado = client.get(DETALLE, params={"page_size": limite + 1})
    assert superado.status_code == 422
    assert str(limite) in superado.json()["detail"]
    assert client.get(DETALLE, params={"page_size": limite}).status_code == 200


def test_limites_temporales(client):
    fuera_de_cobertura = client.get(
        DETALLE, params={"date_from": "2023-01-01", "date_to": "2023-12-31"}
    )
    assert fuera_de_cobertura.status_code == 422
    assert "2024-01-30" in fuera_de_cobertura.json()["detail"]

    invertido = client.get(DETALLE, params={"date_from": "2024-02-02", "date_to": "2024-02-01"})
    assert invertido.status_code == 422

    demasiados_valores = client.get(DETALLE, params=[("region", f"r{i}") for i in range(60)])
    assert demasiados_valores.status_code == 422


def test_filtro_invalido_devuelve_422(client):
    respuesta = client.get(DETALLE, params={"region": "Norte!"})
    assert respuesta.status_code == 422
    assert respuesta.json()["detail"][0]["loc"] == ["regions"]


def test_detalle_aplica_los_mismos_filtros_que_el_resumen(client):
    filtrado = detalle(client, region="norte", category="cat_a")
    assert filtrado["total"] == 2
    assert {item["linea_id"] for item in filtrado["items"]} == {"ln00000001", "ln00000005"}

    resumen = client.get(
        "/api/v1/dashboards/profitability/summary",
        params={"region": "norte", "category": "cat_a"},
    ).json()
    assert resumen["kpis"]["lineas"] == filtrado["total"]


def test_el_detalle_devuelve_los_campos_documentados(client):
    item = detalle(client, page_size=1)["items"][0]
    assert set(item) == {
        "linea_id",
        "pedido_id",
        "fecha",
        "cliente_id",
        "cliente",
        "region",
        "canal",
        "producto_id",
        "producto",
        "categoria",
        "cantidad",
        "precio_unitario_cent",
        "descuento_cent",
        "coste_unitario_cent",
        "venta_bruta_cent",
        "venta_neta_cent",
        "coste_cent",
        "margen_cent",
        "margen_pct",
        "moneda",
    }
    assert item["moneda"] == "MXN"


def test_exportacion_csv(client):
    respuesta = client.get(CSV_URL)
    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/csv")
    assert respuesta.headers["x-dataset-id"] == "test_ds"
    assert respuesta.headers["x-dataset-version"] == "1"
    assert respuesta.headers["x-total-filas"] == "6"
    assert 'attachment; filename="ventas_test_ds.csv"' == respuesta.headers["content-disposition"]

    texto = respuesta.content.decode("utf-8-sig")
    filas = list(csv.reader(io.StringIO(texto)))
    assert filas[0][:4] == ["linea_id", "pedido_id", "fecha", "cliente_id"]
    assert len(filas) == 7  # cabecera + 6 líneas

    importe = filas[1][filas[0].index("venta_neta_cent")]
    assert importe.lstrip("-").isdigit()


def test_exportacion_csv_respeta_los_filtros(client):
    respuesta = client.get(CSV_URL, params={"region": "norte"})
    assert respuesta.headers["x-total-filas"] == "3"
    filas = list(csv.reader(io.StringIO(respuesta.content.decode("utf-8-sig"))))
    assert len(filas) == 4
    regiones = {fila[filas[0].index("region")] for fila in filas[1:]}
    assert regiones == {"Norte"}


def test_exportacion_csv_protege_las_formulas(client, mongo_db):
    mongo_db[SALES_COLLECTION].insert_one(linea_peligrosa())

    respuesta = client.get(CSV_URL)
    assert respuesta.status_code == 200
    filas = list(csv.reader(io.StringIO(respuesta.content.decode("utf-8-sig"))))
    cabecera = filas[0]
    indice_cliente = cabecera.index("cliente")
    indice_producto = cabecera.index("producto")
    indice_region = cabecera.index("region")

    fila = next(fila for fila in filas[1:] if fila[cabecera.index("linea_id")] == "ln00000098")
    # Las celdas de texto que empiezan por = + - @ se neutralizan con un apóstrofo.
    assert fila[indice_cliente] == "'=SUM(A1:A9)"
    assert fila[indice_producto] == "'+Producto Falso"
    assert fila[indice_region] == "'@region-peligrosa"
    # Las celdas numéricas no se tocan.
    assert fila[cabecera.index("venta_neta_cent")].isdigit()


def test_exportacion_respeta_el_limite_de_filas(client):
    app = client.app
    ajustes = get_settings().model_copy(update={"max_export_rows": 3})
    app.dependency_overrides[get_settings] = lambda: ajustes
    try:
        respuesta = client.get(CSV_URL)
        assert respuesta.status_code == 413
        assert "máximo es 3" in respuesta.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_catalogo_de_datasets(client):
    listado = client.get("/api/v1/datasets")
    assert listado.status_code == 200
    assert len(listado.json()) == 1
    dataset = listado.json()[0]
    assert dataset["dataset_id"] == "test_ds"
    assert dataset["origen"] == "sintetico"
    assert dataset["moneda"] == "MXN"
    assert dataset["filas"] == 6
    assert dataset["periodo"] == {"desde": "2024-01-30", "hasta": "2024-02-02", "dias": 4}
    assert dataset["inmutable"] is True

    detalle_dataset = client.get("/api/v1/datasets/test_ds")
    assert detalle_dataset.status_code == 200
    assert detalle_dataset.json() == dataset

    assert client.get("/api/v1/datasets/no_existe").status_code == 404


def test_salud(client):
    vivo = client.get("/api/v1/health/live")
    assert vivo.status_code == 200
    assert vivo.json()["status"] == "ok"

    listo = client.get("/api/v1/health/ready")
    assert listo.status_code == 200
    assert listo.json() == {"status": "ready", "mongo": "ok", "version": "1.0.0"}


def test_la_api_no_expone_escrituras(client):
    for ruta in ("/api/v1/dashboards/profitability/sales", "/api/v1/datasets"):
        assert client.post(ruta, json={}).status_code == 405
        assert client.delete(ruta).status_code == 405


def test_documentacion_openapi_disponible(client):
    respuesta = client.get("/api/openapi.json")
    assert respuesta.status_code == 200
    rutas = respuesta.json()["paths"]
    assert "/api/v1/dashboards/profitability/summary" in rutas
    assert "/api/v1/dashboards/profitability/sales.csv" in rutas
