"""Deriva los agregados de planificación y de evaluación a 28 días.

No entrena ni reimporta nada: carga el **modelo publicado** (su artefacto LightGBM) y la
matriz de ventas ya importada, y calcula dos derivados compactos que la API necesita para la
pantalla de planificación:

1. ``m5_planificacion`` (un documento por serie artículo × tienda, origen del pronóstico):
   unidades observadas en los 28 días anteriores al corte, pronóstico y baseline del
   horizonte, las cuatro semanas (horizontes 1–7, 8–14, 15–21, 22–28) y el estado de
   evaluación de esa serie.
2. ``m5_evaluacion_series`` (un documento por serie y corte evaluado): observado_28,
   pronostico_28, error_28 y la misma terna para el baseline, calculados **sumando primero el
   horizonte y restando después** (nunca sumando errores diarios).

Los documentos tienen ``_id`` determinista (versión · ejecución · origen · tienda · artículo),
así que volver a ejecutar el script deja exactamente el mismo estado. Se niega a escribir si
la ejecución publicada no tiene artefacto o si ya existe un derivado con otro contenido para
las mismas claves (nunca sobrescribe un resultado publicado con un cálculo parcial).

Uso:
    python -m scripts.derivar_planificacion [--version m5-...] [--run run-...]

Los artefactos pesados (matriz de ventas y booster) se leen de ``backend/data/artefactos`` y
no se versionan.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date

import numpy as np
from pymongo import ASCENDING, ReplaceOne

from app.comercial import modelo
from app.db.mongo import (
    M5_CATALOGO,
    M5_ESTADO,
    M5_EVALUACION_MODELOS,
    M5_EVALUACION_SERIES,
    M5_MODELOS,
    M5_PLANIFICACION,
    M5_PREDICCIONES_SERIES,
)
from app.comercial.modelo import HORIZONTE
from app.config import get_settings
from app.db.mongo import init_database

LOGGER = logging.getLogger("derivar_planificacion")

DIAS_OBSERVADOS = 28


def _series_ordenadas(db, version: str) -> tuple[list[tuple[str, str]], dict[str, str], dict[str, str]]:
    """Orden (tienda, artículo) idéntico al del modelo: tienda ascendente, artículo ascendente."""
    tiendas = sorted(
        documento["store_id"]
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "tienda"}, {"store_id": 1, "_id": 0}
        )
    )
    articulos = [
        documento["item_id"]
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "producto"}, {"item_id": 1, "_id": 0}
        ).sort("item_id", ASCENDING)
    ]
    cat_de = {
        documento["item_id"]: documento.get("cat_id")
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "producto"}, {"item_id": 1, "cat_id": 1, "_id": 0}
        )
    }
    dept_de = {
        documento["item_id"]: documento.get("dept_id")
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "producto"}, {"item_id": 1, "dept_id": 1, "_id": 0}
        )
    }
    series = [(tienda, articulo) for tienda in tiendas for articulo in articulos]
    return series, cat_de, dept_de


def _cargar_booster(ruta: str):
    import lightgbm as lgb

    return lgb.Booster(model_file=ruta)


def main() -> int:
    parser = argparse.ArgumentParser(description="Deriva planificación y evaluación a 28 días")
    parser.add_argument("--version", default=None, help="Versión de datos (por omisión, la publicada)")
    parser.add_argument("--run", default=None, help="Ejecución (por omisión, la recomendada)")
    parser.add_argument("--sin-cache", action="store_true", help="No usar la matriz de ventas en caché")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ajustes = get_settings()
    db = init_database(ajustes).m5_db

    version = args.version
    if not version:
        estado = db[M5_ESTADO].find_one({"publicada": True}) or db[M5_ESTADO].find_one()
        if not estado:
            raise SystemExit("No hay versión publicada en m5_estado")
        version = estado["version"]

    run_id = args.run
    if not run_id:
        documento = db[M5_MODELOS].find_one(
            {"version": version, "recomendado": True}
        ) or db[M5_MODELOS].find_one(
            {"version": version, "estado": "publicada"}, sort=[("creado_en", -1)]
        )
        if not documento:
            raise SystemExit(f"Sin ejecución publicada para {version}")
        run_id = documento["run_id"]

    documento = db[M5_MODELOS].find_one({"_id": run_id, "version": version})
    if not documento:
        raise SystemExit(f"No existe la ejecución {run_id} para {version}")
    ruta = documento.get("artefacto")
    if not ruta:
        raise SystemExit(f"La ejecución {run_id} no tiene artefacto: no se puede derivar sin el modelo")

    inicio = time.perf_counter()
    ctx = modelo.cargar_contexto(
        db, version, version_precios=documento.get("version_precios"), usar_cache=not args.sin_cache
    )
    LOGGER.info("Contexto cargado en %.1f s", time.perf_counter() - inicio)

    booster = _cargar_booster(ruta)
    modelo_dict = {
        "booster": booster,
        "variables": documento["variables"],
        "horizonte": int(documento.get("horizonte", HORIZONTE)),
        "num_boost_round": documento.get("num_boost_round"),
    }

    # Origen del pronóstico publicado: el último día observado. Las predicciones cubren los 28
    # días siguientes (días de calendario que todavía no tienen ventas). Es el mismo origen que
    # usa ``scripts/entrenar_m5.py`` al publicar las predicciones.
    ultimo = int(ctx.n_dias)
    origen_plan = ultimo

    # El orden de las series es el del modelo (artículo mayor, tienda menor): se toma del
    # contexto para que la matriz de predicciones y su etiqueta coincidan siempre.
    series = [(tienda, articulo) for articulo, tienda in ctx.series]
    _, cat_de, dept_de = _series_ordenadas(db, version)
    if len(series) != ctx.n_series:
        raise SystemExit(
            f"El contexto tiene {ctx.n_series} series y se esperaban {len(series)}: no coinciden"
        )

    # Cortes evaluados de la ejecución publicada (etapa final).
    cortes = sorted(
        {
            int(registro["origen_d"])
            for registro in db[M5_EVALUACION_MODELOS].find(
                {"version": version, "run_id": run_id}, {"origen_d": 1, "_id": 0}
            )
        }
    )
    LOGGER.info("Cortes evaluados de %s: %s", run_id, cortes)

    # --- 1. Planificación ---------------------------------------------------
    # Se usan las predicciones **publicadas** (un entero por día), de modo que los totales de la
    # tabla, el resumen y la descarga reconstruyan exactamente las series que sirve /pronosticos.
    publicadas = {
        (documento["store_id"], documento["item_id"]): documento
        for documento in db[M5_PREDICCIONES_SERIES].find(
            {"run_id": run_id, "origen_d": origen_plan},
            {"store_id": 1, "item_id": 1, "pronostico": 1, "baseline": 1, "_id": 0},
        )
    }
    if len(publicadas) != ctx.n_series:
        raise SystemExit(
            f"Hay {len(publicadas)} predicciones publicadas y {ctx.n_series} series: no se puede derivar"
        )
    pronostico = np.array(
        [publicadas[(tienda, articulo)]["pronostico"] for tienda, articulo in series], dtype=np.float64
    )
    baseline = np.array(
        [publicadas[(tienda, articulo)]["baseline"] for tienda, articulo in series], dtype=np.float64
    )
    LOGGER.info(
        "Pronóstico publicado leído: %s series · total del horizonte %.0f u",
        pronostico.shape[0],
        float(pronostico.sum()),
    )

    desde_prev = origen_plan - DIAS_OBSERVADOS
    observado_prev = ctx.ventas[:, desde_prev:origen_plan].astype(np.float64)
    semanas_pron = pronostico.reshape(ctx.n_series, 4, 7).sum(axis=2)
    semanas_base = baseline.reshape(ctx.n_series, 4, 7).sum(axis=2)

    # Estado por serie a partir de los cortes evaluados (se rellena tras el paso 2).
    estados: dict[tuple[str, str], dict] = {}

    # --- 2. Evaluación a 28 días por serie y corte --------------------------
    docs_evaluacion: list[ReplaceOne] = []
    for origen in cortes:
        if origen + HORIZONTE > ctx.n_dias:
            LOGGER.warning("Corte d_%s sin 28 días observados: se omite", origen)
            continue
        pred, _ = modelo.predecir(ctx, modelo_dict, origen, baseline=modelo.predecir_baseline(ctx, origen))
        base = modelo.predecir_baseline(ctx, origen)
        obs = ctx.ventas[:, origen : origen + HORIZONTE].astype(np.float64)
        obs28 = obs.sum(axis=1)
        # El error del horizonte se calcula sumando primero y restando después.
        pron28 = pred.astype(np.float64).sum(axis=1)
        base28 = base.astype(np.float64).sum(axis=1)
        err28 = pron28 - obs28
        base_err28 = base28 - obs28
        # Grupos descriptivos con información ANTERIOR al corte (nunca el horizonte evaluado).
        prev = ctx.ventas[:, max(0, origen - 56) : origen]
        obs_prev_56 = prev.sum(axis=1)
        ceros_prev_56 = (prev == 0).sum(axis=1)

        for indice, (tienda, articulo) in enumerate(series):
            clave = (tienda, articulo)
            acumulado = estados.setdefault(
                clave, {"cortes": 0, "mejoran": 0, "empeoran": 0, "empatan": 0}
            )
            acumulado["cortes"] += 1
            if abs(err28[indice]) < abs(base_err28[indice]) - 1e-9:
                acumulado["mejoran"] += 1
            elif abs(err28[indice]) > abs(base_err28[indice]) + 1e-9:
                acumulado["empeoran"] += 1
            else:
                acumulado["empatan"] += 1
            docs_evaluacion.append(
                ReplaceOne(
                    {"_id": f"{version}|{run_id}|{origen}|{tienda}|{articulo}"},
                    {
                        "_id": f"{version}|{run_id}|{origen}|{tienda}|{articulo}",
                        "esquema": "m5-evaluacion-serie-1",
                        "version": version,
                        "run_id": run_id,
                        "origen_d": origen,
                        "origen_fecha": ctx.fecha_de_dia[origen - 1].isoformat(),
                        "fin_fecha": ctx.fecha_de_dia[origen + HORIZONTE - 1].isoformat(),
                        "store_id": tienda,
                        "item_id": articulo,
                        "cat_id": cat_de.get(articulo),
                        "dept_id": dept_de.get(articulo),
                        "observado_28": float(obs28[indice]),
                        "pronostico_28": float(pron28[indice]),
                        "error_28": float(err28[indice]),
                        "error_absoluto_28": float(abs(err28[indice])),
                        "baseline_28": float(base28[indice]),
                        "baseline_error_28": float(base_err28[indice]),
                        "baseline_error_absoluto_28": float(abs(base_err28[indice])),
                        "observado_previo_56": float(obs_prev_56[indice]),
                        "ceros_previos_56": int(ceros_prev_56[indice]),
                        "unidad": "unidades vendidas",
                    },
                    upsert=True,
                )
            )
        LOGGER.info("Corte d_%s derivado (%s series)", origen, len(series))

    if docs_evaluacion:
        db[M5_EVALUACION_SERIES].bulk_write(docs_evaluacion, ordered=False)
        db[M5_EVALUACION_SERIES].create_index(
            [("version", 1), ("run_id", 1), ("origen_d", 1), ("store_id", 1), ("item_id", 1)]
        )
        db[M5_EVALUACION_SERIES].create_index([("version", 1), ("run_id", 1), ("store_id", 1)])
        LOGGER.info("Evaluación por serie guardada: %s documentos", len(docs_evaluacion))

    # --- 3. Documentos de planificación -------------------------------------
    docs_plan: list[ReplaceOne] = []
    for indice, (tienda, articulo) in enumerate(series):
        estado = estados.get((tienda, articulo), {"cortes": 0, "mejoran": 0, "empeoran": 0, "empatan": 0})
        if not estado["cortes"]:
            situacion = "sin_evaluacion"
        elif estado["mejoran"] and not estado["empeoran"]:
            situacion = "menor_error"
        elif estado["empeoran"] and not estado["mejoran"]:
            situacion = "mayor_error"
        elif estado["mejoran"] and estado["empeoran"]:
            situacion = "mixto"
        else:
            situacion = "empate"
        pron28 = float(pronostico[indice].astype(np.float64).sum())
        base28 = float(baseline[indice].astype(np.float64).sum())
        obs28 = float(observado_prev[indice].sum())
        docs_plan.append(
            ReplaceOne(
                {"_id": f"{version}|{run_id}|{tienda}|{articulo}"},
                {
                    "_id": f"{version}|{run_id}|{tienda}|{articulo}",
                    "esquema": "m5-planificacion-1",
                    "version": version,
                    "run_id": run_id,
                    "origen_d": origen_plan,
                    "origen_fecha": ctx.fecha_de_dia[origen_plan - 1].isoformat(),
                    "horizonte_inicio": ctx.fecha_de_dia[origen_plan].isoformat(),
                    "horizonte_fin": ctx.fecha_de_dia[origen_plan + HORIZONTE - 1].isoformat(),
                    "observado_desde": ctx.fecha_de_dia[desde_prev].isoformat(),
                    "observado_hasta": ctx.fecha_de_dia[origen_plan - 1].isoformat(),
                    "store_id": tienda,
                    "item_id": articulo,
                    "cat_id": cat_de.get(articulo),
                    "dept_id": dept_de.get(articulo),
                    "observado_28": obs28,
                    "pronostico_28": pron28,
                    "baseline_28": base28,
                    "diferencia": pron28 - obs28,
                    "variacion_pct": (None if obs28 <= 0 else 100 * (pron28 - obs28) / obs28),
                    "semanas": [float(valor) for valor in semanas_pron[indice]],
                    "semanas_baseline": [float(valor) for valor in semanas_base[indice]],
                    "semanas_observado": [
                        float(valor) for valor in observado_prev[indice].reshape(4, 7).sum(axis=1)
                    ],
                    "evaluacion": {
                        "cortes": estado["cortes"],
                        "mejoran": estado["mejoran"],
                        "empeoran": estado["empeoran"],
                        "empatan": estado["empatan"],
                        "estado": situacion,
                    },
                    "unidad": "unidades vendidas",
                },
                upsert=True,
            )
        )

    db[M5_PLANIFICACION].bulk_write(docs_plan, ordered=False)
    db[M5_PLANIFICACION].create_index(
        [("version", 1), ("run_id", 1), ("store_id", 1), ("item_id", 1)]
    )
    db[M5_PLANIFICACION].create_index([("version", 1), ("run_id", 1), ("dept_id", 1)])
    db[M5_PLANIFICACION].create_index([("version", 1), ("run_id", 1), ("pronostico_28", -1)])
    db[M5_PLANIFICACION].create_index([("version", 1), ("run_id", 1), ("observado_28", -1)])
    LOGGER.info("Planificación guardada: %s documentos", len(docs_plan))

    total = time.perf_counter() - inicio
    LOGGER.info("Derivado completo en %.1f s · versión %s · ejecución %s", total, version, run_id)
    print(
        f"OK {len(docs_plan)} series de planificación · {len(docs_evaluacion)} evaluación(es) por serie "
        f"· versión {version} · ejecución {run_id} · {total:.1f} s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
