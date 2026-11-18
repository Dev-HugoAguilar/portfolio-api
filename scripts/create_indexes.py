"""Crea los índices de MongoDB que falten.

Uso (desde ``backend/``):

    python -m scripts.create_indexes

Es idempotente: si el índice ya existe no se toca. No borra colecciones ni datos.
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.db.indexes import INDEX_SPECS, ensure_indexes
from app.db.mongo import DATASETS_COLLECTION, SALES_COLLECTION, close_database, init_database


def main() -> int:
    settings = get_settings()
    db = init_database(settings)
    try:
        if not db.ping():
            print(f"[ERROR] MongoDB no responde en {settings.safe_mongo_host}")
            return 1

        print(f"Base de datos: {settings.mongodb_db}")
        creados = ensure_indexes(db.db)
        if creados:
            print("Indices creados: " + ", ".join(creados))
        else:
            print("No habia indices pendientes.")

        for coleccion, nombre, claves, opciones in INDEX_SPECS:
            print(f"  - {coleccion}.{nombre} {claves} {opciones or ''}")

        ventas = db.db[SALES_COLLECTION].estimated_document_count()
        datasets = db.db[DATASETS_COLLECTION].count_documents({})
        print(f"Documentos en {SALES_COLLECTION}: {ventas}")
        print(f"Datasets registrados: {datasets}")
        return 0
    finally:
        close_database()


if __name__ == "__main__":
    sys.exit(main())
