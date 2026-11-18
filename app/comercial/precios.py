"""Precios de venta de M5: lectura, validación e importación versionada.

Qué entra
---------
``sell_prices.csv`` (``store_id × item_id × wm_yr_wk → sell_price``). Es el único
archivo de precios de la competición y **no** contiene promociones, descuentos ni
inventario: solo el precio de venta observado por semana.

Reglas que impone este módulo
-----------------------------
- **Clave única** ``(store_id, item_id, wm_yr_wk)``: si se repite, se aborta.
- **Semanas reales**: ``wm_yr_wk`` debe existir en el calendario importado; el cruce
  con ventas se hace por tienda y semana, nunca multiplicando filas de ventas.
- **Cantidades admisibles**: precio finito y mayor que cero. Un precio ausente se
  guarda como ausencia (no se rellena con cero) y **no** se interpreta como falta de
  inventario ni como promoción.
- **Idempotencia**: la versión de precios se deriva del contenido (hash + filas +
  semanas), así que reimportar el mismo archivo no duplica documentos.

Almacenamiento
--------------
``m5_precios``: un documento por ``(store_id, wm_yr_wk)`` con dos listas paralelas
(``items`` y ``precios``). Son ~2 700 documentos pequeños en lugar de 6,8 millones de
filas sueltas, y se leen completos para construir la matriz de precios del modelo.
``m5_precios_manifest``: hash, conteos, cobertura, tamaño y validaciones.
"""

from __future__ import annotations

import csv
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from pymongo.database import Database

from app.comercial import fuentes
from app.db.mongo import M5_CALENDARIO, M5_CATALOGO, M5_PRECIOS, M5_PRECIOS_MANIFEST

logger = logging.getLogger(__name__)

COLUMNAS_PRECIOS = ("store_id", "item_id", "wm_yr_wk", "sell_price")
ESQUEMA_PRECIOS = "m5-precios-1"
LOTE = 200


class ErrorPrecios(RuntimeError):
    """El archivo de precios no cumple las validaciones."""


def _hash(ruta: Path) -> str:
    return fuentes.hash_archivo(ruta)


def crear_version_precios(*, hash_archivo: str, filas: int, semanas: int) -> str:
    """Versión estable de los precios (mismo contenido ⇒ misma versión)."""
    firma = f"precios|{hash_archivo}|{filas}|{semanas}"
    return "m5p-" + hashlib.sha256(firma.encode("utf-8")).hexdigest()[:12]


def leer_precios(carpeta: Path, *, limite: int | None = None) -> dict:
    """Lee ``sell_prices.csv`` en arreglos tipados, sin escribir nada.

    Devuelve los índices de tienda, artículo y semana junto con los precios, más el
    conteo de filas. Los identificadores se conservan como texto en ``tiendas``,
    ``items`` y ``semanas``.
    """
    ruta = Path(carpeta) / fuentes.ARCHIVO_PRECIOS
    if not ruta.exists():
        raise ErrorPrecios(f"Falta {fuentes.ARCHIVO_PRECIOS} en {carpeta}")

    tiendas: dict[str, int] = {}
    items: dict[str, int] = {}
    semanas: dict[int, int] = {}
    i_tienda: list[int] = []
    i_item: list[int] = []
    i_semana: list[int] = []
    precios: list[float] = []

    with ruta.open("r", encoding="utf-8", newline="") as manejador:
        lector = csv.reader(manejador)
        cabecera = next(lector)
        faltan = [columna for columna in COLUMNAS_PRECIOS if columna not in cabecera]
        if faltan:
            raise ErrorPrecios(f"Faltan columnas en {ruta.name}: {', '.join(faltan)}")
        posicion = {columna: cabecera.index(columna) for columna in COLUMNAS_PRECIOS}
        for numero, fila in enumerate(lector, start=2):
            if limite and len(precios) >= limite:
                break
            try:
                tienda = fila[posicion["store_id"]].strip()
                item = fila[posicion["item_id"]].strip()
                semana = int(fila[posicion["wm_yr_wk"]])
                precio = float(fila[posicion["sell_price"]])
            except (IndexError, ValueError) as exc:
                raise ErrorPrecios(f"Fila {numero} ilegible: {fila[:4]}") from exc
            if not np.isfinite(precio) or precio <= 0:
                raise ErrorPrecios(f"Precio no admisible en la fila {numero}: {precio!r}")
            i_tienda.append(tiendas.setdefault(tienda, len(tiendas)))
            i_item.append(items.setdefault(item, len(items)))
            i_semana.append(semanas.setdefault(semana, len(semanas)))
            precios.append(precio)

    if not precios:
        raise ErrorPrecios("El archivo de precios está vacío")

    return {
        "carpeta": str(carpeta),
        "archivo": ruta.name,
        "bytes": ruta.stat().st_size,
        "hash": _hash(ruta),
        "filas": len(precios),
        "tiendas": sorted(tiendas, key=tiendas.get),
        "items": sorted(items, key=items.get),
        "semanas": sorted(semanas),
        "i_tienda": np.asarray(i_tienda, dtype=np.int32),
        "i_item": np.asarray(i_item, dtype=np.int32),
        "i_semana": np.asarray(i_semana, dtype=np.int32),
        "precios": np.asarray(precios, dtype=np.float32),
    }


