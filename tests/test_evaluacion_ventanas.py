"""Pruebas de la tabla completa de evaluación a 28 días (dos granos y segmentos)."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db.mongo import (
    M5_EVALUACION_MODELOS,
    M5_MODELOS,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
)
from app.comercial import ingesta
from app.main import create_app
from tests import m5_fixture
from tests.test_exploracion_m5 import COLECCIONES

RUN = "run-calendario-eval0001"
HORIZONTE = 28


@pytest.fixture()
def cliente_base(mongo_db, tmp_path):
    """Ventas sintéticas importadas y cliente HTTP con la base limpia."""
    for nombre in COLECCIONES:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})
    m5_fixture.escribir(tmp_path)
    manifiesto = ingesta.importar(mongo_db, tmp_path)
    app = create_app(__import__("app.config", fromlist=["get_settings"]).get_settings())
    with TestClient(app) as cliente_http:
        yield {"cliente": cliente_http, "db": mongo_db, "version": manifiesto["version"], "carpeta": tmp_path}
    for nombre in COLECCIONES:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})


def _metricas(n: int, mae: float, sesgo: float, wape: float, denominador: int) -> dict:
    return {"n": n, "mae": mae, "sesgo": sesgo, "wape": wape, "denominador": denominador}


@pytest.fixture()
def evaluacion_completa(cliente_base):  # noqa: F811 - reutiliza la base sintética
    db, version = cliente_base["db"], cliente_base["version"]
    for nombre in (M5_MODELOS, M5_EVALUACION_MODELOS, M5_PREDICCIONES, M5_PREDICCIONES_SERIES):
        db[nombre].delete_many({})
    db[M5_MODELOS].insert_one(
        {
            "_id": RUN,
            "run_id": RUN,
            "esquema": "m5-modelo-1",
            "version": version,
            "variante": "calendario",
            "etiqueta": "Rezagos + calendario",
            "metodo": "LightGBM global condicionado al horizonte",
            "estado": "publicada",
            "recomendado": True,
            "horizonte": HORIZONTE,
            "variables": ["h", "lag_1"],
            "origenes_entrenamiento": [42, 70],
            "ventana_entrenamiento": {"hasta": 70},
            "cobertura": {"series": 9, "series_modelo": 9, "fallback": 0,
                          "cobertura_candidato_pct": 100.0, "cobertura_sistema_pct": 100.0},
            "supuestos": {"precio_futuro": "último precio conocido"},
            "creado_en": datetime(2011, 4, 1, 12, 0),
        }
    )

    # Dos ventanas con observaciones: 42 (desarrollo) y 70 (evaluación final).
    for origen, etapa, mae_cand, mae_base in ((70, "final", 1.0, 2.0),):
        db[M5_EVALUACION_MODELOS].insert_one(
            {
                "_id": f"{RUN}:{etapa}:{origen}",
                "run_id": RUN,
                "version": version,
                "etapa": etapa,
                "origen_d": origen,
                "origen_fecha": "2011-04-09",
                "fin_fecha": "2011-05-06",
                "series": 9,
                "observaciones": 9 * HORIZONTE,
                "candidato": _metricas(9 * HORIZONTE, mae_cand, 0.1, 10.0, 2520),
                "baseline": _metricas(9 * HORIZONTE, mae_base, -0.2, 20.0, 2520),
                "mejora_pct": 50.0,
                "por_horizonte": [
                    {
                        "horizonte": paso,
                        "fecha": f"2011-04-{paso:02d}",
                        "candidato": _metricas(9, mae_cand, 0.1, 10.0, 90),
                        "baseline": _metricas(9, mae_base, -0.2, 20.0, 90),
                    }
                    for paso in range(1, HORIZONTE + 1)
                ],
                "segmentos": [
                    {
                        "nivel": "estado",
                        "clave": "CA",
                        "series": 9,
                        "candidato": _metricas(9 * HORIZONTE, mae_cand, 0.1, 10.0, 2520),
                        "baseline": _metricas(9 * HORIZONTE, mae_base, -0.2, 20.0, 2520),
                        "mejora_pct": 50.0,
                        "agregado": {
                            "candidato": _metricas(HORIZONTE, 3.0, 0.5, 5.0, 2520),
                            "baseline": _metricas(HORIZONTE, 6.0, -0.5, 10.0, 2520),
                            "mejora_pct": 50.0,
                        },
                    },
                    {
                        "nivel": "categoria",
                        "clave": "FOODS",
                        "series": 6,
                        "candidato": _metricas(6 * HORIZONTE, 0.8, 0.05, 8.0, 1680),
                        "baseline": _metricas(6 * HORIZONTE, 1.6, -0.1, 16.0, 1680),
                        "mejora_pct": 50.0,
                        "agregado": {
                            "candidato": _metricas(HORIZONTE, 2.0, 0.2, 4.0, 1680),
                            "baseline": _metricas(HORIZONTE, 4.0, -0.2, 8.0, 1680),
                            "mejora_pct": 50.0,
                        },
                    },
                    {
                        "nivel": "categoria",
                        "clave": "HOBBIES",
                        "series": 3,
                        "candidato": _metricas(3 * HORIZONTE, 1.4, 0.2, 14.0, 840),
                        "baseline": _metricas(3 * HORIZONTE, 1.2, -0.3, 12.0, 840),
                        "mejora_pct": -16.67,
                        "agregado": {
                            "candidato": _metricas(HORIZONTE, 1.0, 0.1, 2.0, 840),
                            "baseline": _metricas(HORIZONTE, 0.8, -0.1, 1.6, 840),
                            "mejora_pct": -25.0,
                        },
                    },
                ],
                "segmentos_mejoran": 2,
                "segmentos_empeoran": 1,
            }
        )

    # Predicciones agregadas del total: 10 por día del candidato y 20 del baseline,
    # observado 11 → MAE candidato 1, baseline 9.
    db[M5_PREDICCIONES].insert_one(
        {
            "_id": f"{RUN}:70:total:total",
            "run_id": RUN,
            "version": version,
            "tipo": "evaluacion",
            "origen_d": 70,
            "origen_fecha": "2011-04-09",
            "dimension": "total",
            "clave": "total",
            "dias": [{"d": 70 + paso, "fecha": f"2011-04-{paso:02d}"} for paso in range(1, 29)],
            "pronostico": [10] * HORIZONTE,
            "baseline": [20] * HORIZONTE,
            "observado": [11] * HORIZONTE,
            "unidad": "unidades vendidas",
        }
    )
    # Vectores por serie del mismo origen: la suma debe dar el agregado publicado.
    for indice in range(9):
        db[M5_PREDICCIONES_SERIES].insert_one(
            {
                "_id": f"{RUN}:70:item_{indice}:CA_1",
                "run_id": RUN,
                "version": version,
                "origen_d": 70,
                "store_id": "CA_1",
                "item_id": f"item_{indice}",
                "pronostico": [1 if indice < 1 else 1] * HORIZONTE,
                "baseline": [2 if indice < 2 else 2] * HORIZONTE,
            }
        )
    # 9 series × 1 = 9 por día; el agregado dice 10: se fuerza una diferencia comprobable.
    return cliente_base


def test_ventanas_tienen_dos_granos_y_wape_en_razon(evaluacion_completa):
    datos = evaluacion_completa["cliente"].get(
        "/api/v1/comercial/m5/evaluacion-ventanas", params={"etapa": "final"}
    ).json()
    assert datos["run_id"] == RUN
    assert datos["granos"] == ["detalle artículo × tienda", "total del segmento"]
    assert len(datos["ventanas"]) == 1
    ventana = datos["ventanas"][0]
    assert ventana["origen_d"] == 70
    assert ventana["dias"] == HORIZONTE
    assert ventana["etapa"] == "final"
    detalle = ventana["detalle"]
    assert detalle["grano"] == "detalle artículo × tienda"
    assert detalle["candidato"]["mae"] == 1.0
    assert detalle["baseline"]["mae"] == 2.0
    assert detalle["diferencia_absoluta"] == pytest.approx(1.0)
    assert detalle["mejora_relativa_pct"] == pytest.approx(50.0)
    assert detalle["candidato"]["wape_razon"] == pytest.approx(0.1)
    assert detalle["candidato"]["wape_pct"] == pytest.approx(10.0)
    # Grano agregado: se suma por día antes de medir (10 · 20 · observado 11).
    agregado = ventana["agregado"]
    assert agregado["grano"] == "total del segmento"
    assert agregado["candidato"]["n"] == HORIZONTE
    assert agregado["candidato"]["mae"] == pytest.approx(1.0)
    assert agregado["baseline"]["mae"] == pytest.approx(9.0)
    assert agregado["diferencia_absoluta"] == pytest.approx(8.0)
    assert datos["formulas"]["unidades"]
    assert datos["formulas"]["compensacion"]


def test_ventanas_traen_segmentos_y_consolidado(evaluacion_completa):
    datos = evaluacion_completa["cliente"].get(
        "/api/v1/comercial/m5/evaluacion-ventanas", params={"etapa": "final"}
    ).json()
    assert [segmento["clave"] for segmento in datos["por_estado"]] == ["CA"]
    assert [segmento["clave"] for segmento in datos["por_categoria"]] == ["FOODS", "HOBBIES"]
    hobbies = next(s for s in datos["por_categoria"] if s["clave"] == "HOBBIES")
    # El candidato empeora en HOBBIES y se publica tal cual (mejora negativa).
    assert hobbies["detalle"]["mejora_relativa_pct"] < 0
    assert hobbies["agregado"]["mejora_relativa_pct"] < 0
    consolidado = datos["consolidado"]["detalle"]
    assert consolidado["candidato"]["n"] == 9 * HORIZONTE
    assert consolidado["candidato"]["mae"] == pytest.approx(1.0)
    assert consolidado["diferencia_absoluta"] == pytest.approx(1.0)


def test_ventanas_declaran_reconciliacion_y_cobertura(evaluacion_completa):
    datos = evaluacion_completa["cliente"].get(
        "/api/v1/comercial/m5/evaluacion-ventanas", params={"etapa": "final"}
    ).json()
    reconciliacion = datos["reconciliacion"]
    assert reconciliacion["origen_d"] == 70
    # 9 series × 1 unidad contra un agregado de 10: no cuadra y se declara.
    assert reconciliacion["cuadra"] is False
    assert reconciliacion["max_diferencia_absoluta"] == pytest.approx(1.0)
    assert datos["cobertura_modelo"]["fallback"] == 0
    assert any("WRMSSE" in advertencia for advertencia in datos["advertencias"])
    assert any("significancia" in advertencia for advertencia in datos["advertencias"])


def test_ventanas_sin_observaciones_no_inventan_metricas(evaluacion_completa):
    """El origen final (sin observado) declara que es pronóstico, no evaluación."""
    db, version = evaluacion_completa["db"], evaluacion_completa["version"]
    db[M5_PREDICCIONES].insert_one(
        {
            "_id": f"{RUN}:98:total:total",
            "run_id": RUN,
            "version": version,
            "tipo": "final",
            "origen_d": 98,
            "origen_fecha": "2011-05-07",
            "dimension": "total",
            "clave": "total",
            "dias": [{"d": 98 + paso, "fecha": f"2011-05-{paso:02d}"} for paso in range(1, 29)],
            "pronostico": [10] * HORIZONTE,
            "baseline": [20] * HORIZONTE,
            "observado": None,
            "unidad": "unidades vendidas",
        }
    )
    from app.repositories.comercial_repository import obtener_repositorio
    from app.services.comercial_service import ComercialServicio

    servicio = ComercialServicio(obtener_repositorio(db))
    grano = __import__("app.services.comercial_service", fromlist=["_grano_agregado"])._grano_agregado(
        obtener_repositorio(db), version, RUN, 98
    )
    assert grano.candidato.n == 0
    assert grano.candidato.mae is None
    assert "pronóstico" in (grano.motivo or "")
    assert servicio  # el servicio existe y no inventa métricas para ese origen
