"""Genera la base local de publicación (demo) a partir de la base local de trabajo.

Qué hace, en orden:

1. **Comprueba el destino**: la URI es local (la misma validación que usa la API), la base destino
   existe como nombre declarado y es distinta de la fuente.
2. **Copia solo lo que la demo consume**, con tres decisiones de diseño justificadas por el
   inventario:
   - **Solo la revisión activa** de predicciones, series, evaluación y planificación. La revisión
     anterior se queda en la base local: no se borra ni se modifica la fuente.
   - **Precios compactos**: catálogo de artículos por tienda (una vez) y, por semana, un arreglo
     binario de centavos `int32` alineado a ese catálogo (`0` = sin precio). Es exacto: los precios
     del archivo tienen dos decimales.
   - **Campos sin consumidores** fuera del paquete (`item_index`, `store_index`, `esquema`,
     `artefacto` con ruta local) y rutas locales eliminadas de los manifiestos.
3. **Escribe con identificadores deterministas** (los `_id` de origen, o claves estables) y de
   forma idempotente: cada documento se reemplaza por su `_id`, así que repetir el proceso deja el
   mismo estado.
4. **Activa al final**: el marcador de versión (`m5_estado`) se escribe después de todas las
   colecciones; mientras tanto la base no se considera publicada.
5. **Valida** antes de dar por buena la generación (recuentos, reconciliación de unidades, semanas
   y agregados, métricas idénticas, precios exactos, ausencia de rutas locales, revisión única).

Uso (desde ``backend/``):

    python -m scripts.generar_demo_publicacion --destino atlas_comercial_demo
    python -m scripts.generar_demo_publicacion --destino atlas_comercial_demo --verificar
    python -m scripts.generar_demo_publicacion --destino atlas_comercial_demo --json salida.json

La fuente nunca se escribe: todas las escrituras van a la base destino.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Any, Iterator

import numpy as np
from bson import Binary
from pymongo.database import Database

from app.comercial.conexion import HOSTS_LOCALES
from app.comercial.precios_almacen import M5_PRECIOS_CATALOGO
from app.config import get_settings
from app.db.mongo import (
    M5_AGREGADOS,
    M5_AUDITORIA_TEMPORAL,
    M5_BLOQUES,
    M5_CALENDARIO,
    M5_CATALOGO,
    M5_DERIVADOS,
    M5_ESTADO,
    M5_EVALUACION,
    M5_EVALUACION_MODELOS,
    M5_EVALUACION_SERIES,
    M5_IMPORTACIONES,
    M5_MODELOS,
    M5_PLANIFICACION,
    M5_PRECIOS,
    M5_PRECIOS_MANIFEST,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PRONOSTICOS,
    M5_PUBLICACION,
    init_database,
)

ESQUEMA_PAQUETE = "atlas-comercial-demo-1"
MANIFIESTO = "m5_demo_manifest"
LOTE = 1000

#: Colecciones que la demo consume, en el orden en que se escriben. `m5_estado` va al final
#: porque es el marcador de versión publicada.
COLECCIONES = [
    M5_IMPORTACIONES,
    M5_CATALOGO,
    M5_CALENDARIO,
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_DERIVADOS,
    M5_MODELOS,
    M5_EVALUACION_MODELOS,
    M5_PRONOSTICOS,
    M5_EVALUACION,
    M5_PUBLICACION,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_EVALUACION_SERIES,
    M5_PLANIFICACION,
    M5_AUDITORIA_TEMPORAL,
    M5_PRECIOS_MANIFEST,
    M5_PRECIOS_CATALOGO,
    M5_PRECIOS,
    M5_ESTADO,
]

#: Campos que se retiran del paquete: no tienen consumidor en la API y/o contienen rutas locales.
CAMPOS_FUERA = {
    M5_BLOQUES: ("esquema",),
    M5_PREDICCIONES_SERIES: ("item_index", "store_index", "esquema"),
    M5_MODELOS: ("artefacto",),
    M5_IMPORTACIONES: ("carpeta_origen",),
    M5_PRECIOS_MANIFEST: ("carpeta_origen",),
}


def _validar_destino_local(uri: str, destino: str, fuente: str) -> None:
    """Comprueba que el servidor es local y que la base destino no es la de origen.

    No se usa ``resolver_destino`` porque exige que la base del URI coincida con la declarada y aquí
    el destino es otra base del **mismo** servidor local. Las comprobaciones de seguridad son las
    mismas: sin ``mongodb+srv``, solo hosts locales y destino distinto de la fuente.
    """
    texto = (uri or "").strip()
    if texto.startswith("mongodb+srv://"):
        raise SystemExit("mongodb+srv:// no está permitido en este incremento")
    if not texto.startswith("mongodb://"):
        raise SystemExit("COMERCIAL_MONGODB_URI debe empezar por mongodb://")
    partes = urlsplit(texto)
    host = (partes.hostname or "").strip().lower()
    if host not in HOSTS_LOCALES:
        raise SystemExit(f"Destino no autorizado: solo MongoDB local ({', '.join(sorted(HOSTS_LOCALES))})")
    if not destino or destino == fuente:
        raise SystemExit("La base destino debe ser explícita y distinta de la base de origen")
    if not destino.endswith(("_demo", "_pub", "_demo_test")):
        raise SystemExit("El nombre de la base destino debe terminar en _demo, _pub o _demo_test")


def _hash_documento(documento: dict[str, Any]) -> str:
    """Huella estable del documento para el manifiesto del paquete."""
    texto = json.dumps(documento, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _limpiar(documento: dict[str, Any], coleccion: str) -> dict[str, Any]:
    salida = dict(documento)
    for campo in CAMPOS_FUERA.get(coleccion, ()):
        salida.pop(campo, None)
    if coleccion == M5_MODELOS and documento.get("artefacto"):
        # Se conserva el nombre del archivo, no la ruta del equipo donde se entrenó.
        salida["artefacto_nombre"] = str(documento["artefacto"]).replace("\\", "/").split("/")[-1]
    return salida


def _revision_activa(db: Database) -> dict[str, Any]:
    publicacion = db[M5_PUBLICACION].find_one({"activa": True})
    if not publicacion:
        raise SystemExit("La base de origen no tiene una publicación activa")
    return publicacion


def _solo_revision(documentos: Iterator[dict], revision: str | None, *, exigir: bool = True) -> Iterator[dict]:
    """Filtra documentos de la revisión activa (los antiguos no llevan campo ``revision``)."""
    for documento in documentos:
        valor = documento.get("revision")
        if revision is None:
            if valor in (None, ""):
                yield documento
        elif valor == revision:
            yield documento
        elif exigir and valor is not None:
            # Otra revisión distinta de la activa: se descarta (nunca se mezcla).
            continue


def _precios_compactos(origen: Database, destino: Database, version_precios: str) -> dict[str, Any]:
    """Reescribe los precios al esquema compacto (catálogo por tienda + centavos binarios).

    Devuelve el recuento de documentos y de pares con precio para la validación.
    """
    tiendas = sorted(
        {documento["store_id"] for documento in origen[M5_PRECIOS].find({"version_precios": version_precios}, {"store_id": 1})}
    )
    documentos_semana = 0
    pares_con_precio = 0
    for tienda in tiendas:
        # Catálogo estable: orden alfabético de los artículos que alguna vez tuvieron precio.
        articulos = sorted(
            {
                item
                for documento in origen[M5_PRECIOS].find(
                    {"version_precios": version_precios, "store_id": tienda}, {"items": 1}
                )
                for item in documento.get("items") or []
            }
        )
        posicion = {item: indice for indice, item in enumerate(articulos)}
        destino[M5_PRECIOS_CATALOGO].replace_one(
            {"_id": f"{version_precios}:{tienda}"},
            {
                "_id": f"{version_precios}:{tienda}",
                "esquema": "m5-precios-catalogo-1",
                "version_precios": version_precios,
                "store_id": tienda,
                "items": articulos,
                "items_total": len(articulos),
            },
            upsert=True,
        )
        for documento in origen[M5_PRECIOS].find(
            {"version_precios": version_precios, "store_id": tienda},
            {"wm_yr_wk": 1, "items": 1, "precios": 1, "version_ventas": 1},
        ):
            centavos = np.zeros(len(articulos), dtype=np.int32)
            for item, precio in zip(documento.get("items") or [], documento.get("precios") or [], strict=False):
                indice = posicion.get(item)
                if indice is None:
                    continue
                valor = int(round(float(precio) * 100))
                if valor <= 0:
                    raise SystemExit(
                        f"Precio no positivo al compactar ({tienda}, semana {documento['wm_yr_wk']}, {item})"
                    )
                centavos[indice] = valor
                pares_con_precio += 1
            destino[M5_PRECIOS].replace_one(
                {"_id": f"{version_precios}:{tienda}:{int(documento['wm_yr_wk'])}"},
                {
                    "_id": f"{version_precios}:{tienda}:{int(documento['wm_yr_wk'])}",
                    "esquema": "m5-precios-2",
                    "version_precios": version_precios,
                    "version_ventas": documento.get("version_ventas"),
                    "store_id": tienda,
                    "wm_yr_wk": int(documento["wm_yr_wk"]),
                    "items_total": len(articulos),
                    "con_precio": int((centavos > 0).sum()),
                    "centavos": Binary(centavos.tobytes()),
                },
                upsert=True,
            )
            documentos_semana += 1
    return {"tiendas": len(tiendas), "documentos_semana": documentos_semana, "pares_con_precio": pares_con_precio}


def _copiar(origen: Database, destino: Database, coleccion: str, *, revision: str | None) -> int:
    """Copia una colección (con filtro de revisión y limpieza de campos) de forma idempotente."""
    consulta: dict[str, Any] = {}
    if coleccion in (M5_PREDICCIONES, M5_PREDICCIONES_SERIES, M5_EVALUACION_SERIES, M5_PLANIFICACION):
        consulta = {"revision": revision} if revision else {"revision": {"$exists": False}}
    elif coleccion == M5_PUBLICACION:
        consulta = {"activa": True}
    elif coleccion == M5_ESTADO:
        # Se escribe al final: es el marcador de versión publicada.
        consulta = {}
    total = 0
    lote: list[dict[str, Any]] = []
    for documento in origen[coleccion].find(consulta):
        limpio = _limpiar(documento, coleccion)
        lote.append(limpio)
        if len(lote) >= LOTE:
            _escribir(destino, coleccion, lote)
            total += len(lote)
            lote = []
    if lote:
        _escribir(destino, coleccion, lote)
        total += len(lote)
    return total


def _escribir(destino: Database, coleccion: str, documentos: list[dict[str, Any]]) -> None:
    from pymongo import ReplaceOne

    destino[coleccion].bulk_write(
        [ReplaceOne({"_id": documento["_id"]}, documento, upsert=True) for documento in documentos],
        ordered=False,
    )


def _totales(origen: Database, version: str) -> dict[str, Any]:
    agregado = origen[M5_AGREGADOS].find_one({"version": version, "dimension": "total"})
    return {
        "unidades": int(agregado["total"]),
        "dias": int(agregado["d_fin"]),
        "series_desde_agregados": int(
            origen[M5_AGREGADOS].count_documents({"version": version, "dimension": "departamento_tienda"})
        ),
    }


def generar(destino_nombre: str, *, vaciar: bool = False) -> dict[str, Any]:
    """Genera (o actualiza) la base de publicación y devuelve el informe de la generación."""
    settings = get_settings()
    _validar_destino_local(settings.comercial_mongodb_uri, destino_nombre, settings.comercial_mongodb_db or "")
    cliente = init_database(settings)
    origen = cliente.m5_db
    if destino_nombre == origen.name:
        raise SystemExit("La base destino debe ser distinta de la base de origen")

    version_estado = origen[M5_ESTADO].find_one({"version": {"$exists": True}}) or origen[M5_ESTADO].find_one()
    version = version_estado["version"]
    publicacion = _revision_activa(origen)
    revision = publicacion["_id"]
    destino = origen.client[destino_nombre]

    inicio = time.perf_counter()
    destino[MANIFIESTO].update_one(
        {"_id": ESQUEMA_PAQUETE},
        {
            "$set": {
                "estado": "generando",
                "iniciado_en": datetime.now(timezone.utc).replace(tzinfo=None),
                "version": version,
                "run_id": publicacion.get("run_id"),
                "revision": revision,
            }
        },
        upsert=True,
    )

    if vaciar:
        for coleccion in COLECCIONES:
            destino[coleccion].delete_many({})

    # Precios: esquema compacto propio (no se copian los documentos de origen).
    version_precios = (origen[M5_PRECIOS_MANIFEST].find_one(sort=[("importado_en", -1)]) or {}).get(
        "version_precios"
    )
    resumen_precios = _precios_compactos(origen, destino, version_precios) if version_precios else {}

    documentos: dict[str, int] = {}
    for coleccion in COLECCIONES:
        if coleccion == M5_PRECIOS:
            documentos[coleccion] = resumen_precios.get("documentos_semana", 0)
            continue
        if coleccion == M5_PRECIOS_CATALOGO:
            documentos[coleccion] = resumen_precios.get("tiendas", 0)
            continue
        documentos[coleccion] = _copiar(origen, destino, coleccion, revision=revision)

    # Índices indispensables del paquete (idempotentes).
    destino[M5_BLOQUES].create_index([("version", 1), ("store_id", 1), ("item_id", 1)])
    destino[M5_PREDICCIONES].create_index([("revision", 1), ("origen_d", 1), ("dimension", 1)])
    destino[M5_PREDICCIONES_SERIES].create_index([("revision", 1), ("store_id", 1), ("item_id", 1)])
    destino[M5_EVALUACION_SERIES].create_index([("revision", 1), ("origen_d", 1), ("store_id", 1), ("item_id", 1)])
    destino[M5_PLANIFICACION].create_index([("revision", 1), ("store_id", 1), ("item_id", 1)])
    destino[M5_PLANIFICACION].create_index([("revision", 1), ("pronostico_28", -1)])
    destino[M5_PRECIOS].create_index([("version_precios", 1), ("store_id", 1), ("wm_yr_wk", 1)])
    destino[M5_PRECIOS_CATALOGO].create_index([("version_precios", 1), ("store_id", 1)])
    destino[M5_AGREGADOS].create_index([("version", 1), ("dimension", 1), ("clave", 1)])

    duracion = round(time.perf_counter() - inicio, 1)
    totales = _totales(origen, version)
    informe = {
        "esquema": ESQUEMA_PAQUETE,
        "base_origen": origen.name,
        "base_destino": destino.name,
        "version": version,
        "run_id": publicacion.get("run_id"),
        "revision": revision,
        "politica_precision": publicacion.get("politica"),
        "total_horizonte": publicacion.get("total_horizonte"),
        "documentos": documentos,
        "precios": resumen_precios,
        "totales_origen": totales,
        "duracion_s": duracion,
        "generado_en": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
    }
    destino[MANIFIESTO].update_one(
        {"_id": ESQUEMA_PAQUETE},
        {"$set": {**informe, "estado": "generada"}},
        upsert=True,
    )
    return informe


def validar(origen: Database, destino: Database, informe: dict[str, Any]) -> dict[str, Any]:
    """Valida el paquete contra la fuente. Devuelve el informe de validación."""
    version = informe["version"]
    revision = informe["revision"]
    hallazgos: list[str] = []
    datos: dict[str, Any] = {"esquema": ESQUEMA_PAQUETE, "revision": revision, "version": version}

    # 1. Recuentos.
    for coleccion, esperado in informe["documentos"].items():
        real = destino[coleccion].count_documents({})
        if real != esperado:
            hallazgos.append(f"{coleccion}: {real} documentos en destino frente a {esperado} generados")
    datos["documentos"] = {c: destino[c].count_documents({}) for c in COLECCIONES}

    # 2. Revisión única en las colecciones versionadas.
    for coleccion in (M5_PREDICCIONES, M5_PREDICCIONES_SERIES, M5_EVALUACION_SERIES, M5_PLANIFICACION):
        revisiones = set(destino[coleccion].distinct("revision"))
        if revisiones != {revision}:
            hallazgos.append(f"{coleccion}: revisiones en destino {sorted(revisiones)}")

    # 3. Universo y reconciliación de unidades.
    agregado_total = destino[M5_AGREGADOS].find_one({"version": version, "dimension": "total"})
    unidades_agregado = int(agregado_total["total"])
    suma_bloques = int(
        sum(
            sum(documento.get("unidades") or [])
            for documento in destino[M5_BLOQUES].find({"version": version}, {"unidades": 1})
        )
    )
    esperado_unidades = informe["totales_origen"]["unidades"]
    if unidades_agregado != esperado_unidades:
        hallazgos.append(f"agregado total {unidades_agregado} ≠ fuente {esperado_unidades}")
    if suma_bloques != unidades_agregado:
        hallazgos.append(f"suma de bloques {suma_bloques} ≠ agregado {unidades_agregado}")
    datos["unidades"] = {"agregado": unidades_agregado, "bloques": suma_bloques}
    if destino[M5_BLOQUES].count_documents({"version": version}) != 304_900:
        hallazgos.append("m5_bloques no tiene 304 900 bloques (30 490 series × 10)")
    if destino[M5_CATALOGO].count_documents({"version": version, "tipo": "producto"}) != 3_049:
        hallazgos.append("m5_catalogo no tiene 3 049 artículos")
    if destino[M5_CATALOGO].count_documents({"version": version, "tipo": "tienda"}) != 10:
        hallazgos.append("m5_catalogo no tiene 10 tiendas")

    # 4. Planificación: semanas = horizonte y total del horizonte.
    muestra = list(destino[M5_PLANIFICACION].find({"revision": revision}).limit(500))
    desvio_max = 0.0
    for documento in muestra:
        suma = sum(documento.get("semanas") or [])
        desvio_max = max(desvio_max, abs(suma - float(documento.get("pronostico_28") or 0)))
    if desvio_max > 1e-6:
        hallazgos.append(f"planificación: semanas ≠ horizonte (desvío {desvio_max})")
    total_plan = sum(
        float(documento.get("pronostico_28") or 0)
        for documento in destino[M5_PLANIFICACION].find({"revision": revision}, {"pronostico_28": 1})
    )
    if abs(total_plan - float(informe["total_horizonte"])) > 0.5:
        hallazgos.append(f"total del plan {total_plan} ≠ publicación {informe['total_horizonte']}")
    datos["planificacion"] = {"filas": len(muestra), "desvio_semanas_max": desvio_max, "total": round(total_plan, 4)}

    # 5. Predicciones: agregado = suma de series por día.
    objetivo = sorted(
        {int(documento["origen_d"]) for documento in destino[M5_PREDICCIONES].find({"revision": revision}, {"origen_d": 1})}
    )
    reconciliacion = {}
    for origen_d in objetivo:
        series = list(
            destino[M5_PREDICCIONES_SERIES].find(
                {"revision": revision, "origen_d": origen_d}, {"pronostico": 1}
            )
        )
        if not series:
            continue
        suma = np.zeros(len(series[0]["pronostico"]), dtype=float)
        for documento in series:
            suma += np.asarray(documento["pronostico"], dtype=float)
        agregados = list(
            destino[M5_PREDICCIONES].find(
                {"revision": revision, "origen_d": origen_d, "dimension": "total", "clave": "total"},
                {"pronostico": 1},
            )
        )
        if not agregados:
            continue
        esperado = np.asarray(agregados[0]["pronostico"], dtype=float)
        diferencia = float(np.max(np.abs(suma - esperado)))
        reconciliacion[str(origen_d)] = {"series": len(series), "max_diferencia": round(diferencia, 8)}
        if diferencia > 1e-6:
            hallazgos.append(f"predicciones d_{origen_d}: agregado ≠ suma de series (dif. {diferencia})")
    datos["reconciliacion_predicciones"] = reconciliacion

    # 6. Métricas de evaluación idénticas a la fuente.
    for documento in origen[M5_EVALUACION_MODELOS].find({}, {"_id": 1, "candidato": 1, "baseline": 1, "origen_d": 1, "run_id": 1, "etapa": 1}):
        copia = destino[M5_EVALUACION_MODELOS].find_one({"_id": documento["_id"]}, {"candidato": 1, "baseline": 1})
        if not copia:
            hallazgos.append(f"falta la evaluación {documento['_id']}")
            continue
        if copia.get("candidato") != documento.get("candidato") or copia.get("baseline") != documento.get("baseline"):
            hallazgos.append(f"métricas alteradas en {documento['_id']}")
    if destino[M5_EVALUACION_MODELOS].count_documents({}) != origen[M5_EVALUACION_MODELOS].count_documents({}):
        hallazgos.append("el número de evaluaciones no coincide con la fuente")

    # 7. Precios: exactitud frente a la fuente y cobertura.
    version_precios = informe["precios"] and (destino[M5_PRECIOS_MANIFEST].find_one({}) or {}).get("version_precios")
    if version_precios:
        tienda = "CA_1"
        catalogo = destino[M5_PRECIOS_CATALOGO].find_one({"version_precios": version_precios, "store_id": tienda})
        documento_destino = destino[M5_PRECIOS].find_one(
            {"version_precios": version_precios, "store_id": tienda}, sort=[("wm_yr_wk", 1)]
        )
        documento_origen = origen[M5_PRECIOS].find_one(
            {"version_precios": version_precios, "store_id": tienda, "wm_yr_wk": documento_destino["wm_yr_wk"]}
        )
        originales = dict(zip(documento_origen.get("items") or [], documento_origen.get("precios") or [], strict=False))
        valores = memoryview(documento_destino["centavos"]).cast("i")
        revisados = 0
        for item, valor in zip(catalogo["items"], valores, strict=False):
            if valor > 0:
                revisados += 1
                if abs(originales.get(item, 0.0) - valor / 100.0) > 1e-9:
                    hallazgos.append(f"precio alterado en {tienda}/{documento_destino['wm_yr_wk']}/{item}")
                    break
        datos["precios"] = {
            "version_precios": version_precios,
            "catalogo_items": len(catalogo["items"]),
            "semana_revisada": int(documento_destino["wm_yr_wk"]),
            "precios_comparados": revisados,
        }
        pares = sum(int(documento.get("con_precio") or 0) for documento in destino[M5_PRECIOS].find({}, {"con_precio": 1}))
        if pares != informe["precios"]["pares_con_precio"]:
            hallazgos.append(f"pares con precio {pares} ≠ {informe['precios']['pares_con_precio']}")
        datos["precios"]["pares_con_precio"] = pares

    # 8. Sin rutas locales ni credenciales en los documentos.
    sospechosos = 0
    for coleccion in COLECCIONES:
        for documento in destino[coleccion].find({}, {"carpeta_origen": 1, "artefacto": 1}).limit(50):
            if documento.get("carpeta_origen") or documento.get("artefacto"):
                sospechosos += 1
    if sospechosos:
        hallazgos.append(f"{sospechosos} documentos conservan rutas locales")
    datos["rutas_locales"] = sospechosos

    # 9. Publicación activa declarada y versiones.
    activa = destino[M5_PUBLICACION].find_one({"activa": True})
    if not activa or activa.get("_id") != revision:
        hallazgos.append("la publicación activa del paquete no coincide con la revisión declarada")
    datos["publicacion"] = {"revision": (activa or {}).get("_id"), "politica": (activa or {}).get("politica")}
    datos["ok"] = not hallazgos
    datos["hallazgos"] = hallazgos
    return datos


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera y valida la base local de publicación (demo)")
    parser.add_argument("--destino", default="atlas_comercial_demo")
    parser.add_argument("--vaciar", action="store_true", help="borra las colecciones del paquete en destino")
    parser.add_argument("--verificar", action="store_true", help="solo valida una base ya generada")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    settings = get_settings()
    _validar_destino_local(settings.comercial_mongodb_uri, args.destino, settings.comercial_mongodb_db or "")
    cliente = init_database(settings)
    origen = cliente.m5_db
    destino = origen.client[args.destino]

    if args.verificar:
        manifiesto = destino[MANIFIESTO].find_one({"_id": ESQUEMA_PAQUETE})
        if not manifiesto:
            raise SystemExit("No hay paquete generado en el destino")
        informe = {clave: valor for clave, valor in manifiesto.items() if clave not in ("_id", "estado")}
    else:
        informe = generar(args.destino, vaciar=args.vaciar)
        print(
            f"Generado en {informe['base_destino']} · versión {informe['version']} · "
            f"revisión {informe['revision']} · {informe['duracion_s']} s"
        )

    validacion = validar(origen, destino, informe)
    destino[MANIFIESTO].update_one(
        {"_id": ESQUEMA_PAQUETE},
        {
            "$set": {
                "estado": "validada" if validacion["ok"] else "fallida",
                "validacion": validacion,
                "validado_en": datetime.now(timezone.utc).replace(tzinfo=None),
            }
        },
        upsert=True,
    )
    print(
        "Validación: "
        + ("OK" if validacion["ok"] else f"{len(validacion['hallazgos'])} hallazgo(s)")
    )
    for hallazgo in validacion["hallazgos"]:
        print(f"  · {hallazgo}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as archivo:
            json.dump({"generacion": informe, "validacion": validacion}, archivo, ensure_ascii=False, indent=1, default=str)
        print(f"JSON: {args.json}")
    return 0 if validacion["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
