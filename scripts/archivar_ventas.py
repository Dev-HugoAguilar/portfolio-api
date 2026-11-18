"""Archiva y retira del clúster el dataset sintético de «Ventas / Rentabilidad».

Ese conjunto (30 000 líneas de venta) se cargó para una versión anterior del
portafolio. El sitio publicado no lo consulta: los tres tableros en producción
leen ``/api/v1/marketing``, ``/api/v1/atlas-vial`` y ``/api/v1/cnbv``. En un
clúster gratuito al límite, 18 MiB de líneas que nadie pide son espacio perdido.

El script **no borra nada sin copia**: primero escribe
``data/ventas/sales.jsonl.gz`` (una línea JSON por documento) y el metadato del
dataset, comprueba que se puede leer de vuelta, y solo entonces elimina la
colección. Se conserva ``datasets`` (0,04 MiB) para que ``/api/v1/datasets`` siga
respondiendo con la ficha; si algún día vuelve el tablero, se puede recargar con
``python -m scripts.seed_demo``.

Uso::

    python -m scripts.archivar_ventas            # simulación
    python -m scripts.archivar_ventas --aplicar  # archiva y elimina
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from pathlib import Path

from pymongo import MongoClient

from app.config import get_settings

SALES = "sales"
DATASETS = "datasets"
CARPETA = Path("data/ventas")


def cifras(db) -> dict:
    """Resumen que cualquiera puede volver a comprobar en el archivo."""
    total = 0
    lineas = 0
    datasets = Counter()
    for documento in db[SALES].find({}, {"venta_neta_cent": 1, "margen_cent": 1, "dataset_id": 1}):
        lineas += 1
        total += int(documento.get("venta_neta_cent") or 0)
        datasets[documento.get("dataset_id")] += 1
    return {"lineas": lineas, "venta_neta_cent": total, "por_dataset": dict(datasets)}


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Archiva el dataset sintético de ventas")
    argumentos.add_argument("--aplicar", action="store_true", help="Archiva y elimina la colección.")
    opciones = argumentos.parse_args()

    ajustes = get_settings()
    cliente = MongoClient(ajustes.mongodb_uri, serverSelectionTimeoutMS=15_000, compressors="zlib")
    db = cliente[ajustes.mongodb_db]

    if SALES not in db.list_collection_names():
        print("La colección sales no existe: nada que archivar.")
        cliente.close()
        return 0

    antes = cifras(db)
    st = db.command("collStats", SALES)
    print("antes:", antes)
    print(
        f"tamaño: {st.get('size', 0) / 1048576:.2f} MiB de datos · "
        f"{st.get('totalIndexSize', 0) / 1048576:.2f} MiB de índices"
    )
    metadatos = list(db[DATASETS].find({}, {"_id": 0}))
    if not opciones.aplicar:
        print(f"Se escribiría {CARPETA / 'sales.jsonl.gz'} y se eliminaría la colección sales.")
        print("Modo simulación: usa --aplicar para archivar y eliminar.")
        cliente.close()
        return 0

    CARPETA.mkdir(parents=True, exist_ok=True)
    ruta = CARPETA / "sales.jsonl.gz"
    with gzip.open(ruta, "wt", encoding="utf-8") as archivo:
        for documento in db[SALES].find({}):
            documento.pop("_id", None)
            archivo.write(json.dumps(documento, ensure_ascii=False, default=str) + "\n")
    (CARPETA / "datasets.json").write_text(
        json.dumps(metadatos, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    print(f"archivo escrito: {ruta} ({ruta.stat().st_size / 1048576:.2f} MiB)")

    # Se relee el archivo para comprobar que se puede restaurar antes de borrar.
    comprobacion = Counter()
    with gzip.open(ruta, "rt", encoding="utf-8") as archivo:
        for linea in archivo:
            documento = json.loads(linea)
            comprobacion[documento.get("dataset_id")] += 1
    if dict(comprobacion) != antes["por_dataset"]:
        print("El archivo no se pudo releer con las mismas líneas: no se elimina nada.")
        cliente.close()
        return 1

    db.drop_collection(SALES)
    print("colección eliminada:", SALES)
    print("restaurar con: python -m scripts.seed_demo --replace")
    cliente.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
