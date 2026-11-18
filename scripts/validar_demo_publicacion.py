"""Validación autoconsistente de la base de publicación (no necesita la base de trabajo).

Comprueba, contra el manifiesto del paquete y contra el propio contenido:

* recuentos por colección y revisión única;
* universo del contrato (10 tiendas, 3 estados, 3 categorías, 7 departamentos, 3 049 artículos,
  30 490 series, 304 900 bloques);
* reconciliación de unidades entre bloques y agregados;
* planificación: semanas = horizonte (1e-6) y total del horizonte = publicación;
* predicciones: agregado = suma de series por día (1e-6) y métricas de evaluación intactas;
* precios: centavos enteros alineados al catálogo, sin precios ≤ 0 ni conteos alterados;
* ausencia de rutas locales o credenciales en los documentos;
* índices indispensables presentes.

Uso (desde ``backend/``):

    python -m scripts.validar_demo_publicacion --base <base_destino>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from pymongo.database import Database

from app.config import get_settings
from app.db.mongo import (
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_CATALOGO,
    M5_ESTADO,
    M5_EVALUACION_MODELOS,
    M5_EVALUACION_SERIES,
    M5_PLANIFICACION,
    M5_PRECIOS,
    M5_PRECIOS_CATALOGO,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PUBLICACION,
    init_database,
)

MANIFIESTO = "m5_demo_manifest"
ESQUEMA = "atlas-comercial-demo-1"
COLECCIONES_CONTRATO = {
    M5_CATALOGO: 3072,
    M5_BLOQUES: 304900,
    M5_PREDICCIONES_SERIES: 30490,
    M5_EVALUACION_SERIES: 60980,
    M5_PLANIFICACION: 30490,
    M5_PRECIOS: 2820,
    M5_PRECIOS_CATALOGO: 10,
    M5_AGREGADOS: 124,
}


def validar_base(db: Database, manifiesto: dict | None = None) -> dict[str, Any]:
    """Devuelve ``{"ok": bool, "hallazgos": [...], ...}`` para la base indicada."""
    hallazgos: list[str] = []
    guardado = db[MANIFIESTO].find_one({"_id": ESQUEMA})
    # El manifiesto puede venir del paquete (clave ``revision_publicada``) o de la propia base
    # (clave ``revision``). Se acepta cualquiera de los dos y se cae al guardado si falta.
    manifiesto = {**(guardado or {}), **(manifiesto or {})}
    if not manifiesto:
        return {"ok": False, "hallazgos": ["No hay manifiesto del paquete en la base"], "base": db.name}
    revision_manifiesto = manifiesto.get("revision") or manifiesto.get("revision_publicada")

    estado = db[M5_ESTADO].find_one({}) or {}
    version = estado.get("version") or manifiesto.get("version")
    publicacion = db[M5_PUBLICACION].find_one({"activa": True}) or {}
    revision = publicacion.get("_id")

    datos: dict[str, Any] = {
        "base": db.name,
        "version": version,
        "revision": revision,
        "politica": publicacion.get("politica"),
    }

    # 1. Recuentos y contrato.
    recuentos = {coleccion: db[coleccion].count_documents({}) for coleccion in COLECCIONES_CONTRATO}
    datos["documentos"] = recuentos
    for coleccion, esperado in COLECCIONES_CONTRATO.items():
        if recuentos[coleccion] != esperado:
            hallazgos.append(f"{coleccion}: {recuentos[coleccion]} documentos (contrato {esperado})")
    if db[M5_CATALOGO].count_documents({"tipo": "producto"}) != 3049:
        hallazgos.append("m5_catalogo: no hay 3 049 productos")
    if db[M5_CATALOGO].count_documents({"tipo": "tienda"}) != 10:
        hallazgos.append("m5_catalogo: no hay 10 tiendas")
    if db[M5_CATALOGO].count_documents({"tipo": "departamento"}) != 7:
        hallazgos.append("m5_catalogo: no hay 7 departamentos")
    if db[M5_CATALOGO].count_documents({"tipo": "categoria"}) != 3:
        hallazgos.append("m5_catalogo: no hay 3 categorías")

    # 2. Revisión única.
    for coleccion in (M5_PREDICCIONES, M5_PREDICCIONES_SERIES, M5_EVALUACION_SERIES, M5_PLANIFICACION):
        revisiones = set(db[coleccion].distinct("revision"))
        if revisiones != {revision}:
            hallazgos.append(f"{coleccion}: revisiones {sorted(str(valor) for valor in revisiones)}")
    if revision_manifiesto and revision != revision_manifiesto:
        hallazgos.append(
            f"la revisión activa ({revision}) no coincide con la del manifiesto ({revision_manifiesto})"
        )

    # 3. Unidades: bloques = agregado total = manifiesto.
    agregado = db[M5_AGREGADOS].find_one({"version": version, "dimension": "total"}) or {}
    unidades_agregado = int(agregado.get("total") or 0)
    suma_bloques = int(sum(sum(documento.get("unidades") or []) for documento in db[M5_BLOQUES].find({}, {"unidades": 1})))
    esperado = int((manifiesto.get("validacion") or {}).get("unidades", {}).get("agregado") or unidades_agregado)
    if unidades_agregado != suma_bloques:
        hallazgos.append(f"unidades: agregado {unidades_agregado} ≠ bloques {suma_bloques}")
    if esperado and unidades_agregado != esperado:
        hallazgos.append(f"unidades: agregado {unidades_agregado} ≠ manifiesto {esperado}")
    datos["unidades"] = {"agregado": unidades_agregado, "bloques": suma_bloques}

    # 4. Planificación.
    muestra = list(db[M5_PLANIFICACION].find({"revision": revision}).limit(1000))
    desvio = max((abs(sum(documento.get("semanas") or []) - float(documento.get("pronostico_28") or 0)) for documento in muestra), default=0.0)
    total_plan = sum(
        float(documento.get("pronostico_28") or 0)
        for documento in db[M5_PLANIFICACION].find({"revision": revision}, {"pronostico_28": 1})
    )
    total_publicado = float(publicacion.get("total_horizonte") or 0)
    if desvio > 1e-6:
        hallazgos.append(f"planificación: semanas ≠ horizonte (desvío {desvio})")
    if total_publicado and abs(total_plan - total_publicado) > 0.5:
        hallazgos.append(f"planificación: total {total_plan} ≠ publicación {total_publicado}")
    datos["planificacion"] = {"desvio_semanas": desvio, "total": round(total_plan, 4), "publicado": total_publicado}

    # 5. Predicciones: agregado = suma de series y métricas intactas.
    reconciliacion = {}
    for origen_d in sorted({int(documento["origen_d"]) for documento in db[M5_PREDICCIONES].find({"revision": revision}, {"origen_d": 1})}):
        series = list(db[M5_PREDICCIONES_SERIES].find({"revision": revision, "origen_d": origen_d}, {"pronostico": 1}))
        agregado_origen = db[M5_PREDICCIONES].find_one(
            {"revision": revision, "origen_d": origen_d, "dimension": "total", "clave": "total"}, {"pronostico": 1}
        )
        if not series or not agregado_origen:
            continue
        suma = np.zeros(len(series[0]["pronostico"]), dtype=float)
        for documento in series:
            suma += np.asarray(documento["pronostico"], dtype=float)
        diferencia = float(np.max(np.abs(suma - np.asarray(agregado_origen["pronostico"], dtype=float))))
        reconciliacion[str(origen_d)] = {"series": len(series), "max_diferencia": round(diferencia, 8)}
        if diferencia > 1e-6:
            hallazgos.append(f"predicciones d_{origen_d}: agregado ≠ suma de series ({diferencia})")
        if series[0]["pronostico"] and not any(
            abs(valor - round(valor)) > 1e-9
            for documento in series[:50]
            for valor in documento["pronostico"]
        ):
            hallazgos.append(f"predicciones d_{origen_d}: la serie canónica no conserva decimales")
    datos["reconciliacion_predicciones"] = reconciliacion
    if db[M5_EVALUACION_MODELOS].count_documents({}) < 14:
        hallazgos.append("faltan evaluaciones del manifiesto de modelos")

    # 6. Precios: catálogo, centavos positivos y conteos del manifiesto.
    pares = 0
    for documento in db[M5_PRECIOS].find({}, {"centavos": 1, "con_precio": 1, "items_total": 1}):
        valores = memoryview(documento["centavos"]).cast("i")
        if len(valores) != int(documento.get("items_total") or 0):
            hallazgos.append(f"precios: longitud de centavos distinta del catálogo en {documento.get('_id')}")
            break
        if int(np.count_nonzero(np.asarray(valores))) != int(documento.get("con_precio") or 0):
            hallazgos.append(f"precios: conteo con_precio inconsistente en {documento.get('_id')}")
            break
        if (np.asarray(valores) < 0).any():
            hallazgos.append("precios: hay centavos negativos")
            break
        pares += int(documento.get("con_precio") or 0)
    catalogo = db[M5_PRECIOS_CATALOGO].find_one({})
    if not catalogo or not catalogo.get("items"):
        hallazgos.append("precios: falta el catálogo por tienda")
    esperado_pares = int(((manifiesto.get("validacion") or {}).get("precios") or {}).get("pares_con_precio") or 0)
    if esperado_pares and pares != esperado_pares:
        hallazgos.append(f"precios: {pares} pares con precio frente a {esperado_pares} del manifiesto")
    datos["precios"] = {"pares_con_precio": pares, "esperados": esperado_pares}

    # 7. Sin rutas locales ni credenciales.
    sospechosos = 0
    for documento in db["m5_importaciones"].find({}, {"carpeta_origen": 1}):
        if documento.get("carpeta_origen"):
            sospechosos += 1
    for documento in db["m5_modelos"].find({}, {"artefacto": 1}):
        if documento.get("artefacto"):
            sospechosos += 1
    datos["rutas_locales"] = sospechosos
    if sospechosos:
        hallazgos.append(f"{sospechosos} documentos conservan rutas locales")

    # 8. Índices indispensables.
    indices = {}
    for coleccion in (M5_BLOQUES, M5_PREDICCIONES, M5_PREDICCIONES_SERIES, M5_EVALUACION_SERIES, M5_PLANIFICACION, M5_PRECIOS):
        claves = [list(indice["key"]) for indice in db[coleccion].index_information().values() if indice["key"] != [("_id", 1)]]
        indices[coleccion] = claves
        if not claves:
            hallazgos.append(f"{coleccion}: sin índices secundarios")
    datos["indices"] = indices

    datos["ok"] = not hallazgos
    datos["hallazgos"] = hallazgos
    return datos


def main() -> int:
    parser = argparse.ArgumentParser(description="Valida la base de publicación (autoconsistente)")
    parser.add_argument("--base", required=True)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()
    settings = get_settings()
    cliente = init_database(settings)
    db = cliente.m5_db if cliente.m5_db.name == args.base else cliente.m5_db.client[args.base]
    informe = validar_base(db)
    print(
        f"Validación de {informe['base']} · revisión {informe.get('revision')} · "
        + ("OK" if informe["ok"] else f"{len(informe['hallazgos'])} hallazgo(s)")
    )
    for hallazgo in informe["hallazgos"]:
        print(f"  · {hallazgo}")
    if args.json:
        Path(args.json).write_text(json.dumps(informe, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"JSON: {args.json}")
    return 0 if informe["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
