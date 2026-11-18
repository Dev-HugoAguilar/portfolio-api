"""Pruebas de integración del comparativo bancario con el archivo real de la CNBV.

Se usa el boletín descargado en ``backend/data/cnbv`` (fuera de Git). Si el
archivo no está presente, las pruebas se omiten con un mensaje explícito: nunca
se sustituye por datos sintéticos.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from app.cnbv import sources
from app.cnbv.importer import importar_boletin
from app.config import BACKEND_DIR
from app.db.indexes import ensure_indexes
from app.db.mongo import CNBV_BOLETINES, CNBV_FILAS, CNBV_HECHOS, CNBV_INDICADORES

pytestmark = pytest.mark.integracion

DIRECTORIO = BACKEND_DIR / "data" / "cnbv"
BASE_API = "/api/v1/cnbv/banca-multiple"

PERIODO_ACTUAL = "202410"
PERIODO_ANTERIOR = "202409"
PERIODO_ANIO_ANTERIOR = "202310"


def _archivo(periodo: str) -> Path:
    return DIRECTORIO / sources.PATRON_ARCHIVO.format(periodo=periodo)


@pytest.fixture(scope="module")
def archivos_disponibles() -> dict[str, Path]:
    disponibles = {
        periodo: _archivo(periodo)
        for periodo in (PERIODO_ACTUAL, PERIODO_ANTERIOR, PERIODO_ANIO_ANTERIOR)
    }
    faltantes = [periodo for periodo, ruta in disponibles.items() if not ruta.exists()]
    if faltantes:
        pytest.skip(
            "Faltan boletines reales de la CNBV en backend/data/cnbv "
            f"({', '.join(faltantes)}). Descárgalos con "
            "`python -m scripts.importar_cnbv --descargar`."
        )
    return disponibles


@pytest.fixture()
def cnbv_vacio(mongo_db, mongo_client, settings):
    """Deja las colecciones CNBV vacías (para probar ausencia de datos reales)."""
    db = mongo_client[settings.mongodb_db]
    for nombre in (CNBV_BOLETINES, CNBV_HECHOS, CNBV_INDICADORES, CNBV_FILAS):
        db[nombre].delete_many({})
    ensure_indexes(db)
    return db


@pytest.fixture(scope="module")
def cnbv_importado(mongo_client, archivos_disponibles, settings):
    """Importa los tres boletines reales una sola vez por módulo."""
    from app.db.mongo import close_database, init_database

    init_database(settings)
    db = mongo_client[settings.mongodb_db]
    for nombre in (CNBV_BOLETINES, CNBV_HECHOS, CNBV_INDICADORES, CNBV_FILAS):
        db[nombre].delete_many({})
    try:
        for periodo, ruta in archivos_disponibles.items():
            reporte = importar_boletin(
                db, ruta, periodo, url=sources.archivo_fuente(periodo).url
            )
            assert reporte.estado == "importado", reporte.resumen()
        yield db
    finally:
        for nombre in (CNBV_BOLETINES, CNBV_HECHOS, CNBV_INDICADORES, CNBV_FILAS):
            db[nombre].delete_many({})
        close_database()


@pytest.fixture()
def cliente(cnbv_importado, settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _celda(archivo: Path, concepto: str, institucion: str) -> float:
    """Lee un valor directamente del xlsx (camino independiente al importador)."""
    libro = openpyxl.load_workbook(archivo, read_only=True, data_only=True)
    hoja = libro["EF Resumen"]
    filas = list(
        hoja.iter_rows(min_row=1, max_row=min(hoja.max_row, 400), max_col=200, values_only=True)
    )
    libro.close()

    fila_cabecera = col_sistema = None
    for indice, fila in enumerate(filas):
        for columna, celda in enumerate(fila):
            if isinstance(celda, str) and celda.strip().lower() == "sistema":
                fila_cabecera, col_sistema = indice, columna
                break
        if fila_cabecera is not None:
            break

    col_concepto = col_sistema - 1
    columna_institucion = None
    for columna, celda in enumerate(filas[fila_cabecera]):
        if isinstance(celda, str) and celda.strip() == institucion:
            columna_institucion = columna
            break
    assert columna_institucion is not None, f"institución {institucion!r} no encontrada"

    for indice in range(fila_cabecera + 1, len(filas)):
        celda = filas[indice][col_concepto]
        if isinstance(celda, str) and celda.strip() == concepto:
            return float(filas[indice][columna_institucion])
    raise AssertionError(f"concepto {concepto!r} no encontrado")


def test_catalogo_reporta_la_fuente_real(cliente):
    respuesta = cliente.get(f"{BASE_API}/catalogo")
    assert respuesta.status_code == 200, respuesta.text
    datos = respuesta.json()

    assert datos["fuente"]["es_sintetico"] is False
    assert "CNBV" in datos["fuente"]["nombre"]
    assert datos["fuente"]["unidad_saldos"] == "millones_mxn"
    assert datos["fuente"]["atribucion"].startswith("Fuente: Comisión Nacional Bancaria")

    periodos = datos["periodos"]
    assert "2024-10" in periodos and "2023-10" in periodos
    assert datos["cobertura"]["hechos"] > 20_000
    assert datos["cobertura"]["filas_originales"] >= 150 * 3

    for boletin in datos["boletines"]:
        assert boletin["archivo"].startswith("BE_BM_")
        assert boletin["url"].startswith("https://portafolioinfo.cnbv.gob.mx/")
        assert len(boletin["sha256"]) == 64
        assert boletin["revision"] >= 1
        assert boletin["celdas_ausentes"] >= 0

    assert any(concepto["destacado"] for concepto in datos["conceptos"])
    assert len(datos["conceptos"]) > 100
    assert len(datos["instituciones"]) > 40


def test_resumen_concuerda_con_el_archivo_original(cliente, archivos_disponibles):
    respuesta = cliente.get(f"{BASE_API}/resumen", params={"periodo": "2024-10", "concepto": "activo#1"})
    assert respuesta.status_code == 200, respuesta.text
    datos = respuesta.json()

    esperado = _celda(archivos_disponibles[PERIODO_ACTUAL], "ACTIVO", "Sistema")
    assert datos["kpis"]["valor_sistema"] == pytest.approx(esperado, rel=1e-12)
    assert datos["kpis"]["unidad"] == "millones_mxn"
    assert datos["kpis"]["naturaleza"] == "saldo"
    assert datos["kpis"]["instituciones_con_dato"] >= 40

    # El agregado del sistema es la primera fila y vale 100 % de participación.
    primera = datos["por_institucion"][0]
    assert primera["es_sistema"] is True
    assert primera["participacion_pct"] == 100.0

    # Una institución concreta también cuadra contra la celda del archivo.
    bbva = next(fila for fila in datos["por_institucion"] if fila["institucion_id"] == "bbva_mexico")
    assert bbva["valor"] == pytest.approx(
        _celda(archivos_disponibles[PERIODO_ACTUAL], "ACTIVO", "BBVA México"), rel=1e-12
    )
    assert bbva["participacion_pct"] == pytest.approx(
        100.0 * bbva["valor"] / datos["kpis"]["valor_sistema"], abs=0.01
    )


def test_resumen_compara_saldos_con_el_periodo_anterior(cliente, archivos_disponibles):
    datos = cliente.get(
        f"{BASE_API}/resumen", params={"periodo": "2024-10", "concepto": "activo#1"}
    ).json()

    assert datos["comparacion"]["tipo"] == "periodo_anterior"
    assert datos["comparacion"]["periodo_comparado"] == "2024-09"
    assert datos["comparacion"]["motivo"] is None

    anterior = _celda(archivos_disponibles[PERIODO_ANTERIOR], "ACTIVO", "Sistema")
    assert datos["comparacion"]["valor_sistema_periodo_comparado"] == pytest.approx(anterior, rel=1e-12)
    assert datos["comparacion"]["variacion_sistema_pct"] == pytest.approx(
        100.0 * (datos["kpis"]["valor_sistema"] - anterior) / anterior, abs=0.01
    )


def test_resumen_compara_flujos_acumulados_contra_el_mismo_mes_del_anio(cliente):
    datos = cliente.get(
        f"{BASE_API}/resumen", params={"periodo": "2024-10", "concepto": "resultado_neto#1"}
    ).json()

    assert datos["kpis"]["naturaleza"] == "flujo_acumulado"
    assert datos["comparacion"]["tipo"] == "mismo_mes_anio_anterior"
    assert datos["comparacion"]["periodo_comparado"] == "2023-10"
    assert any("acumulado" in aviso for aviso in datos["advertencias"])


def test_serie_cubre_los_periodos_importados(cliente):
    datos = cliente.get(
        f"{BASE_API}/series",
        params=[("concepto", "cartera_de_credito#1"), ("institucion", "sistema")],
    ).json()

    assert datos["concepto"]["concepto_id"] == "cartera_de_credito#1"
    assert datos["periodos"] == ["2023-10", "2024-09", "2024-10"]
    serie = datos["series"][0]
    assert serie["es_sistema"] is True
    assert len(serie["puntos"]) == 3
    valores = [punto["valor"] for punto in serie["puntos"]]
    assert all(valor > 0 for valor in valores)


def test_detalle_pagina_y_conserva_la_fila_de_origen(cliente):
    datos = cliente.get(
        f"{BASE_API}/detalle",
        params={"periodo": "2024-10", "concepto": "activo#1", "page_size": 5},
    ).json()

    assert datos["total"] >= 40
    assert datos["pages"] == (datos["total"] + 4) // 5
    assert len(datos["items"]) == 5
    for item in datos["items"]:
        assert item["unidad"] == "millones_mxn"
        assert item["moneda"] == "MXN"
        assert item["archivo"] == "BE_BM_202410.xlsx"
        assert item["fila_origen"] >= 1


def test_resumen_y_detalle_son_coherentes(cliente):
    resumen = cliente.get(
        f"{BASE_API}/resumen", params={"periodo": "2024-10", "concepto": "cartera_de_credito#1"}
    ).json()
    detalle = cliente.get(
        f"{BASE_API}/detalle",
        params={"periodo": "2024-10", "concepto": "cartera_de_credito#1", "page_size": 200},
    ).json()

    sistema_detalle = next(item for item in detalle["items"] if item["es_sistema"])
    assert sistema_detalle["valor"] == pytest.approx(resumen["kpis"]["valor_sistema"], rel=1e-12)
    assert detalle["total"] == len(resumen["por_institucion"])


def test_indicadores_publicados_por_la_cnbv(cliente):
    datos = cliente.get(f"{BASE_API}/indicadores").json()
    assert datos["periodo_boletin"] == "2024-10"
    indicadores = {indicador["indicador_id"]: indicador for indicador in datos["indicadores"]}
    assert set(indicadores) == {"roa_flujo_12_meses", "roe_flujo_12_meses"}
    for indicador in indicadores.values():
        assert indicador["unidad"] == "porcentaje"
        assert indicador["valores"], "debería traer valores por institución"
        periodos = {valor["periodo"] for valor in indicador["valores"]}
        assert periodos == {"2023-10", "2024-09", "2024-10"}


def test_el_filtro_de_instituciones_no_altera_los_indicadores_del_sector(cliente):
    completo = cliente.get(
        f"{BASE_API}/resumen", params={"periodo": "2024-10", "concepto": "activo#1"}
    ).json()
    filtrado = cliente.get(
        f"{BASE_API}/resumen",
        params=[
            ("periodo", "2024-10"),
            ("concepto", "activo#1"),
            ("institucion", "bbva_mexico"),
            ("institucion", "santander"),
        ],
    ).json()

    assert filtrado["kpis"] == completo["kpis"]
    assert len(filtrado["por_institucion"]) == 3  # Sistema + 2 instituciones
    assert {fila["institucion_id"] for fila in filtrado["por_institucion"]} == {
        "sistema",
        "bbva_mexico",
        "santander",
    }
    # La posición es el ranking del sector, no el de la selección.
    bbva = next(fila for fila in filtrado["por_institucion"] if fila["institucion_id"] == "bbva_mexico")
    assert bbva["posicion"] == 1
    assert any("solo limita las filas" in aviso for aviso in filtrado["advertencias"])


def test_la_importacion_es_idempotente(cnbv_vacio, archivos_disponibles, settings):
    from app.db.mongo import close_database, init_database

    init_database(settings)
    try:
        ruta = archivos_disponibles[PERIODO_ACTUAL]
        primero = importar_boletin(cnbv_vacio, ruta, PERIODO_ACTUAL)
        total_primero = cnbv_vacio[CNBV_HECHOS].count_documents({"vigente": True})
        segundo = importar_boletin(cnbv_vacio, ruta, PERIODO_ACTUAL)

        assert primero.estado == "importado"
        assert segundo.estado == "sin_cambios"
        assert cnbv_vacio[CNBV_HECHOS].count_documents({"vigente": True}) == total_primero
        assert cnbv_vacio[CNBV_BOLETINES].count_documents({"vigente": True}) == 1
    finally:
        close_database()


def test_una_revision_nueva_no_sobrescribe_la_anterior(
    cnbv_vacio, archivos_disponibles, settings, tmp_path
):
    from app.db.mongo import close_database, init_database

    init_database(settings)
    try:
        ruta = archivos_disponibles[PERIODO_ACTUAL]
        importar_boletin(cnbv_vacio, ruta, PERIODO_ACTUAL)

        # Se simula que la CNBV republicó el boletín: mismo periodo, hash distinto.
        alterado = tmp_path / "BE_BM_202410.xlsx"
        alterado.write_bytes(ruta.read_bytes() + b"\0")
        reporte = importar_boletin(cnbv_vacio, alterado, PERIODO_ACTUAL)

        assert reporte.revision == 2
        assert any("hash distinto" in aviso for aviso in reporte.avisos)

        boletines = list(cnbv_vacio[CNBV_BOLETINES].find({"periodo": "2024-10"}).sort("revision", 1))
        assert [boletin["revision"] for boletin in boletines] == [1, 2]
        assert boletines[0]["vigente"] is False
        assert boletines[1]["vigente"] is True

        # Los hechos de la revisión anterior se conservan, pero no se sirven.
        assert cnbv_vacio[CNBV_HECHOS].count_documents({"periodo": "2024-10"}) > cnbv_vacio[
            CNBV_HECHOS
        ].count_documents({"periodo": "2024-10", "vigente": True})
    finally:
        close_database()


def test_sin_datos_reales_no_se_usa_el_dataset_sintetico(cnbv_vacio, sample_dataset, settings, mongo_client):
    """Sin boletines importados los endpoints fallan; no hay respaldo sintético."""
    from fastapi.testclient import TestClient

    from app.db.mongo import close_database, init_database
    from app.main import create_app

    init_database(settings)
    app = create_app(settings)
    try:
        with TestClient(app) as cliente:
            catalogo = cliente.get(f"{BASE_API}/catalogo")
            assert catalogo.status_code == 404
            assert "importar_cnbv" in catalogo.json()["detail"]

            resumen = cliente.get(f"{BASE_API}/resumen")
            assert resumen.status_code == 404

            # El catálogo sintético sigue existiendo, pero es otro dataset.
            datasets = cliente.get("/api/v1/datasets").json()
            assert all(dataset["origen"] == "sintetico" for dataset in datasets)
    finally:
        close_database()
