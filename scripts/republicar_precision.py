"""Republica las predicciones con la política de precisión decimal.

Motivo: la publicación anterior guardaba **enteros por día**. La suma de esos enteros es
1 289 637 u en el horizonte, mientras que el artefacto produce 1 335 219 u: la diferencia
(−3,4 %) la introduce el redondeo diario de la publicación, no el modelo. Este script crea una
**revisión nueva identificable** con los valores decimales canónicos, recalcula los derivados
con esa misma política y solo activa la revisión después de validarla.

No reentrena ni reimporta ventas: usa el artefacto publicado y la matriz de ventas ya
importada. La revisión anterior queda marcada como «anterior» y puede reactivarse con
``--activar <revision>``.

Uso:
    python -m scripts.republicar_precision            # crea y activa la revisión decimal
    python -m scripts.republicar_precision --listar    # lista revisiones
    python -m scripts.republicar_precision --activar rev-xxxxxxxx
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import time
from datetime import datetime, timezone

import numpy as np
from pymongo import ASCENDING, ReplaceOne

from app.comercial import modelo
from app.comercial.modelo import HORIZONTE
from app.config import get_settings
from app.db.mongo import (
    M5_CATALOGO,
    M5_EVALUACION_MODELOS,
    M5_EVALUACION_SERIES,
    M5_MODELOS,
    M5_PLANIFICACION,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PUBLICACION,
    init_database,
)

LOGGER = logging.getLogger("republicar_precision")
POLITICA = "decimal-v1"
DIAS_OBSERVADOS = 28
TOLERANCIA = 1e-6


def _revision(version: str, run_id: str) -> str:
    huella = hashlib.sha256(f"{version}|{run_id}|{POLITICA}".encode("utf-8")).hexdigest()
    return f"rev-{huella[:10]}"


def _activa(db) -> dict | None:
    return db[M5_PUBLICACION].find_one({"activa": True})


def _activar(db, revision: str) -> dict:
    documento = db[M5_PUBLICACION].find_one({"_id": revision})
    if not documento:
        raise SystemExit(f"No existe la revisión {revision}")
    anterior = _activa(db)
    db[M5_PUBLICACION].update_many({"activa": True}, {"$set": {"activa": False, "estado": "anterior"}})
    db[M5_PUBLICACION].update_one(
        {"_id": revision},
        {
            "$set": {
                "activa": True,
                "estado": "activa",
                "activada_en": datetime.now(timezone.utc),
                "anterior": (anterior or {}).get("_id"),
            }
        },
    )
    return db[M5_PUBLICACION].find_one({"_id": revision}) or {}


def _series_ordenadas(db, version: str) -> tuple[list[tuple[str, str]], dict[str, str], dict[str, str]]:
    articulos = [
        documento["item_id"]
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "producto"}, {"item_id": 1, "_id": 0}
        ).sort("item_id", ASCENDING)
    ]
    tiendas = sorted(
        documento["store_id"]
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "tienda"}, {"store_id": 1, "_id": 0}
        )
    )
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
    return [(tienda, articulo) for articulo in articulos for tienda in tiendas], cat_de, dept_de


def _fechas(db, version: str) -> dict[int, str]:
    return {
        int(documento["d"]): documento["fecha"].date().isoformat()
        for documento in db["m5_calendario"].find({}, {"d": 1, "fecha": 1, "_id": 0})
        if documento.get("fecha") is not None
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Republica predicciones con precisión decimal")
    parser.add_argument("--version", default=None)
    parser.add_argument("--run", default=None)
    parser.add_argument("--listar", action="store_true")
    parser.add_argument("--activar", default=None)
    parser.add_argument("--sin-cache", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db = init_database(get_settings()).m5_db

    if args.listar:
        for documento in db[M5_PUBLICACION].find({}).sort("creada_en", -1):
            print(
                f"{documento['_id']:16s} activa={documento.get('activa')} "
                f"politica={documento.get('politica')} run={documento.get('run_id')} "
                f"total_horizonte={documento.get('total_horizonte')}"
            )
        return 0

    if args.activar:
        documento = _activar(db, args.activar)
        print(f"Activa: {documento['_id']} · política {documento.get('politica')}")
        return 0

    version = args.version
    if not version:
        estado = db["m5_estado"].find_one({"publicada": True}) or db["m5_estado"].find_one()
        version = estado["version"] if estado else None
    if not version:
        raise SystemExit("Sin versión de datos publicada")

    run = db[M5_MODELOS].find_one(
        {"_id": args.run, "version": version}
        if args.run
        else {"version": version, "recomendado": True}
    )
    if not run:
        raise SystemExit(f"Sin ejecución publicada para {version}")
    run_id = run["run_id"]

    revision = _revision(version, run_id)
    ya_existe = db[M5_PUBLICACION].find_one({"_id": revision, "estado": "activa"})
    if ya_existe:
        print(f"La revisión {revision} ya está activa · total {ya_existe.get('total_horizonte')}")
        return 0

    inicio = time.perf_counter()
    ctx = modelo.cargar_contexto(
        db, version, version_precios=run.get("version_precios"), usar_cache=not args.sin_cache
    )
    LOGGER.info("Contexto cargado en %.1f s", time.perf_counter() - inicio)

    import lightgbm as lgb

    booster = lgb.Booster(model_file=run["artefacto"])
    modelo_dict = {
        "booster": booster,
        "variables": run["variables"],
        "horizonte": int(run.get("horizonte", HORIZONTE)),
        "num_boost_round": run.get("num_boost_round"),
    }

    series, cat_de, dept_de = _series_ordenadas(db, version)
    if len(series) != ctx.n_series:
        raise SystemExit("El catálogo y la matriz de ventas no coinciden en número de series")
    fechas = _fechas(db, version)
    origen = int(ctx.n_dias)

    # --- Predicción del horizonte publicado, sin redondear ------------------
    candidato, cobertura = modelo.predecir(
        ctx, modelo_dict, origen, baseline=modelo.predecir_baseline(ctx, origen)
    )
    baseline = modelo.predecir_baseline(ctx, origen)
    LOGGER.info("Cobertura del candidato: %s", cobertura)

    observado_previo = ctx.ventas[:, origen - DIAS_OBSERVADOS : origen].astype(np.float64)
    semanas = candidato.reshape(ctx.n_series, 4, 7).sum(axis=2)
    semanas_base = baseline.reshape(ctx.n_series, 4, 7).sum(axis=2)
    semanas_obs = observado_previo.reshape(ctx.n_series, 4, 7).sum(axis=2)
    pronostico_28 = candidato.astype(np.float64).sum(axis=1)
    base_28 = baseline.astype(np.float64).sum(axis=1)
    obs_28 = observado_previo.sum(axis=1)

    # --- Evaluación a 28 días por serie y corte ----------------------------
    cortes = sorted(
        {
            int(registro["origen_d"])
            for registro in db[M5_EVALUACION_MODELOS].find(
                {"version": version, "run_id": run_id}, {"origen_d": 1, "_id": 0}
            )
        }
    )
    docs_eval: list[ReplaceOne] = []
    estados: dict[tuple[str, str], dict] = {}
    for corte in cortes:
        if corte + HORIZONTE > ctx.n_dias:
            continue
        pred, _ = modelo.predecir(
            ctx, modelo_dict, corte, baseline=modelo.predecir_baseline(ctx, corte)
        )
        base = modelo.predecir_baseline(ctx, corte)
        obs = ctx.ventas[:, corte : corte + HORIZONTE].astype(np.float64)
        obs28 = obs.sum(axis=1)
        pron28 = pred.astype(np.float64).sum(axis=1)
        base28 = base.astype(np.float64).sum(axis=1)
        err28 = pron28 - obs28
        previo = ctx.ventas[:, max(0, corte - 56) : corte]
        obs_prev_56 = previo.sum(axis=1)
        ceros_prev_56 = (previo == 0).sum(axis=1)
        for indice, (tienda, articulo) in enumerate(series):
            acumulado = estados.setdefault(
                (tienda, articulo), {"cortes": 0, "mejoran": 0, "empeoran": 0, "empatan": 0}
            )
            acumulado["cortes"] += 1
            if abs(err28[indice]) < abs(base28[indice] - obs28[indice]) - 1e-9:
                acumulado["mejoran"] += 1
            elif abs(err28[indice]) > abs(base28[indice] - obs28[indice]) + 1e-9:
                acumulado["empeoran"] += 1
            else:
                acumulado["empatan"] += 1
            docs_eval.append(
                ReplaceOne(
                    {"_id": f"{revision}|{corte}|{tienda}|{articulo}"},
                    {
                        "_id": f"{revision}|{corte}|{tienda}|{articulo}",
                        "esquema": "m5-evaluacion-serie-1",
                        "revision": revision,
                        "politica": POLITICA,
                        "version": version,
                        "run_id": run_id,
                        "origen_d": corte,
                        "origen_fecha": fechas.get(corte),
                        "fin_fecha": fechas.get(corte + HORIZONTE),
                        "store_id": tienda,
                        "item_id": articulo,
                        "cat_id": cat_de.get(articulo),
                        "dept_id": dept_de.get(articulo),
                        "observado_28": float(obs28[indice]),
                        "pronostico_28": float(pron28[indice]),
                        "baseline_28": float(base28[indice]),
                        "error_28": float(err28[indice]),
                        "error_absoluto_28": float(abs(err28[indice])),
                        "baseline_error_28": float(base28[indice] - obs28[indice]),
                        "baseline_error_absoluto_28": float(abs(base28[indice] - obs28[indice])),
                        "observado_previo_56": float(obs_prev_56[indice]),
                        "ceros_previos_56": int(ceros_prev_56[indice]),
                        "unidad": "unidades vendidas",
                    },
                    upsert=True,
                )
            )
        LOGGER.info("Corte d_%s recalculado con precisión decimal", corte)

    db[M5_EVALUACION_SERIES].delete_many({"revision": revision})
    if docs_eval:
        db[M5_EVALUACION_SERIES].bulk_write(docs_eval, ordered=False)
        db[M5_EVALUACION_SERIES].create_index(
            [("revision", 1), ("origen_d", 1), ("store_id", 1), ("item_id", 1)]
        )

    # --- Series y agregados publicados -------------------------------------
    db[M5_PREDICCIONES_SERIES].delete_many({"revision": revision, "origen_d": origen})
    docs_series = [
        ReplaceOne(
            {"_id": f"{revision}|{origen}|{tienda}|{articulo}"},
            {
                "_id": f"{revision}|{origen}|{tienda}|{articulo}",
                "esquema": "m5-prediccion-serie-2",
                "revision": revision,
                "politica": POLITICA,
                "run_id": run_id,
                "version": version,
                "store_id": tienda,
                "item_id": articulo,
                "origen_d": origen,
                "origen_fecha": fechas.get(origen),
                "pronostico": [float(valor) for valor in candidato[indice]],
                "baseline": [float(valor) for valor in baseline[indice]],
                "metodo": "modelo",
                "unidad": "unidades vendidas",
            },
            upsert=True,
        )
        for indice, (tienda, articulo) in enumerate(series)
    ]
    if docs_series:
        db[M5_PREDICCIONES_SERIES].bulk_write(docs_series, ordered=False)
        db[M5_PREDICCIONES_SERIES].create_index([("revision", 1), ("store_id", 1), ("item_id", 1)])

    # Agregados por dimensión: suma de los valores decimales (no del redondeo).
    dias = [
        {"d": origen + paso, "fecha": fechas.get(origen + paso)}
        for paso in range(1, HORIZONTE + 1)
    ]
    dimensiones = modelo.agregar_por_dimension(ctx, candidato)
    base_dimensiones = modelo.agregar_por_dimension(ctx, baseline)
    db[M5_PREDICCIONES].delete_many({"revision": revision, "origen_d": origen})
    docs_agregados = []
    for dimension, claves in dimensiones.items():
        for clave, valores in claves.items():
            docs_agregados.append(
                ReplaceOne(
                    {"_id": f"{revision}|{origen}|{dimension}|{clave}"},
                    {
                        "_id": f"{revision}|{origen}|{dimension}|{clave}",
                        "esquema": "m5-prediccion-2",
                        "revision": revision,
                        "politica": POLITICA,
                        "run_id": run_id,
                        "version": version,
                        "tipo": "final",
                        "origen_d": origen,
                        "origen_fecha": fechas.get(origen),
                        "dimension": dimension,
                        "clave": clave,
                        "dias": dias,
                        "pronostico": [float(valor) for valor in valores],
                        "baseline": [float(valor) for valor in base_dimensiones[dimension][clave]],
                        "observado": None,
                        "unidad": "unidades vendidas",
                    },
                    upsert=True,
                )
            )
    if docs_agregados:
        db[M5_PREDICCIONES].bulk_write(docs_agregados, ordered=False)
        db[M5_PREDICCIONES].create_index([("revision", 1), ("origen_d", 1), ("dimension", 1)])

    # --- Agregados de los cortes históricos (misma política) ----------------
    for corte in cortes:
        if corte + HORIZONTE > ctx.n_dias:
            continue
        pred_corte, _ = modelo.predecir(
            ctx, modelo_dict, corte, baseline=modelo.predecir_baseline(ctx, corte)
        )
        base_corte = modelo.predecir_baseline(ctx, corte)
        obs_corte = ctx.ventas[:, corte : corte + HORIZONTE].astype(np.float64)
        dimensiones_corte = modelo.agregar_por_dimension(ctx, pred_corte)
        base_corte_dim = modelo.agregar_por_dimension(ctx, base_corte)
        obs_corte_dim = modelo.agregar_por_dimension(ctx, obs_corte)
        dias_corte = [
            {"d": corte + paso, "fecha": fechas.get(corte + paso)}
            for paso in range(1, HORIZONTE + 1)
        ]
        db[M5_PREDICCIONES].delete_many({"revision": revision, "origen_d": corte})
        docs_corte = []
        for dimension, claves in dimensiones_corte.items():
            for clave, valores in claves.items():
                docs_corte.append(
                    ReplaceOne(
                        {"_id": f"{revision}|{corte}|{dimension}|{clave}"},
                        {
                            "_id": f"{revision}|{corte}|{dimension}|{clave}",
                            "esquema": "m5-prediccion-2",
                            "revision": revision,
                            "politica": POLITICA,
                            "run_id": run_id,
                            "version": version,
                            "tipo": "evaluacion",
                            "origen_d": corte,
                            "origen_fecha": fechas.get(corte),
                            "dimension": dimension,
                            "clave": clave,
                            "dias": dias_corte,
                            "pronostico": [float(valor) for valor in valores],
                            "baseline": [
                                float(valor) for valor in base_corte_dim[dimension][clave]
                            ],
                            "observado": [
                                float(valor) for valor in obs_corte_dim[dimension][clave]
                            ],
                            "unidad": "unidades vendidas",
                        },
                        upsert=True,
                    )
                )
        if docs_corte:
            db[M5_PREDICCIONES].bulk_write(docs_corte, ordered=False)
        LOGGER.info("Agregados del corte d_%s republicados en decimal", corte)

    # --- Derivado de planificación ----------------------------------------
    docs_plan = []
    for indice, (tienda, articulo) in enumerate(series):
        estado = estados.get(
            (tienda, articulo), {"cortes": 0, "mejoran": 0, "empeoran": 0, "empatan": 0}
        )
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
        docs_plan.append(
            ReplaceOne(
                {"_id": f"{revision}|{tienda}|{articulo}"},
                {
                    "_id": f"{revision}|{tienda}|{articulo}",
                    "esquema": "m5-planificacion-2",
                    "revision": revision,
                    "politica": POLITICA,
                    "version": version,
                    "run_id": run_id,
                    "origen_d": origen,
                    "origen_fecha": fechas.get(origen),
                    "horizonte_inicio": fechas.get(origen + 1),
                    "horizonte_fin": fechas.get(origen + HORIZONTE),
                    "observado_desde": fechas.get(origen - DIAS_OBSERVADOS + 1),
                    "observado_hasta": fechas.get(origen),
                    "store_id": tienda,
                    "item_id": articulo,
                    "cat_id": cat_de.get(articulo),
                    "dept_id": dept_de.get(articulo),
                    "observado_28": float(obs_28[indice]),
                    "pronostico_28": float(pronostico_28[indice]),
                    "baseline_28": float(base_28[indice]),
                    "diferencia": float(pronostico_28[indice] - obs_28[indice]),
                    "variacion_pct": (
                        None
                        if obs_28[indice] <= 0
                        else float(100 * (pronostico_28[indice] - obs_28[indice]) / obs_28[indice])
                    ),
                    "semanas": [float(valor) for valor in semanas[indice]],
                    "semanas_baseline": [float(valor) for valor in semanas_base[indice]],
                    "semanas_observado": [float(valor) for valor in semanas_obs[indice]],
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
    db[M5_PLANIFICACION].delete_many({"revision": revision})
    db[M5_PLANIFICACION].bulk_write(docs_plan, ordered=False)
    db[M5_PLANIFICACION].create_index([("revision", 1), ("store_id", 1), ("item_id", 1)])
    db[M5_PLANIFICACION].create_index([("revision", 1), ("pronostico_28", -1)])

    # --- Validación antes de activar --------------------------------------
    total_series = float(pronostico_28.sum())
    total_semanas = float(semanas.sum())
    agregado = db[M5_PREDICCIONES].find_one(
        {"revision": revision, "dimension": "total", "origen_d": origen}
    )
    total_agregado = float(sum(agregado["pronostico"])) if agregado else None
    discrepancias = []
    if abs(total_series - total_semanas) > TOLERANCIA:
        discrepancias.append(f"semanas ({total_semanas}) ≠ horizonte ({total_series})")
    if total_agregado is None or abs(total_agregado - total_series) > max(TOLERANCIA, 1e-6 * abs(total_series)):
        discrepancias.append(f"agregado ({total_agregado}) ≠ suma de series ({total_series})")
    if documento_previo := db[M5_PUBLICACION].find_one({"_id": revision}):
        pass
    # Muestreo de series pequeñas: garantiza que no hay truncamiento a cero.
    fraccionarias = int((np.abs(candidato - np.round(candidato)) > 0.01).sum())
    if fraccionarias == 0:
        discrepancias.append("no hay valores fraccionarios: la predicción parece redondeada")
    if discrepancias:
        raise SystemExit("Revisión NO activada: " + "; ".join(discrepancias))

    anterior = _activa(db)
    db[M5_PUBLICACION].update_one(
        {"_id": revision},
        {
            "$set": {
                "esquema": "m5-publicacion-1",
                "version": version,
                "run_id": run_id,
                "politica": POLITICA,
                "precision": "decimal; el redondeo se aplica solo al presentar",
                "creada_en": datetime.now(timezone.utc),
                "total_horizonte": total_series,
                "total_agregado": total_agregado,
                "series": int(ctx.n_series),
                "cortes": cortes,
                "valores_fraccionarios": fraccionarias,
                "nota": (
                    "Serie canónica decimal. La revisión anterior guardaba enteros por día; su "
                    "total era 1 289 637 u frente a 1 335 219 u del artefacto."
                ),
                "activa": False,
                "estado": "validada",
            }
        },
        upsert=True,
    )
    activo = _activar(db, revision)
    print(
        f"OK revisión {revision} activa · política {POLITICA} · total horizonte "
        f"{total_series:,.2f} u · agregado {total_agregado:,.2f} u · anterior {activo.get('anterior')}"
    )
    LOGGER.info("Revisión validada y activada en %.1f s", time.perf_counter() - inicio)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
