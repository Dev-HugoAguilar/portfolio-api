"""Pruebas del motor predictivo (precios, variables, métricas y contrato HTTP).

Las ventas son **sintéticas** (``tests/m5_fixture.py``). Las pruebas del modelo usan
arreglos pequeños y un ``booster`` de prueba: no entrenan LightGBM, que se ejecuta en
``scripts/entrenar_m5.py`` sobre el universo real de M5.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.comercial import ingesta, modelo, precios
from app.db.mongo import (
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_CALENDARIO,
    M5_CATALOGO,
    M5_DERIVADOS,
    M5_EVALUACION,
    M5_EVALUACION_MODELOS,
    M5_IMPORTACIONES,
    M5_MODELOS,
    M5_PRECIOS,
    M5_PRECIOS_MANIFEST,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PRONOSTICOS,
)
from app.main import create_app
from tests import m5_fixture

COLECCIONES_MODELO = (
    M5_PRECIOS,
    M5_PRECIOS_MANIFEST,
    M5_MODELOS,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_EVALUACION_MODELOS,
)
COLECCIONES_M5 = (
    M5_IMPORTACIONES,
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_CATALOGO,
    M5_CALENDARIO,
    M5_PRONOSTICOS,
    M5_EVALUACION,
    M5_DERIVADOS,
) + COLECCIONES_MODELO


@pytest.fixture()
def ventas(mongo_db, tmp_path):
    """Ventas sintéticas importadas como versión publicada."""
    for nombre in COLECCIONES_M5:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})
    m5_fixture.escribir(tmp_path)
    manifiesto = ingesta.importar(mongo_db, tmp_path)
    yield {"db": mongo_db, "version": manifiesto["version"], "carpeta": tmp_path}
    for nombre in COLECCIONES_M5:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})


# --------------------------------------------------------------------------- #
# Precios
# --------------------------------------------------------------------------- #


def _escribir_precios(carpeta: Path, filas: list[tuple[str, str, int, str]]) -> Path:
    ruta = carpeta / "sell_prices.csv"
    ruta.write_text(
        "store_id,item_id,wm_yr_wk,sell_price\n"
        + "\n".join(",".join(str(valor) for valor in fila) for fila in filas)
        + "\n",
        encoding="utf-8",
    )
    return ruta


def _semanas(db) -> list[int]:
    """Semanas distintas del calendario, en orden."""
    return sorted({int(d["wm_yr_wk"]) for d in db[M5_CALENDARIO].find({}, {"wm_yr_wk": 1})})


def _primer_item_y_tienda(db, version: str) -> tuple[str, str]:
    producto = db[M5_CATALOGO].find_one({"version": version, "tipo": "producto"})
    tienda = db[M5_CATALOGO].find_one({"version": version, "tipo": "tienda"})
    return producto["item_id"], tienda["store_id"]


def test_precios_rechazan_clave_repetida(ventas, tmp_path):
    db = ventas["db"]
    semana = _semanas(db)[0]
    _escribir_precios(
        tmp_path,
        [("CA_1", "FOODS_1_001", semana, "1.50"), ("CA_1", "FOODS_1_001", semana, "1.60")],
    )
    datos = precios.leer_precios(tmp_path)
    with pytest.raises(precios.ErrorPrecios, match="repetidas"):
        precios.validar_precios(db, ventas["version"], datos)


@pytest.mark.parametrize("valor", ["0", "-1.2", "abc"])
def test_precios_rechazan_valor_no_admisible(ventas, tmp_path, valor):
    db = ventas["db"]
    _escribir_precios(tmp_path, [("CA_1", "FOODS_1_001", _semanas(db)[0], valor)])
    with pytest.raises(precios.ErrorPrecios):
        precios.leer_precios(tmp_path)


def test_precios_rechazan_semanas_ajenas_al_calendario(ventas, tmp_path):
    db = ventas["db"]
    _escribir_precios(tmp_path, [("CA_1", "FOODS_1_001", 99999, "2.00")])
    datos = precios.leer_precios(tmp_path)
    with pytest.raises(precios.ErrorPrecios, match="calendario"):
        precios.validar_precios(db, ventas["version"], datos)


def test_precios_no_exigen_todo_el_catalogo(ventas, tmp_path):
    """Un artículo sin precio no es un error: el precio se conserva ausente."""
    db, version = ventas["db"], ventas["version"]
    item, tienda = _primer_item_y_tienda(db, version)
    catalogo = db[M5_CATALOGO].count_documents({"version": version, "tipo": "producto"})
    assert catalogo > 1, "la prueba necesita más de un artículo en el catálogo"
    _escribir_precios(tmp_path, [(tienda, item, _semanas(db)[0], "1.50")])
    manifiesto = precios.importar_precios(db, version, tmp_path)
    validaciones = manifiesto["validaciones"]
    assert validaciones["items_con_precio"] == 1
    assert validaciones["pares_de_la_rejilla"] == 1, "la rejilla solo cuenta lo que trae el archivo"
    assert validaciones["items_desconocidos"] == []


def test_precios_importacion_idempotente_y_ausente_no_es_cero(ventas, tmp_path):
    db, version = ventas["db"], ventas["version"]
    item, tienda = _primer_item_y_tienda(db, version)
    otras = [d["item_id"] for d in db[M5_CATALOGO].find({"version": version, "tipo": "producto"})]
    sin_precio = next(codigo for codigo in sorted(otras) if codigo != item)
    semanas = _semanas(db)
    _escribir_precios(
        tmp_path,
        [
            (tienda, item, semanas[0], "1.50"),
            (tienda, item, semanas[5], "1.75"),
            # Este segundo artículo solo tiene precio en la última semana: la primera
            # semana debe quedar como ausencia, nunca como cero.
            (tienda, sin_precio, semanas[5], "9.90"),
        ],
    )
    primero = precios.importar_precios(db, version, tmp_path)
    assert primero["reutilizado"] is False
    assert primero["documentos"] == 2, "un documento por (tienda, semana)"
    segundo = precios.importar_precios(db, version, tmp_path)
    assert segundo["reutilizado"] is True
    assert db[M5_PRECIOS].count_documents({"version_precios": primero["version_precios"]}) == 2

    cargados = precios.cargar_precios(db, primero["version_precios"])
    assert sorted(cargados["items"]) == sorted([item, sin_precio])
    assert cargados["matriz"].shape == (2, 2, 1)
    indice_sin = cargados["items"].index(sin_precio)
    assert np.isnan(cargados["matriz"][0, indice_sin, 0]), "una semana sin precio es ausencia"
    assert cargados["matriz"][1, indice_sin, 0] == pytest.approx(9.90)
    con_precio = cargados["matriz"][:, cargados["items"].index(item), 0]
    assert np.nansum(con_precio) == pytest.approx(3.25)
    assert not np.isnan(con_precio).any()


def test_precios_no_duplican_ventas(ventas, tmp_path):
    """El cruce de precios es por tienda y semana: las ventas no cambian."""
    db, version = ventas["db"], ventas["version"]
    item, tienda = _primer_item_y_tienda(db, version)
    antes = {d["_id"]: d["total"] for d in db[M5_BLOQUES].find({}, {"total": 1})}
    agregados_antes = {d["_id"]: d["unidades"] for d in db[M5_AGREGADOS].find({}, {"unidades": 1})}
    _escribir_precios(tmp_path, [(tienda, item, _semanas(db)[0], "2.10")])
    precios.importar_precios(db, version, tmp_path)
    despues = {d["_id"]: d["total"] for d in db[M5_BLOQUES].find({}, {"total": 1})}
    assert despues == antes
    assert {d["_id"]: d["unidades"] for d in db[M5_AGREGADOS].find({}, {"unidades": 1})} == agregados_antes


# --------------------------------------------------------------------------- #
# Contexto sintético y variables
# --------------------------------------------------------------------------- #

N_DIAS = 480
ORIGEN = 430


def _contexto(n_series: int = 6, n_dias: int = N_DIAS) -> modelo.Contexto:
    generador = np.random.default_rng(7)
    ventas = generador.integers(0, 5, size=(n_series, n_dias)).astype(np.int32)
    ventas[:, ::7] += 3
    tiendas = ["CA_1", "CA_2"]
    categorias = ["FOODS", "HOBBIES"]
    departamentos = ["FOODS_1", "HOBBIES_1"]
    dias = np.arange(n_dias)
    calendario = {
        "d": (dias + 1).astype(np.int32),
        "dow": ((dias % 7) + 1).astype(np.int8),
        "month": ((dias // 30) % 12 + 1).astype(np.int8),
        "day": (dias % 28 + 1).astype(np.int8),
        "doy": (dias % 365 + 1).astype(np.int16),
        "week": (11100 + dias // 7).astype(np.int32),
        "snap_ca": (dias % 10 == 0).astype(np.int8),
        "snap_tx": np.zeros(n_dias, dtype=np.int8),
        "snap_wi": np.zeros(n_dias, dtype=np.int8),
        "evento_1": (dias % 40 == 0).astype(np.int8),
        "evento_2": np.zeros(n_dias, dtype=np.int8),
        "tipo_1": np.zeros(n_dias, dtype=np.int8),
        "tipo_2": np.zeros(n_dias, dtype=np.int8),
        "semana_anio": (dias // 7 % 52 + 1).astype(np.int8),
        "fin_de_semana": ((dias % 7) >= 5).astype(np.int8),
        "desde_inicio": dias.astype(np.int32),
    }
    semanas = sorted(set(calendario["week"].tolist()))
    posicion = {semana: indice for indice, semana in enumerate(semanas)}
    semana_por_dia = np.array([posicion[int(s)] for s in calendario["week"]], dtype=np.int32)
    precios_serie = np.full((n_series, len(semanas)), 2.0, dtype=np.float32)
    precios_serie[:, ::13] += 0.5
    return modelo.Contexto(
        version="test",
        version_precios="test",
        series=[(f"item_{i}", tiendas[i % 2]) for i in range(n_series)],
        item_idx=np.arange(n_series, dtype=np.int32),
        store_idx=(np.arange(n_series) % 2).astype(np.int32),
        cat_idx=(np.arange(n_series) % 2).astype(np.int8),
        dept_idx=(np.arange(n_series) % 2).astype(np.int8),
        state_idx=np.zeros(n_series, dtype=np.int8),
        ventas=ventas,
        calendario=calendario,
        semana_por_dia=semana_por_dia,
        semanas=semanas,
        precios_serie=precios_serie,
        precios_rellenados=modelo._rellenar_causal(precios_serie),
        fecha_de_dia=[date(2013, 1, 1) + timedelta(days=int(d)) for d in dias],
        nombres={
            "items": [f"item_{i}" for i in range(n_series)],
            "tiendas": tiendas,
            "estados": ["CA"],
            "categorias": categorias,
            "departamentos": departamentos,
            "tipos_evento": ["", "Sporting", "Cultural", "National", "Religious"],
        },
    )


def test_variables_no_usan_ventas_futuras():
    """Cambiar las ventas posteriores al origen no altera ninguna variable."""
    original, nombres = modelo.construir_matriz(_contexto(), ORIGEN, con_precios=True)
    alterado = _contexto()
    alterado.ventas[:, ORIGEN:] = 999
    modificado, _ = modelo.construir_matriz(alterado, ORIGEN, con_precios=True)
    assert np.array_equal(original, modificado, equal_nan=True)
    assert len(nombres) == len(modelo.FEATURES_BASE) + len(modelo.FEATURES_PRECIO)


def test_variables_de_ventas_corresponden_al_pasado_observado():
    ctx = _contexto()
    _, nombres = modelo.construir_matriz(ctx, ORIGEN, con_precios=False)
    matriz, _ = modelo.construir_matriz(ctx, ORIGEN, con_precios=False)
    serie = 3
    fila = serie * modelo.HORIZONTE
    for rezago in (1, 7, 28, 364):
        columna = nombres.index(f"lag_{rezago}")
        assert matriz[fila, columna] == pytest.approx(ctx.ventas[serie, ORIGEN - rezago])
    columna = nombres.index("media_28")
    assert matriz[fila, columna] == pytest.approx(ctx.ventas[serie, ORIGEN - 28 : ORIGEN].mean())
    # El rezago anual solo existe con más de un año de historia.
    assert nombres.index("lag_364") >= 0


def test_variables_de_precio_ignoran_semanas_posteriores():
    """El precio del día objetivo es el último conocido, nunca el realizado."""
    indice = modelo._features(True).index("precio_supuesto")
    original, _ = modelo.construir_matriz(_contexto(), ORIGEN, con_precios=True)
    alterado = _contexto()
    desde = int(alterado.semana_por_dia[ORIGEN - 1]) + 1
    alterado.precios_serie[:, desde:] += 10  # solo semanas posteriores al origen
    alterado.precios_rellenados = modelo._rellenar_causal(alterado.precios_serie)
    modificado, _ = modelo.construir_matriz(alterado, ORIGEN, con_precios=True)
    assert np.array_equal(original[:, indice], modificado[:, indice])


def test_vectores_de_28_dias_y_validacion_del_origen():
    ctx = _contexto()
    matriz, nombres = modelo.construir_matriz(ctx, ORIGEN, con_precios=False)
    assert matriz.shape == (ctx.n_series * modelo.HORIZONTE, len(nombres))
    horizontes = matriz[:, nombres.index("h")]
    assert horizontes.min() == 1 and horizontes.max() == modelo.HORIZONTE
    with pytest.raises(ValueError, match="Origen fuera de rango"):
        modelo.construir_matriz(ctx, modelo.MINIMO_HISTORIA - 1, con_precios=False)
    with pytest.raises(ValueError, match="Origen fuera de rango"):
        modelo.construir_matriz(ctx, ctx.n_dias + 1, con_precios=False)
    with pytest.raises(ValueError, match="calendario"):
        modelo.construir_matriz(_contexto(n_dias=ORIGEN + 10), ORIGEN, con_precios=False)


def test_baseline_comparable_y_sin_futuro():
    """El baseline del modelo coincide con el estacional ya publicado."""
    from app.comercial import forecast

    ctx = _contexto()
    nuestro = modelo.predecir_baseline(ctx, ORIGEN)
    for fila in range(ctx.n_series):
        anterior = forecast.predecir(list(ctx.ventas[fila]), ORIGEN, modelo.HORIZONTE)
        assert list(nuestro[fila].astype(int)) == anterior
    alterado = _contexto()
    alterado.ventas[:, ORIGEN:] = 12345
    assert np.array_equal(modelo.predecir_baseline(alterado, ORIGEN), nuestro)


# --------------------------------------------------------------------------- #
# Métricas, agregados y fallback
# --------------------------------------------------------------------------- #


def test_metricas_denominadores_y_wape_no_calculable():
    error = np.array([[1.0, -2.0], [3.0, 0.0]])
    observado = np.array([[2.0, 2.0], [4.0, 0.0]])
    datos = modelo.metricas(error, observado)
    assert datos["n"] == 4
    assert datos["mae"] == pytest.approx(1.5)
    assert datos["sesgo"] == pytest.approx(0.5)
    assert datos["wape"] == pytest.approx(100 * 6 / 8)
    assert datos["denominador"] == 8

    vacio = modelo.metricas(np.zeros((1, 3)), np.zeros((1, 3)))
    assert vacio["wape"] is None
    assert vacio["denominador_cero"] is True


def test_agregar_metricas_suma_errores_y_no_promedia_porcentajes():
    a = {"n": 10, "mae": 1.0, "sesgo": 0.5, "wape": 10.0, "denominador": 100}
    b = {"n": 10, "mae": 3.0, "sesgo": -0.5, "wape": 60.0, "denominador": 50}
    total = modelo.agregar_metricas_guardadas([a, b])
    assert total["n"] == 20
    assert total["mae"] == pytest.approx(2.0)
    assert total["sesgo"] == pytest.approx(0.0)
    assert total["wape"] == pytest.approx(100 * 40 / 150, abs=1e-3)
    assert total["wape"] != pytest.approx((10 + 60) / 2)
    assert modelo.agregar_metricas_guardadas([])["wape"] is None


def test_agregados_reconcilian_con_el_detalle():
    ctx = _contexto()
    valores = np.arange(ctx.n_series * modelo.HORIZONTE, dtype=float).reshape(
        ctx.n_series, modelo.HORIZONTE
    )
    agregados = modelo.agregar_por_dimension(ctx, valores)
    total = agregados["total"]["total"]
    assert np.allclose(total, valores.sum(axis=0))
    for dimension in ("estado", "tienda", "categoria", "departamento", "categoria_tienda", "departamento_tienda"):
        assert np.allclose(sum(agregados[dimension].values()), total), dimension


class _BoosterNulo:
    """Booster de prueba que devuelve NaN en la primera serie."""

    def __init__(self) -> None:
        self.forma = None

    def predict(self, x, num_threads=8):  # noqa: ANN001, ARG002
        self.forma = x.shape
        salida = np.zeros(x.shape[0], dtype=float)
        salida[0] = np.nan
        return salida


def test_fallback_explicito_al_baseline(monkeypatch):
    ctx = _contexto()
    baseline = modelo.predecir_baseline(ctx, ORIGEN)
    monkeypatch.setattr(
        modelo,
        "construir_matriz",
        lambda *args, **kwargs: (np.zeros((ctx.n_series * modelo.HORIZONTE, 1)), ["h"]),
    )
    prediccion, informe = modelo.predecir(
        ctx, {"booster": _BoosterNulo(), "variables": ["h"]}, ORIGEN, baseline=baseline
    )
    assert informe["fallback"] == 1
    assert informe["series_modelo"] == ctx.n_series - 1
    assert informe["cobertura_candidato_pct"] < 100
    assert informe["motivo_fallback"]
    assert np.allclose(prediccion[0], baseline[0]), "la serie con error usa el baseline"
    assert np.allclose(prediccion[1:], 0.0), "las demás series conservan la predicción"


def test_prediccion_invariante_a_ventas_posteriores_al_origen():
    """Con el modelo entrenado y fijo, alterar el futuro no cambia la predicción emitida.

    Es la prueba de ausencia de fuga en el punto de inferencia: si alguna variable usara
    ventas posteriores al origen, la predicción cambiaría.
    """
    ctx = _contexto()
    datos = modelo.entrenar(
        ctx,
        [ORIGEN - modelo.HORIZONTE, ORIGEN],
        con_precios=False,
        fraccion_ceros=1.0,
        num_boost_round=15,
    )
    baseline = modelo.predecir_baseline(ctx, ORIGEN)
    primera, informe_primero = modelo.predecir(ctx, datos, ORIGEN, baseline=baseline)

    alterado = _contexto()
    alterado.ventas[:, ORIGEN:] = 999  # solo días posteriores al origen
    segunda, informe_segundo = modelo.predecir(alterado, datos, ORIGEN, baseline=baseline)

    assert np.array_equal(primera, segunda), "la predicción no puede depender de ventas futuras"
    assert informe_primero == informe_segundo


def test_prediccion_invariante_a_precios_posteriores_al_origen():
    """El precio futuro es un supuesto: cambiar semanas posteriores no altera la predicción."""
    ctx = _contexto()
    datos = modelo.entrenar(
        ctx,
        [ORIGEN - modelo.HORIZONTE],
        con_precios=True,
        fraccion_ceros=1.0,
        num_boost_round=10,
    )
    primera, _ = modelo.predecir(ctx, datos, ORIGEN)

    alterado = _contexto()
    desde = int(alterado.semana_por_dia[ORIGEN - 1]) + 1  # semanas posteriores al origen
    alterado.precios_serie[:, desde:] *= 3.0
    alterado.precios_rellenados = modelo._rellenar_causal(alterado.precios_serie)
    segunda, _ = modelo.predecir(alterado, datos, ORIGEN)

    assert np.array_equal(primera, segunda), "la predicción no puede usar precios posteriores"


def test_entrenamiento_termina_antes_de_los_objetivos_evaluados():
    """Las filas de entrenamiento acaban en el origen y no invaden la ventana evaluada."""
    ctx = _contexto()
    datos = modelo.entrenar(
        ctx, [ORIGEN - modelo.HORIZONTE], con_precios=False, fraccion_ceros=1.0, num_boost_round=5
    )
    assert datos["origenes_entrenamiento"] == [ORIGEN - modelo.HORIZONTE]
    # La ventana de entrenamiento cubre 28 días después de su origen: el último día usado
    # es ORIGEN − 1, justo antes de la primera fecha objetivo evaluada desde ORIGEN.
    ultimo_dia_entrenado = datos["origenes_entrenamiento"][0] + modelo.HORIZONTE - 1
    assert ultimo_dia_entrenado == ORIGEN - 1


def test_evaluacion_compara_mismas_series_fechas_y_segmentos():
    ctx = _contexto()
    observado = ctx.ventas[:, ORIGEN : ORIGEN + modelo.HORIZONTE].astype(np.float32)
    candidato = observado + 1.0
    baseline = observado + 2.0
    resultado = modelo.evaluar_origen(
        ctx,
        origen=ORIGEN,
        prediccion=candidato,
        baseline=baseline,
        etiquetas=modelo.etiquetas_serie(ctx),
    )
    assert resultado["candidato"]["n"] == resultado["baseline"]["n"] == observado.size
    assert resultado["candidato"]["mae"] == pytest.approx(1.0)
    assert resultado["baseline"]["mae"] == pytest.approx(2.0)
    assert resultado["candidato"]["sesgo"] == pytest.approx(1.0)
    assert resultado["mejora_pct"] == pytest.approx(50.0)
    assert resultado["mejora_mae_pct"] == pytest.approx(50.0)
    assert len(resultado["por_horizonte"]) == modelo.HORIZONTE
    assert resultado["por_horizonte"][0]["horizonte"] == 1
    assert {segmento["nivel"] for segmento in resultado["segmentos"]} == {
        "estado",
        "tienda",
        "categoria",
        "departamento",
    }
    tienda = next(s for s in resultado["segmentos"] if s["nivel"] == "tienda")
    # El segmento reúne sus series: el error de la serie agregada (suma por día antes
    # de medir) crece con el número de series y no es el MAE por serie.
    assert tienda["series"] == 3
    assert tienda["candidato"]["mae"] == pytest.approx(1.0)
    assert tienda["agregado"]["candidato"]["n"] == modelo.HORIZONTE
    assert tienda["agregado"]["candidato"]["mae"] == pytest.approx(3.0)
    assert tienda["agregado"]["candidato"]["mae"] != pytest.approx(tienda["candidato"]["mae"])
    assert resultado["segmentos_mejoran"] + resultado["segmentos_empeoran"] <= len(
        resultado["segmentos"]
    )


def test_firma_y_run_id_reproducibles():
    configuracion = {
        "variables": ["h", "lag_1"],
        "parametros": {"objective": "tweedie"},
        "num_boost_round": 320,
        "origenes_entrenamiento": [1541, 1609],
        "fraccion_ceros": 0.06,
    }
    firma = modelo.firma_configuracion(dict(configuracion), 30490)
    assert firma == modelo.firma_configuracion(dict(configuracion), 30490)
    assert firma != modelo.firma_configuracion(dict(configuracion), 4000), (
        "una muestra técnica no comparte identificador con el universo completo"
    )
    run = modelo.crear_run_id(version="v1", version_precios=None, variante="calendario", firma=firma)
    assert run.startswith("run-calendario-")
    assert run == modelo.crear_run_id(
        version="v1", version_precios=None, variante="calendario", firma=firma
    )
    assert run != modelo.crear_run_id(
        version="v1", version_precios="m5p-x", variante="calendario", firma=firma
    )


# --------------------------------------------------------------------------- #
# API: contrato del pronóstico y de la evaluación
# --------------------------------------------------------------------------- #

ULTIMO_OBSERVADO = 70
CORTE_EVALUACION = 42
RUN = "run-calendario-prueba0001"


@pytest.fixture()
def api_modelo(ventas, settings):
    """Publica una ejecución mínima (sin entrenar) y devuelve el cliente HTTP."""
    db, version = ventas["db"], ventas["version"]
    ultimo = db[M5_CALENDARIO].find_one(sort=[("d", -1)])
    fecha = ultimo["fecha"].date() if isinstance(ultimo["fecha"], datetime) else ultimo["fecha"]
    # El calendario sintético tiene 70 días: se añaden 28 para el horizonte final.
    for paso in range(1, modelo.HORIZONTE + 1):
        siguiente = fecha + timedelta(days=paso)
        db[M5_CALENDARIO].insert_one(
            {
                "_id": 5000 + paso,
                "d": ULTIMO_OBSERVADO + paso,
                "fecha": datetime.combine(siguiente, time(0, 0)),
                "wm_yr_wk": int(ultimo["wm_yr_wk"]) + paso // 7,
                "weekday": siguiente.strftime("%A"),
                "wday": siguiente.isoweekday(),
                "month": siguiente.month,
                "year": siguiente.year,
                "snap_ca": 0,
                "snap_tx": 0,
                "snap_wi": 0,
                "evento_1": None,
                "evento_2": None,
                "tipo_1": None,
                "tipo_2": None,
            }
        )
    dias = [
        {"d": ULTIMO_OBSERVADO + paso, "fecha": (fecha + timedelta(days=paso)).isoformat()}
        for paso in range(1, modelo.HORIZONTE + 1)
    ]
    db[M5_MODELOS].insert_one(
        {
            "_id": RUN,
            "run_id": RUN,
            "esquema": "m5-modelo-1",
            "version": version,
            "version_precios": "m5p-prueba",
            "variante": "calendario",
            "etiqueta": "Rezagos + calendario, eventos y SNAP",
            "metodo": "LightGBM global condicionado al horizonte",
            "estado": "publicada",
            "recomendado": True,
            "semilla": 1,
            "horizonte": modelo.HORIZONTE,
            # El último día usado en entrenamiento; el origen final es hasta + 28.
            "origenes_entrenamiento": [CORTE_EVALUACION],
            "ventana_entrenamiento": {"filas": 100, "hasta": ULTIMO_OBSERVADO - modelo.HORIZONTE},
            "variables": ["h", "lag_1", "snap"],
            "importancias": [{"variable": "lag_1", "ganancia": 10.0}],
            "supuestos": {"precio_futuro": "último precio conocido"},
            "cobertura": {
                "series": 9,
                "series_modelo": 8,
                "fallback": 1,
                "cobertura_candidato_pct": 88.89,
                "cobertura_sistema_pct": 100.0,
            },
            "metricas_resumen": {"final": {"mae": 1.0}},
            "creado_en": datetime(2013, 4, 1, 12, 0),
        }
    )
    for dimension, clave in (("total", "total"), ("tienda", "CA_1"), ("estado", "CA")):
        db[M5_PREDICCIONES].insert_one(
            {
                "_id": f"{RUN}:{ULTIMO_OBSERVADO}:{dimension}:{clave}",
                "run_id": RUN,
                "version": version,
                "tipo": "final",
                "origen_d": ULTIMO_OBSERVADO,
                "origen_fecha": fecha.isoformat(),
                "dimension": dimension,
                "clave": clave,
                "dias": dias,
                "pronostico": [10] * modelo.HORIZONTE,
                "baseline": [12] * modelo.HORIZONTE,
                "observado": None,
                "unidad": "unidades vendidas",
            }
        )
    # Corte de evaluación: mismas claves, con observado posterior.
    for dimension, clave in (("total", "total"), ("tienda", "CA_1")):
        db[M5_PREDICCIONES].insert_one(
            {
                "_id": f"{RUN}:{CORTE_EVALUACION}:{dimension}:{clave}",
                "run_id": RUN,
                "version": version,
                "tipo": "evaluacion",
                "origen_d": CORTE_EVALUACION,
                "origen_fecha": "2013-03-01",
                "dimension": dimension,
                "clave": clave,
                "dias": [
                    {"d": CORTE_EVALUACION + paso, "fecha": f"2013-03-{paso + 1:02d}"}
                    for paso in range(1, modelo.HORIZONTE + 1)
                ],
                "pronostico": [10] * modelo.HORIZONTE,
                "baseline": [12] * modelo.HORIZONTE,
                "observado": [11] * modelo.HORIZONTE,
                "unidad": "unidades vendidas",
            }
        )
    db[M5_EVALUACION_MODELOS].insert_one(
        {
            "_id": f"{RUN}:final:{CORTE_EVALUACION}",
            "run_id": RUN,
            "version": version,
            "etapa": "final",
            "origen_d": CORTE_EVALUACION,
            "origen_fecha": "2013-03-01",
            "fin_fecha": "2013-03-28",
            "series": 9,
            "observaciones": 9 * modelo.HORIZONTE,
            "candidato": {"n": 252, "mae": 1.0, "sesgo": 0.1, "wape": 10.0, "denominador": 2520},
            "baseline": {"n": 252, "mae": 2.0, "sesgo": 0.2, "wape": 20.0, "denominador": 2520},
            "mejora_pct": 50.0,
            "mejora_mae_pct": 50.0,
            "por_horizonte": [
                {
                    "horizonte": paso,
                    "fecha": (date(2013, 3, 1) + timedelta(days=paso - 1)).isoformat(),
                    "candidato": {"n": 9, "mae": 1.0, "sesgo": 0.1, "wape": 10.0, "denominador": 90},
                    "baseline": {"n": 9, "mae": 2.0, "sesgo": 0.2, "wape": 20.0, "denominador": 90},
                }
                for paso in range(1, modelo.HORIZONTE + 1)
            ],
            "segmentos": [
                {
                    "nivel": "tienda",
                    "clave": "CA_1",
                    "series": 3,
                    "candidato": {"n": 84, "mae": 1.0, "sesgo": 0.1, "wape": 10.0, "denominador": 840},
                    "baseline": {"n": 84, "mae": 2.0, "sesgo": 0.2, "wape": 20.0, "denominador": 840},
                    "mejora_pct": 50.0,
                    "agregado": {
                        "candidato": {"n": 28, "mae": 0.5, "sesgo": 0.05, "wape": 5.0, "denominador": 280},
                        "baseline": {"n": 28, "mae": 1.5, "sesgo": 0.15, "wape": 15.0, "denominador": 280},
                        "mejora_pct": 66.667,
                    },
                }
            ],
            "segmentos_mejoran": 1,
            "segmentos_empeoran": 0,
        }
    )
    app = create_app(settings)
    with TestClient(app) as cliente:
        yield {"cliente": cliente, "run_id": RUN}


def test_api_pronosticos_final(api_modelo):
    datos = api_modelo["cliente"].get(
        "/api/v1/comercial/m5/pronosticos?nivel=total&dias_historico=30"
    ).json()
    assert datos["run_id"] == api_modelo["run_id"]
    assert datos["modelo"]["variante"] == "calendario"
    assert datos["es_futuro"] is True
    assert datos["estado_evaluacion"] == "sin_observaciones"
    assert datos["cobertura"]["fallback"] == 1
    assert datos["supuestos"]["precio_futuro"]
    assert datos["rango_objetivo"]["dias"] == modelo.HORIZONTE
    assert datos["dias"][0]["d"] == ULTIMO_OBSERVADO + 1
    serie = datos["series"][0]
    assert len(serie["pronostico"]) == len(serie["baseline"]) == modelo.HORIZONTE
    # Sin observación posterior no se imputa nada.
    assert serie["observado"][-modelo.HORIZONTE :] == [None] * modelo.HORIZONTE
    assert len(datos["historico_dias"]) == min(30, ULTIMO_OBSERVADO)


def test_api_pronosticos_corte_de_evaluacion(api_modelo):
    datos = api_modelo["cliente"].get(
        f"/api/v1/comercial/m5/pronosticos?nivel=total&origen={CORTE_EVALUACION}"
    ).json()
    assert datos["origen_d"] == CORTE_EVALUACION
    assert datos["es_futuro"] is False
    assert datos["estado_evaluacion"] == "con_observaciones"
    assert datos["metricas"]["mae"] == 1.0
    assert datos["baseline_metricas"]["mae"] == 2.0
    assert datos["series"][0]["observado"][-modelo.HORIZONTE :] == [11] * modelo.HORIZONTE


def test_api_pronosticos_nivel_y_filtros(api_modelo):
    cliente = api_modelo["cliente"]
    tienda = cliente.get("/api/v1/comercial/m5/pronosticos?nivel=tienda&tienda=CA_1")
    assert tienda.status_code == 200
    assert tienda.json()["series"][0]["clave"] == "CA_1"

    # Una clave sin predicción publicada no se rellena con ceros.
    vacio = cliente.get("/api/v1/comercial/m5/pronosticos?nivel=tienda&tienda=TX_1")
    assert vacio.status_code == 422
    assert vacio.json()["detail"]["estado"] == "filtro_invalido"
    assert "TX_1" in vacio.json()["detail"]["mensaje"]

    origen = cliente.get("/api/v1/comercial/m5/pronosticos?nivel=total&origen=1")
    assert origen.status_code == 422
    assert "d_" in origen.json()["detail"]["mensaje"]


def test_api_evaluacion(api_modelo):
    cliente = api_modelo["cliente"]
    datos = cliente.get("/api/v1/comercial/m5/evaluacion?nivel=total").json()
    assert datos["etapa"] == "final"
    assert datos["resumen"]["mae"] == 1.0
    assert datos["baseline_resumen"]["mae"] == 2.0
    assert datos["mejora_pct"] == 50.0
    assert len(datos["por_horizonte"]) == modelo.HORIZONTE
    assert datos["mejoras"]["segmentos_mejoran"] == 1
    assert datos["cobertura"]["fallback"] == 1
    assert datos["ventanas"][0]["origen_d"] == CORTE_EVALUACION

    tienda = cliente.get("/api/v1/comercial/m5/evaluacion?nivel=tienda&clave=CA_1").json()
    assert tienda["resumen"]["mae"] == 1.0
    assert len(tienda["segmentos"]) == 1
    assert tienda["segmentos"][0]["agregado"]["mejora_pct"] == pytest.approx(66.667)

    assert cliente.get("/api/v1/comercial/m5/evaluacion?nivel=producto").status_code == 422
    assert cliente.get("/api/v1/comercial/m5/evaluacion?nivel=tienda&clave=NOPE").status_code == 422
    assert cliente.get("/api/v1/comercial/m5/evaluacion?nivel=total&etapa=otra").status_code == 422


def test_api_modelos(api_modelo):
    datos = api_modelo["cliente"].get("/api/v1/comercial/m5/modelos").json()
    assert datos["recomendado"] == api_modelo["run_id"]
    ejecucion = datos["ejecuciones"][0]
    assert ejecucion["variante"] == "calendario"
    assert ejecucion["importancias"][0]["variable"] == "lag_1"
    assert "no se versionan" in datos["nota"]
