"""Copia las colecciones de un tablero desde MongoDB local a Atlas.

La migración es **aditiva**: no borra la copia local (queda como respaldo de la
migración) y no toca colecciones de otros tableros. Cada tablero se identifica por
el prefijo de sus colecciones.

Uso:
    python -m scripts.migrar_a_atlas --tablero comparativo --verificar
    python -m scripts.migrar_a_atlas --tablero atlas-vial
    python -m scripts.migrar_a_atlas --tablero ventas
    python -m scripts.migrar_a_atlas --tablero todos --forzar

Requiere `MONGODB_URI` de destino (Atlas) en el entorno, ya resuelto por la
configuración de la aplicación.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

from app.config import get_settings
from app.db.indexes import INDEX_SPECS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrar_atlas")

SALIDA_OK = 0
SALIDA_ERROR = 1

#: Origen de la migración (la copia local verificada).
ORIGEN = "mongodb://127.0.0.1:27017/hugo_portfolio"

#: Colecciones por tablero. El prefijo evita colisiones entre casos.
TABLEROS = {
    "comparativo": ["cnbv_boletines", "cnbv_hechos", "cnbv_indicadores"],
    "atlas-vial": ["atlas_hechos", "atlas_importaciones", "atlas_estado", "atlas_bloqueos"],
    "ventas": ["sales", "datasets"],
}

#: Índices que necesita cada tablero si su colección aún no los tiene.
INDICES = {nombre for nombre, _, _, _ in INDEX_SPECS}

LOTE = 2_000


def _copiar(origen, destino, coleccion: str, forzar: bool) -> dict:
    """Copia una colección por lotes; no borra el origen."""
    if coleccion not in origen.list_collection_names():
        return {"coleccion": coleccion, "estado": "no existe en el origen"}

    if destino[coleccion].count_documents({}, limit=1) and not forzar:
        existentes = destino[coleccion].count_documents({})
        return {
            "coleccion": coleccion,
            "estado": "ya tenía datos (no se toca)",
            "documentos": existentes,
        }

    if forzar:
        destino[coleccion].delete_many({})

    total = 0
    lote: list[dict] = []
    for documento in origen[coleccion].find({}):
        lote.append(documento)
        if len(lote) >= LOTE:
            destino[coleccion].insert_many(lote, ordered=False)
            total += len(lote)
            lote = []
    if lote:
        destino[coleccion].insert_many(lote, ordered=False)
        total += len(lote)
    return {"coleccion": coleccion, "estado": "copiada", "documentos": total}


def _tamano(db, coleccion: str) -> dict:
    stats = db.command("collStats", coleccion)
    return {
        "documentos": stats.get("count", 0),
        "datos_mib": round(stats.get("size", 0) / 1048576, 2),
        "indices_mib": round(stats.get("totalIndexSize", 0) / 1048576, 2),
        "indices": sorted(info["name"] for info in db[coleccion].list_indexes()),
    }


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Migración de tableros a Atlas")
    argumentos.add_argument(
        "--tablero",
        required=True,
        choices=[*TABLEROS, "todos"],
        help="Tablero a migrar (o «todos»).",
    )
    argumentos.add_argument("--forzar", action="store_true", help="Reemplaza los datos del destino.")
    argumentos.add_argument(
        "--verificar", action="store_true", help="Solo compara origen y destino."
    )
    opciones = argumentos.parse_args()

    ajustes = get_settings()
    if not ajustes.mongodb_uri.startswith("mongodb+srv"):
        print(
            "El destino no es un clúster de Atlas: revisa MONGODB_URI en el entorno.",
            file=sys.stderr,
        )
        return SALIDA_ERROR

    destinos = TABLEROS if opciones.tablero == "todos" else {opciones.tablero: TABLEROS[opciones.tablero]}
    resultados: dict[str, list[dict]] = {}

    try:
        origen_cliente = MongoClient(ORIGEN, serverSelectionTimeoutMS=5_000)
        origen = origen_cliente["hugo_portfolio"]
        destino_cliente = MongoClient(ajustes.mongodb_uri, serverSelectionTimeoutMS=10_000, compressors="zlib")
        destino = destino_cliente[ajustes.mongodb_db]

        for tablero, colecciones in destinos.items():
            print(f"\n== {tablero} ==")
            filas = []
            for coleccion in colecciones:
                if opciones.verificar:
                    if coleccion not in destino.list_collection_names():
                        filas.append({"coleccion": coleccion, "estado": "falta en Atlas"})
                        continue
                    origen_n = origen[coleccion].count_documents({})
                    destino_n = destino[coleccion].count_documents({})
                    filas.append(
                        {
                            "coleccion": coleccion,
                            "estado": "ok" if origen_n == destino_n else "discrepancia",
                            "origen": origen_n,
                            "destino": destino_n,
                            **_tamano(destino, coleccion),
                        }
                    )
                else:
                    filas.append(_copiar(origen, destino, coleccion, opciones.forzar))
            # Índices solo de las colecciones migradas (no toca otros tableros).
            creados = []
            for spec_coleccion, nombre_indice, claves, opciones in INDEX_SPECS:
                if spec_coleccion not in colecciones:
                    continue
                if spec_coleccion not in destino.list_collection_names():
                    continue
                existentes = {info["name"] for info in destino[spec_coleccion].list_indexes()}
                if nombre_indice in existentes:
                    continue
                try:
                    destino[spec_coleccion].create_index(claves, name=nombre_indice, **opciones)
                    creados.append(f"{spec_coleccion}.{nombre_indice}")
                except Exception as error:  # noqa: BLE001 - se informa y se sigue
                    print(f"  aviso: no se pudo crear {nombre_indice}: {type(error).__name__}")
            for fila in filas:
                coleccion = fila["coleccion"]
                if coleccion in destino.list_collection_names():
                    fila.update(_tamano(destino, coleccion))
                print("  " + json.dumps(fila, ensure_ascii=False))
            if creados:
                print(f"  índices asegurados: {', '.join(creados)}")
            resultados[tablero] = filas

        stats = destino.command("dbStats", scale=1)
        print(
            f"\nAtlas «{ajustes.mongodb_db}»: {stats.get('objects')} documentos · "
            f"datos {stats.get('dataSize', 0) / 1048576:.1f} MiB · "
            f"índices {stats.get('indexSize', 0) / 1048576:.2f} MiB"
        )
        return SALIDA_OK
    except PyMongoError as error:
        print(f"Fallo de MongoDB: {type(error).__name__}: {str(error)[:160]}", file=sys.stderr)
        return SALIDA_ERROR
    finally:
        try:
            origen_cliente.close()
            destino_cliente.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