def validar_precios(db: Database, version_ventas: str, datos: dict) -> dict:
    """Valida claves, semanas, catálogo y cobertura **antes** de escribir."""
    catalogo_tiendas = {
        d["store_id"]
        for d in db[M5_CATALOGO].find({"version": version_ventas, "tipo": "tienda"}, {"store_id": 1})
    }
    catalogo_items = {
        d["item_id"]
        for d in db[M5_CATALOGO].find({"version": version_ventas, "tipo": "producto"}, {"item_id": 1})
    }
    semanas_calendario = {
        int(d["wm_yr_wk"]) for d in db[M5_CALENDARIO].find({}, {"wm_yr_wk": 1}) if d.get("wm_yr_wk")
    }

    tiendas_desconocidas = [t for t in datos["tiendas"] if t not in catalogo_tiendas]
    items_desconocidos = [i for i in datos["items"] if i not in catalogo_items]
    semanas_desconocidas = [s for s in datos["semanas"] if s not in semanas_calendario]

    # Unicidad de (store, item, week) con una clave entera compacta.
    ancho_semana = int(datos["i_semana"].max()) + 1
    ancho_item = int(datos["i_item"].max()) + 1
    claves = (
        datos["i_tienda"].astype(np.int64) * (ancho_item * ancho_semana)
        + datos["i_item"].astype(np.int64) * ancho_semana
        + datos["i_semana"].astype(np.int64)
    )
    unicas = np.unique(claves)
    duplicadas = int(claves.size - unicas.size)

    # Cobertura: series (tienda × artículo) con al menos un precio y semanas ocupadas.
    pares = np.unique(
        datos["i_tienda"].astype(np.int64) * ancho_item + datos["i_item"].astype(np.int64)
    )
    rejilla = len(datos["tiendas"]) * len(datos["items"])

    validaciones = {
        "clave_unica": duplicadas == 0,
        "duplicadas": duplicadas,
        "precios_finitos_positivos": bool(np.all(np.isfinite(datos["precios"])) and np.all(datos["precios"] > 0)),
        "tiendas_desconocidas": tiendas_desconocidas,
        "items_desconocidos": items_desconocidos,
        "semanas_desconocidas": semanas_desconocidas,
        "tiendas": len(datos["tiendas"]),
        "items_con_precio": len(datos["items"]),
        "semanas": len(datos["semanas"]),
        "filas": int(datos["filas"]),
        "pares_tienda_item_con_precio": int(pares.size),
        "pares_de_la_rejilla": int(rejilla),
        "cobertura_pares_pct": round(100 * pares.size / rejilla, 2) if rejilla else None,
        "precio_min": float(datos["precios"].min()),
        "precio_max": float(datos["precios"].max()),
        "precio_medio": round(float(datos["precios"].mean()), 4),
    }
    if not validaciones["clave_unica"]:
        raise ErrorPrecios(
            f"Hay {duplicadas} claves repetidas (store_id × item_id × wm_yr_wk): no se importa."
        )
    if not validaciones["precios_finitos_positivos"]:
        raise ErrorPrecios("Hay precios no finitos o no positivos: no se importa.")
    if tiendas_desconocidas or items_desconocidos:
        raise ErrorPrecios(
            "El archivo de precios trae identificadores que no están en el catálogo: "
            f"{tiendas_desconocidas[:3]} {items_desconocidos[:3]}"
        )
    if semanas_desconocidas:
        raise ErrorPrecios(
            f"Hay {len(semanas_desconocidas)} semanas sin relación en el calendario: "
            f"{semanas_desconocidas[:3]}"
        )
    return validaciones


