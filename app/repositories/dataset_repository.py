"""Repositorio del catálogo de datasets.

La colección ``datasets`` guarda **metadatos** del conjunto de datos (no filas de
venta). Un dataset cargado se considera inmutable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pymongo.database import Database

from app.db.mongo import DATASETS_COLLECTION


class DatasetRepository:
    def __init__(self, db: Database) -> None:
        self._col = db[DATASETS_COLLECTION]

    def list_all(self) -> list[dict]:
        return list(self._col.find({}, projection={"_id": 0}).sort("dataset_id", 1))

    def get(self, dataset_id: str) -> dict | None:
        return self._col.find_one({"dataset_id": dataset_id}, projection={"_id": 0})

    def upsert_metadata(self, metadata: dict) -> None:
        """Crea o actualiza los metadatos del dataset indicado (idempotente)."""
        metadata = dict(metadata)
        metadata.setdefault("actualizado_en", datetime.now(timezone.utc))
        self._col.update_one(
            {"dataset_id": metadata["dataset_id"]},
            {"$set": metadata},
            upsert=True,
        )
