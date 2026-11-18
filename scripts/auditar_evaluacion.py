"""Auditoría independiente de la evaluación del pronóstico publicado.

Recalcula **desde las predicciones y las observaciones por artículo × tienda × fecha × corte**
todas las métricas que publica la aplicación, y las contrasta con lo que sirven la API y el
almacén. No reutiliza el resumen que consume la interfaz: vuelve a predecir con el artefacto
publicado (sin reentrenar), reconstruye el observado desde la matriz de ventas importada y aplica
las fórmulas con numpy.

Qué comprueba:

A. Detalle diario (series × días): MAE, WAPE y sesgo.
B. Horizonte de 28 días por artículo × tienda: primero se suman los 28 días de cada serie, luego se
   miden MAE_28, WAPE_28 y sesgo_28.
C. Agregados diarios por segmento (total, estado, tienda, categoría, departamento): primero se suman
   predicción y observado por fecha y segmento, después se mide.
D. Consolidación entre cortes: se suman numeradores y denominadores, nunca se promedian
   porcentajes; candidato y baseline se miden sobre exactamente las mismas observaciones.

Además:

* comprueba unicidad de claves dentro de cada revisión y corte (sin doble conteo entre la
  publicación decimal y la anterior);
* compara las cifras guardadas (`m5_evaluacion_modelos`, `m5_predicciones`) con el recálculo;
* distingue lo que cambia por la política de precisión de lo que cambiaría por duplicados.

Uso (desde ``backend/``):

    python -m scripts.auditar_evaluacion                # informe en consola
    python -m scripts.auditar_evaluacion --json salida.json
    python -m scripts.auditar_evaluacion --sin-recalculo  # solo contrastes de lo guardado

Sale con código 1 si alguna cifra publicada no coincide con el recálculo dentro de la tolerancia
declarada (1e-6 relativo para las métricas, 0,51 u/día para la publicación en enteros).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

import numpy as np

from app.comercial import modelo
from app.config import get_settings
from pymongo import ASCENDING

from app.db.mongo import (
    M5_CATALOGO,
    M5_EVALUACION_MODELOS,
    M5_EVALUACION_SERIES,
    M5_MODELOS,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    init_database,
)

LOGGER = logging.getLogger("auditar_evaluacion")

#: Tolerancia relativa de comparación de métricas recalculadas frente a las publicadas.
TOLERANCIA = 1e-6
#: Las métricas guardadas se redondean a cuatro decimales al publicarse: medio dígito de margen.
TOLERANCIA_ABSOLUTA = 5e-5


def _coincide(guardado: float, recalculado: float) -> bool:
    """Compara una cifra publicada con la recalculada dentro de las tolerancias declaradas."""
    margen = max(TOLERANCIA_ABSOLUTA, TOLERANCIA * abs(float(guardado)))
    return abs(float(guardado) - float(recalculado)) <= margen

DIMENSIONES = ("total", "estado", "tienda", "categoria", "departamento")


def _metricas(error: np.ndarray, observado: np.ndarray) -> dict[str, Any]:
    """MAE, WAPE y sesgo de errores ya alineados (``error = pronóstico − observado``)."""
    error = np.asarray(error, dtype=np.float64).reshape(-1)
    observado = np.asarray(observado, dtype=np.float64).reshape(-1)
    valido = np.isfinite(error) & np.isfinite(observado)
    error, observado = error[valido], observado[valido]
    if error.size == 0:
        return {"n": 0, "mae": None, "sesgo": None, "wape": None, "denominador": 0.0}
    denominador = float(np.sum(np.abs(observado)))
    return {
        "n": int(error.size),
        "mae": float(np.mean(np.abs(error))),
        "sesgo": float(np.mean(error)),
        "wape": (100.0 * float(np.sum(np.abs(error))) / denominador) if denominador > 0 else None,
        "denominador": denominador,
    }


def _consolidar(metricas_por_corte: list[dict[str, Any]]) -> dict[str, Any]:
    """Consolida varios cortes sumando numeradores y denominadores (no promedia porcentajes).

    ``mae`` se obtiene de ``Σ|error| / Σn`` y ``wape`` de ``Σ|error| / Σobservado``; ambas se
    reconstruyen desde ``mae``, ``n`` y ``wape``, ``denominador`` de cada corte.
    """
    n = sum(int(m["n"]) for m in metricas_por_corte)
    suma_abs = sum(float(m["mae"]) * int(m["n"]) for m in metricas_por_corte if m["mae"] is not None)
    suma_error = sum(
        float(m["sesgo"]) * int(m["n"]) for m in metricas_por_corte if m["sesgo"] is not None
    )
    denominador = sum(float(m["denominador"]) for m in metricas_por_corte)
    return {
        "n": n,
        "mae": suma_abs / n if n else None,
        "sesgo": suma_error / n if n else None,
        "wape": (100.0 * suma_abs / denominador) if denominador > 0 else None,
        "denominador": denominador,
        "ventanas": len(metricas_por_corte),
    }


#: Tolerancias de presentación alineadas con ``src/metricas.ts``.
TOLERANCIA_IGUALDAD_ABSOLUTA = 5e-5
TOLERANCIA_IGUALDAD_RELATIVA = 1e-9
UMBRAL_DIFERENCIA_PEQUENA_PCT = 0.05


def _mejora(modelo_m: dict[str, Any], baseline_m: dict[str, Any]) -> dict[str, Any]:
    """Mejora relativa con signo: ``(error_baseline − error_modelo) / error_baseline``.

    Distingue igualdad **numérica** (los errores coinciden dentro de la precisión publicada) de
    **diferencia relativa pequeña** (no son iguales, pero la mejora no alcanza 0,05 %). La misma
    regla que aplica la interfaz, para que el informe y las pantallas no discrepen.
    """
    base = baseline_m.get("mae")
    cand = modelo_m.get("mae")
    if base is None or cand is None:
        return {"tipo": "no_calculable", "pct": None, "motivo": "MAE no calculable"}
    base = float(base)
    cand = float(cand)
    if base == 0 and cand == 0:
        return {
            "tipo": "no_calculable",
            "pct": None,
            "motivo": "ambos errores son cero: la mejora porcentual no está definida",
        }
    margen = max(TOLERANCIA_IGUALDAD_ABSOLUTA, TOLERANCIA_IGUALDAD_RELATIVA * max(abs(base), abs(cand)))
    if abs(cand - base) <= margen:
        return {
            "tipo": "sin_diferencia_numerica",
            "pct": 0.0,
            "motivo": "mismo error dentro de la precisión del cálculo",
        }
    if base == 0:
        return {
            "tipo": "no_calculable",
            "pct": None,
            "diferencia_absoluta": cand - base,
            "motivo": "el baseline tiene error cero: no hay porcentaje ni infinito",
        }
    pct = 100.0 * (base - cand) / base
    if abs(pct) < UMBRAL_DIFERENCIA_PEQUENA_PCT - 1e-12:
        return {
            "tipo": "diferencia_pequena",
            "pct": pct,
            "motivo": "por debajo del umbral de 0,05 %; no se declara empate",
        }
    return {"tipo": "mejor" if pct > 0 else "peor", "pct": pct}


def _cargar(db, version: str, run_id: str, revision: str | None):
    """Contexto de ventas y artefacto de la ejecución publicada."""
    run = db[M5_MODELOS].find_one({"run_id": run_id, "version": version})
    if not run:
        raise SystemExit(f"Sin manifiesto para {run_id} en {version}")
    ctx = modelo.cargar_contexto(
        db, version, version_precios=run.get("version_precios"), usar_cache=True
    )
    import lightgbm as lgb

    booster = lgb.Booster(model_file=run["artefacto"])
    modelo_dict = {
        "booster": booster,
        "variables": run["variables"],
        "horizonte": int(run.get("horizonte", modelo.HORIZONTE)),
        "num_boost_round": run.get("num_boost_round"),
    }
    return ctx, modelo_dict, run


def _series_ordenadas(db, version: str) -> tuple[list[tuple[str, str]], dict[str, str], dict[str, str]]:
    """Mismo orden serie × (tienda, artículo) que usa el contexto del modelo.

    El contexto ordena los artículos alfabéticamente y, para cada artículo, las diez tiendas.
    Reconstruirlo aquí es lo que permite atribuir cada fila de la matriz a su tienda, su artículo,
    su categoría y su departamento sin depender del resumen publicado.
    """
    articulos = [
        documento["item_id"]
        for documento in db[M5_CATALOGO]
        .find({"version": version, "tipo": "producto"}, {"item_id": 1, "_id": 0})
        .sort("item_id", ASCENDING)
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


def _claves_duplicadas(db, version: str, revisiones: list[str | None]) -> dict[str, Any]:
    """Comprueba que no haya claves repetidas dentro de una misma revisión y corte."""
    salida: dict[str, Any] = {}
    for revision in revisiones:
        filtro = {"version": version}
        filtro["revision"] = revision if revision else {"$exists": False}
        etiqueta = revision or "enteros-legado"
        for coleccion in (M5_PREDICCIONES, M5_PREDICCIONES_SERIES, M5_EVALUACION_SERIES):
            claves = {
                M5_PREDICCIONES: ["origen_d", "dimension", "clave"],
                M5_PREDICCIONES_SERIES: ["origen_d", "store_id", "item_id"],
                M5_EVALUACION_SERIES: ["origen_d", "store_id", "item_id"],
            }[coleccion]
            agrupado = list(
                db[coleccion].aggregate(
                    [
                        {"$match": filtro},
                        {"$group": {"_id": {c: f"${c}" for c in claves}, "n": {"$sum": 1}}},
                        {"$match": {"n": {"$gt": 1}}},
                        {"$limit": 5},
                    ]
                )
            )
            total = db[coleccion].count_documents(filtro)
            salida[f"{coleccion}:{etiqueta}"] = {
                "documentos": total,
                "claves_repetidas": len(agrupado),
                "ejemplos": [registro["_id"] for registro in agrupado],
            }
    return salida


def auditar(db, *, recalcular: bool = True) -> dict[str, Any]:
    estado = db["m5_estado"].find_one({"publicada": True}) or db["m5_estado"].find_one()
    if not estado:
        raise SystemExit("Sin versión publicada")
    version = estado["version"]
    publicacion = db["m5_publicacion"].find_one({"activa": True}) or {}
    run = db[M5_MODELOS].find_one({"version": version, "recomendado": True})
    if not run:
        raise SystemExit("Sin ejecución publicada")
    run_id = run["run_id"]
    revision = publicacion.get("_id")

    cortes = sorted(
        int(d["origen_d"])
        for d in db[M5_EVALUACION_MODELOS].find(
            {"version": version, "run_id": run_id, "etapa": "final"}, {"origen_d": 1}
        )
    )

    informe: dict[str, Any] = {
        "version": version,
        "run_id": run_id,
        "revision": revision,
        "politica": publicacion.get("politica"),
        "cortes": cortes,
        "duplicados": _claves_duplicadas(db, version, [revision]),
        "comparaciones": [],
        "diferencias": [],
    }

    if not cortes:
        informe["aviso"] = "sin cortes finales publicados"
        return informe

    if not recalcular:
        informe["aviso"] = "solo contrastes de lo guardado (--sin-recalculo)"
        return informe

    ctx, modelo_dict, _ = _cargar(db, version, run_id, revision)
    series, cat_de, dept_de = _series_ordenadas(db, version)
    if len(series) != ctx.n_series:
        raise SystemExit(
            f"El catálogo ({len(series)}) y la matriz de ventas ({ctx.n_series}) no coinciden"
        )
    tiendas = np.array([tienda for tienda, _ in series])
    articulos = np.array([articulo for _, articulo in series])
    categorias = np.array([cat_de.get(articulo) for articulo in articulos], dtype=object)
    departamentos = np.array([dept_de.get(articulo) for articulo in articulos], dtype=object)

    acumulado: dict[str, list[dict[str, Any]]] = {}
    for corte in cortes:
        if corte + modelo.HORIZONTE > ctx.n_dias:
            informe["diferencias"].append(
                f"corte d_{corte}: el horizonte excede el histórico disponible"
            )
            continue
        pred, _ = modelo.predecir(
            ctx, modelo_dict, corte, baseline=modelo.predecir_baseline(ctx, corte)
        )
        base = modelo.predecir_baseline(ctx, corte)
        obs = ctx.ventas[:, corte : corte + modelo.HORIZONTE].astype(np.float64)
        pred = np.asarray(pred, dtype=np.float64)
        base = np.asarray(base, dtype=np.float64)

        # A. Detalle diario (serie × día).
        diario_c = _metricas(pred - obs, obs)
        diario_b = _metricas(base - obs, obs)
        # B. Horizonte: primero la suma de los 28 días por serie, luego la métrica.
        pron28 = pred.sum(axis=1)
        base28 = base.sum(axis=1)
        obs28 = obs.sum(axis=1)
        horizonte_c = _metricas(pron28 - obs28, obs28)
        horizonte_b = _metricas(base28 - obs28, obs28)

        # C. Agregado diario por segmento: primero la suma por fecha, luego la métrica.
        segmentos: dict[str, Any] = {}
        for dimension in DIMENSIONES:
            if dimension == "total":
                grupos = {"total": np.ones(len(series), dtype=bool)}
            elif dimension == "estado":
                grupos = {
                    estado_: np.array([tienda.startswith(f"{estado_}_") for tienda in tiendas])
                    for estado_ in sorted({tienda.split("_")[0] for tienda in tiendas})
                }
            elif dimension == "tienda":
                grupos = {tienda: tiendas == tienda for tienda in sorted(set(tiendas.tolist()))}
            elif dimension == "categoria":
                grupos = {
                    categoria: categorias == categoria
                    for categoria in sorted(set(categorias.tolist()))
                }
            else:
                grupos = {
                    departamento: departamentos == departamento
                    for departamento in sorted(set(departamentos.tolist()))
                }
            for clave, mascara in grupos.items():
                if not mascara.any():
                    continue
                pred_dia = pred[mascara].sum(axis=0)
                base_dia = base[mascara].sum(axis=0)
                obs_dia = obs[mascara].sum(axis=0)
                segmentos[f"{dimension}:{clave}"] = {
                    "series": int(mascara.sum()),
                    "candidato": _metricas(pred_dia - obs_dia, obs_dia),
                    "baseline": _metricas(base_dia - obs_dia, obs_dia),
                }

        # Contraste con lo guardado por el publicador.
        guardado = db[M5_EVALUACION_MODELOS].find_one(
            {"version": version, "run_id": run_id, "etapa": "final", "origen_d": corte}
        ) or {}
        publicado: dict[str, Any] = {}
        for dimension in DIMENSIONES:
            for documento in db[M5_PREDICCIONES].find(
                {
                    "version": version,
                    "run_id": run_id,
                    "origen_d": corte,
                    "dimension": dimension,
                    "revision": revision,
                }
            ):
                pron = np.asarray(documento.get("pronostico") or [], dtype=float)
                base_v = np.asarray(documento.get("baseline") or [], dtype=float)
                obs_v = np.asarray(
                    [np.nan if v is None else float(v) for v in (documento.get("observado") or [])],
                    dtype=float,
                )
                if pron.size == 0 or np.isnan(obs_v).all():
                    continue
                publicado[f"{dimension}:{documento['clave']}"] = {
                    "candidato": _metricas(pron - obs_v, obs_v),
                    "baseline": _metricas(base_v - obs_v, obs_v),
                }

        bloque = {
            "corte": corte,
            "fechas": {
                "origen": str(guardado.get("origen_fecha") or ""),
                "fin": str(guardado.get("fin_fecha") or ""),
            },
            "A_detalle_diario": {"candidato": diario_c, "baseline": diario_b},
            "B_horizonte_28": {
                "candidato": horizonte_c,
                "baseline": horizonte_b,
                "mejora": _mejora(horizonte_c, horizonte_b),
            },
            "C_agregado_diario": {
                clave: {
                    **valor,
                    "mejora": _mejora(valor["candidato"], valor["baseline"]),
                }
                for clave, valor in segmentos.items()
            },
            "publicado": publicado,
            "guardado": {
                "candidato": guardado.get("candidato"),
                "baseline": guardado.get("baseline"),
                "segmentos": guardado.get("segmentos"),
            },
        }
        informe["comparaciones"].append(bloque)

        # Contraste: lo guardado frente al recálculo.
        for nombre, recalculado, guardado_m in (
            ("detalle diario candidato", diario_c, guardado.get("candidato")),
            ("detalle diario baseline", diario_b, guardado.get("baseline")),
        ):
            if not guardado_m:
                informe["diferencias"].append(f"d_{corte}: sin métricas guardadas para {nombre}")
                continue
            for campo in ("mae", "sesgo", "wape"):
                esperado = guardado_m.get(campo)
                obtenido = recalculado.get(campo)
                if esperado is None or obtenido is None:
                    if (esperado is None) != (obtenido is None):
                        informe["diferencias"].append(
                            f"d_{corte} {nombre}.{campo}: guardado={esperado} recalculado={obtenido}"
                        )
                    continue
                if not _coincide(esperado, obtenido):
                    informe["diferencias"].append(
                        f"d_{corte} {nombre}.{campo}: guardado={esperado} recalculado={obtenido:.6f}"
                    )

        # Contraste: agregados publicados frente al recálculo (mismo corte y revisión).
        for clave, valor in publicado.items():
            recalculado = segmentos.get(clave)
            if not recalculado:
                informe["diferencias"].append(
                    f"d_{corte} {clave}: publicado sin equivalente en el recálculo"
                )
                continue
            for campo in ("mae", "sesgo", "wape"):
                esperado = valor["candidato"].get(campo)
                obtenido = recalculado["candidato"].get(campo)
                if esperado is None or obtenido is None:
                    continue
                if not _coincide(esperado, obtenido):
                    informe["diferencias"].append(
                        f"d_{corte} {clave}.candidato.{campo}: publicado={esperado} recalculado={obtenido:.6f}"
                    )

        acumulado.setdefault("detalle_c", []).append(diario_c)
        acumulado.setdefault("detalle_b", []).append(diario_b)
        acumulado.setdefault("horizonte_c", []).append(horizonte_c)
        acumulado.setdefault("horizonte_b", []).append(horizonte_b)
        for clave, valor in segmentos.items():
            acumulado.setdefault(f"agregado_c::{clave}", []).append(valor["candidato"])
            acumulado.setdefault(f"agregado_b::{clave}", []).append(valor["baseline"])

    # D. Consolidación entre cortes.
    consolidado: dict[str, Any] = {}
    for grano in ("detalle", "horizonte"):
        cand = _consolidar(acumulado[f"{grano}_c"])
        base = _consolidar(acumulado[f"{grano}_b"])
        consolidado[grano] = {
            "candidato": cand,
            "baseline": base,
            "mejora": _mejora(cand, base),
        }
    agregados = sorted(
        {clave.split("::", 1)[1] for clave in acumulado if clave.startswith("agregado_c::")}
    )
    consolidado["agregado_diario"] = {
        clave: {
            "candidato": _consolidar(acumulado[f"agregado_c::{clave}"]),
            "baseline": _consolidar(acumulado[f"agregado_b::{clave}"]),
        }
        for clave in agregados
    }
    for clave, valor in consolidado["agregado_diario"].items():
        valor["mejora"] = _mejora(valor["candidato"], valor["baseline"])
    informe["consolidado"] = consolidado
    return informe


def _resumen_consola(informe: dict[str, Any]) -> str:
    lineas: list[str] = []
    lineas.append(
        f"Versión {informe['version']} · ejecución {informe['run_id']} · "
        f"revisión {informe['revision']} ({informe['politica']})"
    )
    lineas.append(f"Cortes finales: {informe['cortes']}")
    for bloque in informe["comparaciones"]:
        corte = bloque["corte"]
        a = bloque["A_detalle_diario"]
        b = bloque["B_horizonte_28"]
        lineas.append(f"\n-- d_{corte}")
        lineas.append(
            "   A detalle diario   MAE {:.4f} · WAPE {:.4f} % · sesgo {:+.4f} u/serie-día "
            "(baseline MAE {:.4f})".format(
                a["candidato"]["mae"],
                a["candidato"]["wape"],
                a["candidato"]["sesgo"],
                a["baseline"]["mae"],
            )
        )
        lineas.append(
            "   B horizonte 28 d   MAE {:.4f} · WAPE {:.4f} % · sesgo {:+.4f} u/serie-horizonte "
            "(baseline MAE {:.4f}) · mejora {}".format(
                b["candidato"]["mae"],
                b["candidato"]["wape"],
                b["candidato"]["sesgo"],
                b["baseline"]["mae"],
                b["mejora"]["tipo"],
            )
        )
        total = bloque["C_agregado_diario"].get("total:total")
        if total:
            lineas.append(
                "   C agregado total   MAE {:.4f} · WAPE {:.4f} % · sesgo {:+.4f} u/día · "
                "baseline MAE {:.4f} · {}".format(
                    total["candidato"]["mae"],
                    total["candidato"]["wape"],
                    total["candidato"]["sesgo"],
                    total["baseline"]["mae"],
                    _texto_mejora(total["mejora"]),
                )
            )
    cons = informe.get("consolidado") or {}
    if cons:
        detalle = cons["detalle"]
        h = cons["horizonte"]
        agg = cons["agregado_diario"].get("total:total")
        lineas.append("\n== Consolidado de {} cortes".format(detalle["candidato"]["ventanas"]))
        lineas.append(
            "   detalle diario  MAE {:.4f} · WAPE {:.4f} % · {}".format(
                detalle["candidato"]["mae"],
                detalle["candidato"]["wape"],
                _texto_mejora(detalle["mejora"]),
            )
        )
        lineas.append(
            "   horizonte 28 d  MAE {:.4f} · WAPE {:.4f} % · {}".format(
                h["candidato"]["mae"],
                h["candidato"]["wape"],
                _texto_mejora(h["mejora"]),
            )
        )
        if agg:
            lineas.append(
                "   agregado total  MAE {:.4f} · WAPE {:.4f} % · {} · baseline MAE {:.4f}".format(
                    agg["candidato"]["mae"],
                    agg["candidato"]["wape"],
                    _texto_mejora(agg["mejora"]),
                    agg["baseline"]["mae"],
                )
            )
    duplicados = informe.get("duplicados") or {}
    repetidas = {k: v for k, v in duplicados.items() if v.get("claves_repetidas")}
    lineas.append(
        "\nDuplicados: "
        + ("ninguno" if not repetidas else json.dumps(repetidas, ensure_ascii=False))
    )
    if informe["diferencias"]:
        lineas.append(f"\n✗ {len(informe['diferencias'])} discrepancia(s):")
        for diferencia in informe["diferencias"][:20]:
            lineas.append(f"   · {diferencia}")
    else:
        lineas.append("\n✓ Las cifras publicadas coinciden con el recálculo independiente")
    return "\n".join(lineas)


def _texto_mejora(mejora: dict[str, Any]) -> str:
    tipo = mejora.get("tipo")
    pct = mejora.get("pct")
    if tipo == "mejor":
        return f"{abs(pct):.3f} % menos error que el baseline"
    if tipo == "peor":
        return f"{abs(pct):.3f} % más error que el baseline"
    if tipo == "sin_diferencia_numerica":
        return "mismo error dentro de la precisión del cálculo"
    if tipo == "diferencia_pequena":
        direccion = "menos" if (pct or 0) > 0 else "más"
        return f"diferencia inferior a 0,05 % ({abs(pct or 0):.4f} % {direccion} error)"
    return f"no calculable ({mejora.get('motivo')})"


def main() -> int:
    parser = argparse.ArgumentParser(description="Auditoría independiente de la evaluación")
    parser.add_argument("--json", default=None, help="guarda el informe completo en un archivo")
    parser.add_argument(
        "--sin-recalculo",
        action="store_true",
        help="solo contrastes de claves y de lo guardado (no vuelve a predecir)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    db = init_database(get_settings()).m5_db
    informe = auditar(db, recalcular=not args.sin_recalculo)
    print(_resumen_consola(informe))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as archivo:
            json.dump(informe, archivo, ensure_ascii=False, indent=1, default=float)
        print(f"\nInforme guardado en {args.json}")
    return 1 if informe["diferencias"] else 0


if __name__ == "__main__":
    sys.exit(main())