def importar_precios(db: Database, version_ventas: str, carpeta: Path) -> dict:
    """Importa los precios de forma idempotente y devuelve el manifiesto."""
    datos = leer_precios(carpeta)
    validaciones = validar_precios(db, version_ventas, datos)
    version = crear_version_precios(
        hash_archivo=datos["hash"], filas=datos["filas"], semanas=len(datos["semanas"])
    )
    existente = db[M5_PRECIOS_MANIFEST].find_one({"_id": version})
    if existente:
        logger.info("Precios ya importados (%s): no se reescribe nada", version)
        return {**existente, "reutilizado": True}

    # Se agrupan por (tienda, semana) para dejar documentos pequeños y legibles.
    tiendas = datos["tiendas"]
    semanas = datos["semanas"]
    claves = (
        datos["i_tienda"].astype(np.int64) * len(semanas) + datos["i_semana"].astype(np.int64)
    )
    orden = np.argsort(claves, kind="stable")
    documentos: list[dict] = []
    inicio = 0
    while inicio < orden.size:
        clave = int(claves[orden[inicio]])
        fin = inicio
        while fin < orden.size and int(claves[orden[fin]]) == clave:
            fin += 1
        filas = orden[inicio:fin]
        tienda = tiendas[clave // len(semanas)]
        semana = semanas[clave % len(semanas)]
        documentos.append(
            {
                "_id": f"{version}:{tienda}:{semana}",
                "esquema": ESQUEMA_PRECIOS,
                "version_precios": version,
                "version_ventas": version_ventas,
                "store_id": tienda,
                "wm_yr_wk": semana,
                "items": [datos["items"][int(i)] for i in datos["i_item"][filas]],
                "precios": [round(float(v), 2) for v in datos["precios"][filas]],
            }
        )
        inicio = fin

    db[M5_PRECIOS].delete_many({"version_precios": version})
    for posicion in range(0, len(documentos), LOTE):
        db[M5_PRECIOS].insert_many(documentos[posicion : posicion + LOTE], ordered=False)

    tamano = db.command("collStats", M5_PRECIOS)
    manifiesto = {
        "_id": version,
        "esquema": ESQUEMA_PRECIOS,
        "version_precios": version,
        "version_ventas": version_ventas,
        "archivo": datos["archivo"],
        "carpeta_origen": datos["carpeta"],
        "bytes_archivo": datos["bytes"],
        "hash": datos["hash"],
        "importado_en": datetime.now(timezone.utc),
        "unidad": "precio de venta por unidad (dólares del conjunto)",
        "validaciones": validaciones,
        "documentos": len(documentos),
        "almacenamiento": {
            "documentos": tamano.get("count"),
            "bytes_datos": tamano.get("size"),
            "bytes_almacenamiento": tamano.get("storageSize"),
            "bytes_indices": tamano.get("totalIndexSize"),
        },
        "reutilizado": False,
    }
    db[M5_PRECIOS_MANIFEST].replace_one({"_id": version}, manifiesto, upsert=True)
    logger.info(
        "Precios importados: %s filas, %s documentos, %s semanas",
        datos["filas"],
        len(documentos),
        len(semanas),
    )
    return manifiesto


def manifiesto_vigente(db: Database) -> dict | None:
    """Manifiesto de precios más reciente."""
    return db[M5_PRECIOS_MANIFEST].find_one(sort=[("importado_en", -1)])


def cargar_precios(db: Database, version_precios: str) -> dict:
    """Carga los precios en la forma que consume el modelo.

    Devuelve ``items`` (lista), ``tiendas`` (lista), ``semanas`` (lista ordenada) y
    ``matriz`` ``(n_semanas, n_items, n_tiendas)`` de ``float32`` con ``NaN`` donde no
    hay precio observado: la ausencia se conserva como ausencia.
    """
    documentos = list(
        db[M5_PRECIOS].find(
            {"version_precios": version_precios},
            {"store_id": 1, "wm_yr_wk": 1, "items": 1, "precios": 1, "_id": 0},
        )
    )
    if not documentos:
        raise ErrorPrecios(f"No hay precios importados para la versión {version_precios}")
    items = sorted({item for documento in documentos for item in documento["items"]})
    tiendas = sorted({documento["store_id"] for documento in documentos})
    semanas = sorted({int(documento["wm_yr_wk"]) for documento in documentos})
    pos_item = {item: indice for indice, item in enumerate(items)}
    pos_tienda = {tienda: indice for indice, tienda in enumerate(tiendas)}
    pos_semana = {semana: indice for indice, semana in enumerate(semanas)}
    matriz = np.full((len(semanas), len(items), len(tiendas)), np.nan, dtype=np.float32)
    for documento in documentos:
        semana = pos_semana[int(documento["wm_yr_wk"])]
        tienda = pos_tienda[documento["store_id"]]
        for item, precio in zip(documento["items"], documento["precios"], strict=False):
            matriz[semana, pos_item[item], tienda] = precio
    return {
        "version_precios": version_precios,
        "items": items,
        "tiendas": tiendas,
        "semanas": semanas,
        "matriz": matriz,
    }
