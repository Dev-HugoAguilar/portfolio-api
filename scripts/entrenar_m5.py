"""Entrena, evalúa y publica el modelo predictivo a 28 días (Walmart M5).

Etapas
------
1. **Piloto** (``--piloto``): subconjunto determinista (tres estados, tres categorías,
   distintas intensidades de ventas) para medir tiempos, memoria y tamaño de
   artefactos antes de ampliar. No es el resultado final.
2. **Desarrollo** (``--variantes``): entrena las variantes candidatas con cortes de
   desarrollo y compara contra el baseline. Sirve para elegir el modelo recomendado.
3. **Final**: evalúa el modelo elegido en los cortes reservados y genera el pronóstico
   desde el último día observado.

Uso:
    python -m scripts.entrenar_m5 --piloto
    python -m scripts.entrenar_m5 --variantes
    python -m scripts.entrenar_m5 --final
    python -m scripts.entrenar_m5 --todo        # desarrollo + final + publicación
    python -m scripts.entrenar_m5 --estado      # ejecuciones guardadas

Los artefactos pesados (matriz de ventas y modelos) se guardan en
``backend/data/artefactos`` (fuera de Git). En MongoDB local quedan resultados,
metadatos y evaluaciones.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.comercial import ingesta, modelo, precios as precios_mod
from app.config import get_settings
from app.db.mongo import (
    M5_EVALUACION_MODELOS,
    M5_MODELOS,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    init_database,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("entrenar_m5")

OK = 0
FALLO = 1


def _json(datos) -> None:
    print(json.dumps(datos, ensure_ascii=False, indent=2, default=str))


def _log(mensaje: dict) -> None:
    logger.info("%s", json.dumps(mensaje, ensure_ascii=False, default=str))


def _guardar_modelo(modelo_entrenado: dict, run_id: str) -> Path:
    """Guarda el booster en disco (artefacto de trabajo, fuera de Git)."""
    modelo.ARTEFACTOS.mkdir(parents=True, exist_ok=True)
    ruta = modelo.ARTEFACTOS / f"{run_id}.txt"
    modelo_entrenado["booster"].save_model(str(ruta))
    return ruta


def _subconjunto_piloto(ctx: modelo.Contexto) -> np.ndarray:
    """Series del piloto: 40 artículos por categoría en los tres estados.

    Determinista: se ordenan los artículos por unidades totales y se toman el más
    vendido, el mediano y el menos vendido de cada categoría, más una muestra
    sistemática. Incluye series con ventas cero.
    """
    totales = ctx.ventas.sum(axis=1)
    categorias = np.array([ctx.nombres["categorias"][i] for i in ctx.cat_idx])
    indices: list[int] = []
    for categoria in sorted(set(categorias)):
        propias = np.flatnonzero(categorias == categoria)
        orden = propias[np.argsort(totales[propias])]
        elegidos = set(orden[:5]) | set(orden[-5:]) | set(orden[len(orden) // 2 : len(orden) // 2 + 10])
        elegidos |= set(orden[:: max(1, len(orden) // 20)][:20])
        indices.extend(sorted(elegidos))
    return np.array(sorted(set(indices)), dtype=np.int64)


def _matriz(ctx: modelo.Contexto, indices: np.ndarray | None) -> modelo.Contexto:
    """Copia del contexto restringida a un subconjunto de series."""
    if indices is None:
        return ctx
    return modelo.Contexto(
        version=ctx.version,
        version_precios=ctx.version_precios,
        series=[ctx.series[i] for i in indices],
        item_idx=ctx.item_idx[indices],
        store_idx=ctx.store_idx[indices],
        cat_idx=ctx.cat_idx[indices],
        dept_idx=ctx.dept_idx[indices],
        state_idx=ctx.state_idx[indices],
        ventas=ctx.ventas[indices],
        calendario=ctx.calendario,
        semana_por_dia=ctx.semana_por_dia,
        semanas=ctx.semanas,
        precios_serie=ctx.precios_serie[indices] if ctx.precios_serie is not None else None,
        precios_rellenados=(
            ctx.precios_rellenados[indices] if ctx.precios_rellenados is not None else None
        ),
        pos_semana=ctx.pos_semana,
        fecha_de_dia=ctx.fecha_de_dia,
        nombres=ctx.nombres,
        metricas_carga={**ctx.metricas_carga, "series_piloto": int(len(indices))},
    )


def _supuestos(variables: list[str]) -> dict[str, str]:
    """Supuestos del manifiesto derivados de las variables que la ejecución usa de verdad.

    La política de precios solo se declara en la variante que tiene variables de precio; el
    resto de variantes no congela ningún precio y lo dice. La precisión se declara con la
    política vigente: la serie canónica conserva decimales y el redondeo es solo de
    presentación.
    """
    usa_precios = any(
        str(nombre).startswith("precio") or nombre == "tiene_precio" for nombre in variables
    )
    supuestos = {
        "calendario": "Día de la semana, mes, semana del año, eventos y SNAP se conocen por adelantado.",
        "snap": "Flags reales del calendario para el estado de cada tienda; no son promociones.",
        "recorte": "Las predicciones negativas se recortan a cero (también al evaluar).",
        "redondeo": (
            "No se redondea antes de calcular las métricas ni al publicar: la serie canónica "
            "conserva decimales (política decimal-v1) y el redondeo se aplica solo al presentar."
        ),
        "promociones": "M5 no contiene promociones: los cambios de precio no se etiquetan como campañas.",
    }
    if usa_precios:
        supuestos["precio_futuro"] = (
            "El precio de los 28 días pronosticados es el último precio conocido en la semana "
            "del origen (no se usan precios futuros del archivo)."
        )
    else:
        supuestos["precio_futuro"] = (
            "No usa precios: esta variante no incluye variables de precio, así que no congela ni "
            "supone precios futuros."
        )
    return supuestos


def _documento_run(
    *,
    run_id: str,
    version: str,
    version_precios: str | None,
    variante: str,
    modelo_entrenado: dict,
    origenes_entrenamiento: list[int],
    cobertura: dict,
    duracion_s: float,
    memoria: dict,
    artefacto: str,
    estado: str,
    metricas_resumen: dict | None = None,
    recomendado: bool = False,
) -> dict:
    return {
        "_id": run_id,
        "esquema": "m5-modelo-1",
        "run_id": run_id,
        "version": version,
        "version_precios": version_precios,
        "variante": variante,
        "etiqueta": modelo.VARIANTES[variante]["etiqueta"],
        "metodo": "LightGBM global condicionado al horizonte (árboles de decisión)",
        "descripcion": modelo.VARIANTES[variante]["descripcion"],
        "estado": estado,
        "recomendado": recomendado,
        "semilla": modelo.SEMILLA,
        "horizonte": modelo.HORIZONTE,
        "origenes_entrenamiento": origenes_entrenamiento,
        "ventana_entrenamiento": {
            "desde": "historia completa disponible hasta el origen de cada fila",
            "hasta": origenes_entrenamiento[-1] if origenes_entrenamiento else None,
            "filas": modelo_entrenado["filas_entrenamiento"],
            "fraccion_ceros_muestreada": modelo_entrenado["fraccion_ceros"],
            "nota": (
                "Solo filas con t ≤ origen: los rezagos, medias móviles y precios usan "
                "exclusivamente información conocida al emitir el pronóstico."
            ),
        },
        "variables": list(modelo_entrenado["variables"]),
        "importancias": modelo_entrenado["importancias"],
        "parametros": modelo_entrenado["parametros"],
        "num_boost_round": modelo_entrenado["num_boost_round"],
        "firma_configuracion": modelo.firma_configuracion(
            modelo_entrenado, (cobertura or {}).get("series")
        ),
        "supuestos": _supuestos(modelo_entrenado["variables"]),
        "cobertura": cobertura,
        "metricas_resumen": metricas_resumen,
        "artefacto": artefacto,
        "duracion_s": round(duracion_s, 1),
        "memoria": memoria,
        "entorno": {
            "python": platform.python_version(),
            "plataforma": platform.platform(),
            "numpy": np.__version__,
        },
        "creado_en": datetime.now(timezone.utc),
    }


OBJETIVO = "tweedie"
"""Objetivo de LightGBM. Se comparó con ``tweedie`` en una muestra y se conserva el
que dio mejor WAPE de desarrollo; el valor se documenta en la ejecución."""


def _entrenar_variante(
    ctx: modelo.Contexto,
    variante: str,
    origenes: list[int],
    *,
    num_boost_round: int = 320,
    objetivo: str | None = None,
) -> dict:
    return modelo.entrenar(
        ctx,
        origenes,
        con_precios=modelo.VARIANTES[variante]["precios"],
        num_boost_round=num_boost_round,
        objetivo=objetivo or OBJETIVO,
        log=_log,
    )


def _evaluar(
    ctx: modelo.Contexto,
    *,
    modelo_entrenado: dict | None,
    origenes: list[int],
    etiquetas: dict[str, np.ndarray],
    usar_baseline: bool = False,
    matrices: dict | None = None,
) -> list[dict]:
    """Evalúa en cada origen comparando candidato y baseline en las mismas series.

    ``matrices`` (opcional) devuelve las predicciones de cada origen para publicarlas
    como cortes históricos sin volver a calcularlas.
    """
    resultados = []
    for origen in origenes:
        baseline = modelo.predecir_baseline(ctx, origen)
        if usar_baseline or modelo_entrenado is None:
            candidato = baseline.copy()
            cobertura = {"series": int(ctx.n_series), "series_modelo": 0, "fallback": 0}
        else:
            candidato, cobertura = modelo.predecir(
                ctx, modelo_entrenado, origen, baseline=baseline
            )
        evaluacion = modelo.evaluar_origen(
            ctx,
            origen=origen,
            prediccion=candidato,
            baseline=baseline,
            etiquetas=etiquetas,
        )
        evaluacion["cobertura"] = cobertura
        resultados.append(evaluacion)
        if matrices is not None:
            matrices[origen] = (candidato, baseline)
    return resultados


def _resumen_evaluaciones(evaluaciones: list[dict], clave: str) -> dict:
    """Métricas globales sumando errores absolutos y denominadores."""
    suma_abs = 0.0
    suma_error = 0.0
    denominador = 0.0
    n = 0
    for evaluacion in evaluaciones:
        metricas = evaluacion[clave]
        if metricas["n"] == 0:
            continue
        n += metricas["n"]
        suma_abs += metricas["mae"] * metricas["n"]
        suma_error += metricas["sesgo"] * metricas["n"]
        denominador += metricas["denominador"]
    if n == 0:
        return {"n": 0, "mae": None, "sesgo": None, "wape": None, "denominador": 0, "ventanas": len(evaluaciones)}
    return {
        "n": n,
        "mae": round(suma_abs / n, 4),
        "sesgo": round(suma_error / n, 4),
        "wape": round(100 * suma_abs / denominador, 4) if denominador > 0 else None,
        "denominador": int(round(denominador)),
        "ventanas": len(evaluaciones),
    }


def _metricas_desarrollo(db, version: str, variante: str) -> dict | None:
    """Métricas de desarrollo de la ejecución guardada de esa variante (universo completo)."""
    for documento in db[M5_MODELOS].find(
        {"version": version, "variante": variante, "estado": "desarrollo"}
    ):
        resumen = (documento.get("metricas_resumen") or {}).get("desarrollo")
        cobertura = documento.get("cobertura") or {}
        if resumen and cobertura.get("series") == 30490:
            return resumen
    return None


def _guardar_evaluaciones(
    db, run_id: str, version: str, variante: str, evaluaciones: list[dict], *, etapa: str
) -> None:
    """Reemplaza la evaluación de la ejecución (idempotente por run_id)."""
    db[M5_EVALUACION_MODELOS].delete_many({"run_id": run_id, "etapa": etapa})
    documentos = []
    for evaluacion in evaluaciones:
        documentos.append(
            {
                "_id": f"{run_id}:{etapa}:{evaluacion['origen_d']}",
                "esquema": "m5-evaluacion-modelo-1",
                "run_id": run_id,
                "version": version,
                "variante": variante,
                "etapa": etapa,
                "origen_d": evaluacion["origen_d"],
                "origen_fecha": evaluacion["origen_fecha"],
                "fin_fecha": evaluacion["fin_fecha"],
                "series": evaluacion["series"],
                "observaciones": evaluacion["observaciones"],
                "candidato": evaluacion["candidato"],
                "baseline": evaluacion["baseline"],
                "mejora_pct": evaluacion["mejora_pct"],
                "mejora_mae_pct": evaluacion["mejora_mae_pct"],
                "por_horizonte": evaluacion["por_horizonte"],
                "segmentos": evaluacion["segmentos"],
                "segmentos_mejoran": evaluacion["segmentos_mejoran"],
                "segmentos_empeoran": evaluacion["segmentos_empeoran"],
            }
        )
    if documentos:
        db[M5_EVALUACION_MODELOS].insert_many(documentos, ordered=False)


def _publicar_predicciones(
    db,
    ctx: modelo.Contexto,
    *,
    run_id: str,
    version: str,
    origen: int,
    candidato: np.ndarray,
    baseline: np.ndarray,
    tipo: str,
) -> dict:
    """Guarda las predicciones agregadas y, si es el origen final, las de cada serie."""
    dias = [
        {"d": origen + paso, "fecha": ctx.fecha_de_dia[origen + paso - 1].isoformat()}
        for paso in range(1, modelo.HORIZONTE + 1)
    ]
    dimensiones = modelo.agregar_por_dimension(ctx, np.round(candidato))
    base_dimensiones = modelo.agregar_por_dimension(ctx, np.round(baseline))
    observado = ctx.ventas[:, origen : origen + modelo.HORIZONTE]
    obs_dimensiones = modelo.agregar_por_dimension(ctx, observado)

    db[M5_PREDICCIONES].delete_many({"run_id": run_id, "origen_d": origen})
    documentos = []
    for dimension, claves in dimensiones.items():
        for clave, valores in claves.items():
            observados = obs_dimensiones[dimension].get(clave)
            documentos.append(
                {
                    "_id": f"{run_id}:{origen}:{dimension}:{clave}",
                    "esquema": "m5-prediccion-1",
                    "run_id": run_id,
                    "version": version,
                    "tipo": tipo,
                    "origen_d": origen,
                    "origen_fecha": ctx.fecha_de_dia[origen - 1].isoformat(),
                    "dimension": dimension,
                    "clave": clave,
                    "dias": dias,
                    "pronostico": [int(round(v)) for v in valores],
                    "baseline": [int(round(v)) for v in base_dimensiones[dimension][clave]],
                    "observado": [int(v) for v in observados] if origen + modelo.HORIZONTE <= ctx.n_dias else None,
                    "unidad": "unidades vendidas",
                }
            )
    if documentos:
        db[M5_PREDICCIONES].insert_many(documentos, ordered=False)

    if tipo == "final":
        db[M5_PREDICCIONES_SERIES].delete_many({"run_id": run_id})
        documentos_series = []
        for indice, (item, tienda) in enumerate(ctx.series):
            documentos_series.append(
                {
                    "_id": f"{run_id}:{tienda}:{item}",
                    "esquema": "m5-prediccion-serie-1",
                    "run_id": run_id,
                    "version": version,
                    "store_id": tienda,
                    "item_id": item,
                    "item_index": int(ctx.item_idx[indice]),
                    "store_index": int(ctx.store_idx[indice]),
                    "origen_d": origen,
                    "origen_fecha": ctx.fecha_de_dia[origen - 1].isoformat(),
                    "pronostico": [int(round(v)) for v in candidato[indice]],
                    "baseline": [int(round(v)) for v in baseline[indice]],
                    "metodo": "modelo",
                }
            )
        for posicion in range(0, len(documentos_series), 2000):
            db[M5_PREDICCIONES_SERIES].insert_many(
                documentos_series[posicion : posicion + 2000], ordered=False
            )
    return {"documentos": len(documentos), "dias": len(dias), "tipo": tipo}


def _publicar_modelo(db, documento: dict) -> None:
    db[M5_MODELOS].replace_one({"_id": documento["_id"]}, documento, upsert=True)


def _marcar_recomendado(db, *, version: str, run_id: str) -> None:
    db[M5_MODELOS].update_many(
        {"version": version, "recomendado": True, "run_id": {"$ne": run_id}},
        {"$set": {"recomendado": False}},
    )
    db[M5_MODELOS].update_one({"_id": run_id}, {"$set": {"recomendado": True}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Entrena y evalúa el modelo a 28 días.")
    parser.add_argument("--piloto", action="store_true", help="Piloto técnico acotado.")
    parser.add_argument("--variantes", action="store_true", help="Desarrollo: variantes candidatas.")
    parser.add_argument("--final", action="store_true", help="Cortes reservados y pronóstico final.")
    parser.add_argument("--todo", action="store_true", help="Desarrollo + final + publicación.")
    parser.add_argument("--estado", action="store_true", help="Ejecuciones guardadas.")
    parser.add_argument("--sin-cache", action="store_true", help="No reutilizar la matriz de ventas.")
    parser.add_argument("--rondas", type=int, default=320, help="Iteraciones de boosting.")
    parser.add_argument(
        "--muestra",
        type=int,
        default=None,
        help="Solo pruebas técnicas: restringe a N series (muestra sistemática).",
    )
    parser.add_argument(
        "--objetivo",
        default="tweedie",
        choices=("regression_l2", "tweedie"),
        help="Objetivo de LightGBM (se documenta en la ejecución).",
    )
    args = parser.parse_args(argv)
    global OBJETIVO
    OBJETIVO = args.objetivo

    settings = get_settings()
    base = init_database(settings)
    db = base.m5_db
    version = ingesta.version_actual(db)
    if not version:
        _json({"estado": "pendiente", "mensaje": "Datos de ventas pendientes de importar"})
        return FALLO

    if args.estado:
        runs = list(db[M5_MODELOS].find({}, {"parametros": 0}).sort("creado_en", -1))
        _json({"version": version, "ejecuciones": runs})
        return OK

    manifiesto_precios = precios_mod.manifiesto_vigente(db)
    version_precios = manifiesto_precios["version_precios"] if manifiesto_precios else None
    if not version_precios:
        _json({"estado": "sin_precios", "mensaje": "Importa los precios antes de entrenar con precio"})
        return FALLO

    inicio = time.perf_counter()
    ctx = modelo.cargar_contexto(
        db, version, version_precios=version_precios, usar_cache=not args.sin_cache, log=_log
    )
    logger.info("Contexto cargado: %s", ctx.metricas_carga)
    if args.muestra:
        indices = np.arange(0, ctx.n_series, max(1, ctx.n_series // args.muestra))[: args.muestra]
        ctx = _matriz(ctx, indices)
        logger.info("Muestra técnica de %s series (solo para comparar configuraciones)", ctx.n_series)

    publicar = args.muestra is None

    if args.piloto:
        indices = _subconjunto_piloto(ctx)
        ctx_piloto = _matriz(ctx, indices)
        logger.info("Piloto con %s series", ctx_piloto.n_series)
        etiquetas = modelo.etiquetas_serie(ctx_piloto)
        origenes_entrenamiento = [1541, 1621, 1701, 1745]
        variante = "precios"
        modelo_entrenado = _entrenar_variante(
            ctx_piloto, variante, origenes_entrenamiento, num_boost_round=min(args.rondas, 120)
        )
        evaluaciones = _evaluar(
            ctx_piloto,
            modelo_entrenado=modelo_entrenado,
            origenes=modelo.ORIGENES_DESARROLLO,
            etiquetas=etiquetas,
        )
        run_id = modelo.crear_run_id(
            version=version,
            version_precios=version_precios,
            variante=variante,
            firma=modelo.firma_configuracion(modelo_entrenado, ctx_piloto.n_series) + "-piloto",
        )
        salida = {
            "piloto": True,
            "run_id": run_id,
            "series": ctx_piloto.n_series,
            "entrenamiento": {
                "filas": modelo_entrenado["filas_entrenamiento"],
                "duracion_s": modelo_entrenado["duracion_s"],
                "memoria_mb": modelo_entrenado["memoria_mb"],
            },
            "evaluacion": [
                {
                    "origen": evaluacion["origen_d"],
                    "candidato_wape": evaluacion["candidato"]["wape"],
                    "baseline_wape": evaluacion["baseline"]["wape"],
                    "mejora_pct": evaluacion["mejora_pct"],
                }
                for evaluacion in evaluaciones
            ],
            "duracion_total_s": round(time.perf_counter() - inicio, 1),
            "nota": "Piloto técnico: no es el resultado del universo M5.",
        }
        _json(salida)
        return OK

    if not (args.variantes or args.final or args.todo):
        parser.print_help()
        return FALLO

    etiquetas = modelo.etiquetas_serie(ctx)
    origenes_dev = list(modelo.ORIGENES_DESARROLLO)
    origenes_final = list(modelo.ORIGENES_FINALES)
    entrenamiento_dev = [origen for origen in (1541, 1609, 1677, 1745)]
    ultimo = ctx.n_dias

    resultados: dict = {"version": version, "version_precios": version_precios, "variantes": {}}

    # --- Baseline de referencia (sin entrenamiento) ------------------------
    if args.todo or args.variantes:
        baseline_dev = _evaluar(
            ctx,
            modelo_entrenado=None,
            origenes=origenes_dev,
            etiquetas=etiquetas,
            usar_baseline=True,
        )
        run_id_base = modelo.crear_run_id(
            version=version, version_precios=version_precios, variante="baseline", firma="estacional-semanal"
        )
        resumen_base = {
            "desarrollo": _resumen_evaluaciones(baseline_dev, "baseline"),
            "final": _resumen_evaluaciones(
                _evaluar(
                    ctx,
                    modelo_entrenado=None,
                    origenes=origenes_final,
                    etiquetas=etiquetas,
                    usar_baseline=True,
                ),
                "baseline",
            ),
        }
        _guardar_evaluaciones(db, run_id_base, version, "baseline", baseline_dev, etapa="desarrollo")
        _publicar_modelo(
            db,
            {
                "_id": run_id_base,
                "esquema": "m5-modelo-1",
                "run_id": run_id_base,
                "version": version,
                "version_precios": version_precios,
                "variante": "baseline",
                "etiqueta": "Baseline estacional semanal",
                "metodo": "Ingenuo estacional semanal (repite los últimos 7 días observados)",
                "descripcion": "Referencia obligatoria: no usa variables exógenas ni entrenamiento.",
                "estado": "publicada",
                "recomendado": False,
                "semilla": None,
                "horizonte": modelo.HORIZONTE,
                "origenes_entrenamiento": [],
                "ventana_entrenamiento": {"filas": 0, "nota": "No entrena: es una regla fija."},
                "variables": [],
                "importancias": [],
                "parametros": {},
                "supuestos": {"precio_futuro": "No usa precios.", "recorte": "No genera negativos."},
                "cobertura": {"series": int(ctx.n_series), "metodo": "baseline"},
                "metricas_resumen": resumen_base,
                "artefacto": None,
                "duracion_s": None,
                "memoria": {},
                "entorno": {"python": platform.python_version(), "numpy": np.__version__},
                "creado_en": datetime.now(timezone.utc),
            },
        )
        resultados["variantes"]["baseline"] = resumen_base

    # --- Variantes candidatas: desarrollo ---------------------------------
    if args.variantes or args.todo:
        for variante in modelo.VARIANTES:
            logger.info("Entrenando variante %s (desarrollo)", variante)
            modelo_entrenado = _entrenar_variante(
                ctx, variante, entrenamiento_dev, num_boost_round=args.rondas
            )
            evaluaciones = _evaluar(
                ctx,
                modelo_entrenado=modelo_entrenado,
                origenes=origenes_dev,
                etiquetas=etiquetas,
            )
            run_id = modelo.crear_run_id(
                version=version,
                version_precios=version_precios,
                variante=variante,
                firma=modelo.firma_configuracion(modelo_entrenado, ctx.n_series),
            )
            if publicar:
                _guardar_evaluaciones(db, run_id, version, variante, evaluaciones, etapa="desarrollo")
            _publicar_modelo(
                db,
                _documento_run(
                    run_id=run_id,
                    version=version,
                    version_precios=version_precios,
                    variante=variante,
                    modelo_entrenado=modelo_entrenado,
                    origenes_entrenamiento=entrenamiento_dev,
                    cobertura={
                        "series": int(ctx.n_series),
                        "series_modelo": int(ctx.n_series),
                        "fallback": 0,
                        "nota": "El modelo predice todas las series del universo (incluidas las de ventas cero).",
                    },
                    duracion_s=modelo_entrenado["duracion_s"],
                    memoria={"entrenamiento_mb": modelo_entrenado["memoria_mb"], **modelo.memoria_mb()},
                    artefacto=str(_guardar_modelo(modelo_entrenado, run_id)),
                    estado="desarrollo",
                    metricas_resumen={
                        "desarrollo": _resumen_evaluaciones(evaluaciones, "candidato"),
                        "baseline_desarrollo": _resumen_evaluaciones(evaluaciones, "baseline"),
                    },
                ),
            )
            resultados["variantes"][variante] = {
                "candidato": _resumen_evaluaciones(evaluaciones, "candidato"),
                "baseline": _resumen_evaluaciones(evaluaciones, "baseline"),
                "mejora_por_ventana": [
                    {
                        "origen": evaluacion["origen_d"],
                        "mejora_pct": evaluacion["mejora_pct"],
                    }
                    for evaluacion in evaluaciones
                ],
                "importancias": modelo_entrenado["importancias"],
                "duracion_s": modelo_entrenado["duracion_s"],
                "filas": modelo_entrenado["filas_entrenamiento"],
            }

    if args.final or args.todo:
        # Variante recomendada: la que tenga mejor WAPE en desarrollo; si ninguna
        # mejora al baseline, se recomienda el baseline. Si no se entrenaron variantes
        # en esta invocación, se leen las ejecuciones de desarrollo ya guardadas.
        recomendada = "baseline"
        mejor = None
        wape_base = resultados["variantes"].get("baseline", {}).get("desarrollo", {}).get("wape")
        if resultados["variantes"]:
            for variante, datos in resultados["variantes"].items():
                if variante == "baseline":
                    continue
                wape = datos["candidato"]["wape"]
                if wape is not None and (mejor is None or wape < mejor):
                    mejor = wape
                    recomendada = variante
        else:
            for documento in db[M5_MODELOS].find(
                {"version": version, "estado": "desarrollo", "variante": {"$ne": "baseline"}}
            ):
                resumen = (documento.get("metricas_resumen") or {}).get("desarrollo") or {}
                wape = resumen.get("wape")
                baseline_resumen = (documento.get("metricas_resumen") or {}).get(
                    "baseline_desarrollo"
                ) or {}
                if wape_base is None:
                    wape_base = baseline_resumen.get("wape")
                if wape is not None and (mejor is None or wape < mejor):
                    mejor = wape
                    recomendada = documento["variante"]
        if mejor is not None and wape_base is not None and mejor >= wape_base:
            recomendada = "baseline"
        logger.info("Variante recomendada: %s", recomendada)
        resultados["recomendada"] = recomendada

        origenes_entrenamiento_final = [origen for origen in (1621, 1677, 1745, 1829, 1857)]
        if recomendada == "baseline":
            evaluaciones_final = _evaluar(
                ctx,
                modelo_entrenado=None,
                origenes=origenes_final,
                etiquetas=etiquetas,
                usar_baseline=True,
            )
            run_id = modelo.crear_run_id(
                version=version, version_precios=version_precios, variante="baseline", firma="estacional-semanal"
            )
            _guardar_evaluaciones(db, run_id, version, "baseline", evaluaciones_final, etapa="final")
            _marcar_recomendado(db, version=version, run_id=run_id)
            # Pronóstico final del baseline desde el último día observado.
            baseline_final = modelo.predecir_baseline(ctx, ultimo)
            _publicar_predicciones(
                db,
                ctx,
                run_id=run_id,
                version=version,
                origen=ultimo,
                candidato=baseline_final,
                baseline=baseline_final,
                tipo="final",
            )
            resultados["final"] = {
                "recomendada": "baseline",
                "evaluacion": [
                    {
                        "origen": evaluacion["origen_d"],
                        "wape": evaluacion["baseline"]["wape"],
                        "mae": evaluacion["baseline"]["mae"],
                    }
                    for evaluacion in evaluaciones_final
                ],
            }
        else:
            logger.info("Entrenando modelo final (%s) con datos hasta el último origen", recomendada)
            modelo_entrenado = _entrenar_variante(
                ctx, recomendada, origenes_entrenamiento_final, num_boost_round=args.rondas
            )
            matrices_final: dict = {}
            evaluaciones_final = _evaluar(
                ctx,
                modelo_entrenado=modelo_entrenado,
                origenes=origenes_final,
                etiquetas=etiquetas,
                matrices=matrices_final,
            )
            run_id = modelo.crear_run_id(
                version=version,
                version_precios=version_precios,
                variante=recomendada,
                firma=modelo.firma_configuracion(modelo_entrenado, ctx.n_series),
            )
            _guardar_evaluaciones(db, run_id, version, recomendada, evaluaciones_final, etapa="final")
            baseline_final = modelo.predecir_baseline(ctx, ultimo)
            candidato_final, cobertura_final = modelo.predecir(
                ctx, modelo_entrenado, ultimo, baseline=baseline_final
            )
            publicacion = _publicar_predicciones(
                db,
                ctx,
                run_id=run_id,
                version=version,
                origen=ultimo,
                candidato=candidato_final,
                baseline=baseline_final,
                tipo="final",
            )
            _publicar_modelo(
                db,
                _documento_run(
                    run_id=run_id,
                    version=version,
                    version_precios=version_precios,
                    variante=recomendada,
                    modelo_entrenado=modelo_entrenado,
                    origenes_entrenamiento=origenes_entrenamiento_final,
                    cobertura={
                        **cobertura_final,
                        "nota": (
                            "El modelo pronostica las 30 490 series del universo, incluidas las "
                            "de ventas cero. Si alguna serie produjera una predicción no finita se "
                            "usa el baseline para esa serie y se declara en el conteo de fallback."
                        ),
                    },
                    duracion_s=modelo_entrenado["duracion_s"],
                    memoria={"entrenamiento_mb": modelo_entrenado["memoria_mb"], **modelo.memoria_mb()},
                    artefacto=str(_guardar_modelo(modelo_entrenado, run_id)),
                    estado="publicada",
                    metricas_resumen={
                        "desarrollo": _metricas_desarrollo(db, version, recomendada),
                        "final": _resumen_evaluaciones(evaluaciones_final, "candidato"),
                        "baseline_final": _resumen_evaluaciones(evaluaciones_final, "baseline"),
                    },
                ),
            )
            # Cortes históricos publicados: dejan ver el observado posterior al corte.
            for origen_eval, (cand_eval, base_eval) in matrices_final.items():
                _publicar_predicciones(
                    db,
                    ctx,
                    run_id=run_id,
                    version=version,
                    origen=origen_eval,
                    candidato=cand_eval,
                    baseline=base_eval,
                    tipo="evaluacion",
                )
            _marcar_recomendado(db, version=version, run_id=run_id)
            resultados["final"] = {
                "recomendada": recomendada,
                "run_id": run_id,
                "publicacion": publicacion,
                "candidato": _resumen_evaluaciones(evaluaciones_final, "candidato"),
                "baseline": _resumen_evaluaciones(evaluaciones_final, "baseline"),
                "evaluacion": [
                    {
                        "origen": evaluacion["origen_d"],
                        "candidato_wape": evaluacion["candidato"]["wape"],
                        "baseline_wape": evaluacion["baseline"]["wape"],
                        "mejora_pct": evaluacion["mejora_pct"],
                        "segmentos_mejoran": evaluacion["segmentos_mejoran"],
                        "segmentos_empeoran": evaluacion["segmentos_empeoran"],
                    }
                    for evaluacion in evaluaciones_final
                ],
            }

    resultados["duracion_total_s"] = round(time.perf_counter() - inicio, 1)
    _json(resultados)
    return OK


if __name__ == "__main__":
    sys.exit(main())
