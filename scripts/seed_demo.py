"""Carga el dataset sintético de demostración en MongoDB.

Uso (desde ``backend/``):

    python -m scripts.seed_demo                 # carga si falta (idempotente)
    python -m scripts.seed_demo --dry-run       # solo genera y valida, no escribe
    python -m scripts.seed_demo --replace       # reemplaza SOLO este dataset_id

Garantias:

* Nunca se borran bases de datos ni colecciones completas.
* ``--replace`` elimina unicamente las lineas del ``dataset_id`` demostrativo y
  exige pedirlo de forma explicita.
* Volver a ejecutar sin ``--replace`` no duplica nada: si el dataset ya esta
  cargado, el script termina sin escribir.
* Los importes derivados se recalculan y se comparan antes de insertar.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from app.config import get_settings
from app.db.indexes import ensure_indexes
from app.db.mongo import DATASETS_COLLECTION, SALES_COLLECTION, close_database, init_database
from app.repositories.dataset_repository import DatasetRepository
from app.services import demo_data
from app.services.metrics import line_amounts

CAMPOS_DERIVADOS = ("venta_bruta_cent", "descuento_cent", "venta_neta_cent", "coste_cent", "margen_cent")


def validar(documentos: list[dict]) -> None:
    """Comprueba identificadores unicos e importes derivados."""
    vistos: set[str] = set()
    for documento in documentos:
        linea_id = documento["linea_id"]
        if linea_id in vistos:
            raise ValueError(f"linea_id duplicado: {linea_id}")
        vistos.add(linea_id)

        calculado = line_amounts(
            documento["cantidad"],
            documento["precio_unitario_cent"],
            documento["descuento_cent"],
            documento["coste_unitario_cent"],
        )
        for campo in CAMPOS_DERIVADOS:
            if documento[campo] != calculado[campo]:
                raise ValueError(
                    f"{linea_id}: {campo}={documento[campo]} no coincide con {calculado[campo]}"
                )


def resumen(documentos: list[dict], metadatos: dict) -> str:
    fechas = [documento["fecha"] for documento in documentos]
    return (
        f"  lineas: {len(documentos)}\n"
        f"  pedidos: {metadatos['pedidos']}\n"
        f"  clientes: {metadatos['clientes']} | productos: {metadatos['productos']}\n"
        f"  periodo: {min(fechas).date()} a {max(fechas).date()}\n"
        f"  semilla: {metadatos['semilla']} | esquema: {metadatos['esquema_version']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carga el dataset sintetico de demostracion.")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="reemplaza las lineas existentes de este dataset_id (no toca nada mas)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="genera y valida el dataset sin escribir en MongoDB",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=2000,
        help="documentos por lote de insercion (por defecto 2000)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    dataset_id = demo_data.DATASET_ID
    print(f"Base de datos: {settings.mongodb_db} ({settings.safe_mongo_host})")
    print(f"Dataset: {dataset_id}")

    db = init_database(settings)
    try:
        if not db.ping():
            print(f"[ERROR] MongoDB no responde en {settings.safe_mongo_host}")
            return 1

        ventas = db.db[SALES_COLLECTION]
        existentes = ventas.count_documents({"dataset_id": dataset_id})
        print(f"Lineas ya presentes: {existentes}")

        if args.dry_run:
            documentos, metadatos = demo_data.generate_dataset()
            validar(documentos)
            print("[dry-run] Dataset generado y validado; no se escribe nada.")
            print(resumen(documentos, metadatos))
            return 0

        if existentes and not args.replace:
            print(
                "El dataset ya esta cargado: no se escribe nada. "
                "Usa --replace para recargarlo."
            )
            if db.db[DATASETS_COLLECTION].find_one({"dataset_id": dataset_id}) is None:
                _, metadatos = demo_data.generate_dataset()
                metadatos["filas"] = existentes
                DatasetRepository(db.db).upsert_metadata(metadatos)
                print("Metadatos del dataset regenerados.")
            return 0

        # Los indices van antes de la carga: la validacion y el conteo los usan.
        ensure_indexes(db.db)

        inicio = time.perf_counter()
        documentos, metadatos = demo_data.generate_dataset()
        validar(documentos)
        print(f"Dataset generado en {time.perf_counter() - inicio:.2f} s")

        if args.replace and existentes:
            borrados = ventas.delete_many({"dataset_id": dataset_id}).deleted_count
            print(f"Reemplazo: {borrados} lineas del dataset {dataset_id} eliminadas.")

        inicio = time.perf_counter()
        lote = max(100, args.batch_size)
        insertados = 0
        for posicion in range(0, len(documentos), lote):
            parte = documentos[posicion : posicion + lote]
            ventas.insert_many(parte, ordered=False)
            insertados += len(parte)
        duracion = time.perf_counter() - inicio

        metadatos["filas"] = insertados
        metadatos["cargado_en"] = datetime.now(timezone.utc)
        DatasetRepository(db.db).upsert_metadata(metadatos)

        total = ventas.count_documents({"dataset_id": dataset_id})
        print(f"Insertadas {insertados} lineas en {duracion:.2f} s")
        print(resumen(documentos, metadatos))
        print(f"Verificacion: {total} lineas del dataset en la coleccion {SALES_COLLECTION}")
        return 0 if total == insertados else 1
    except Exception as error:  # noqa: BLE001 - el script informa y falla
        print(f"[ERROR] {type(error).__name__}: {error}")
        return 1
    finally:
        close_database()


if __name__ == "__main__":
    sys.exit(main())
