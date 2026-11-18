"""Pruebas de la carga del dataset: idempotencia y reemplazo acotado."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.db.mongo import DATASETS_COLLECTION, SALES_COLLECTION
from app.services.demo_data import DATASET_ID
from scripts import seed_demo

pytestmark = pytest.mark.integracion


def test_carga_idempotente_y_reemplazo_acotado(mongo_db):
    ventas = mongo_db[SALES_COLLECTION]
    metadatos = mongo_db[DATASETS_COLLECTION]

    # 1) Primera carga.
    assert seed_demo.main([]) == 0
    assert ventas.count_documents({"dataset_id": DATASET_ID}) == 30_000

    # 2) Volver a ejecutar no duplica nada.
    assert seed_demo.main([]) == 0
    assert ventas.count_documents({"dataset_id": DATASET_ID}) == 30_000

    # 3) Un dataset ajeno sobrevive a un --replace (solo se reemplaza el propio).
    ventas.insert_one(
        {
            "dataset_id": "otro_dataset",
            "linea_id": "ajena_1",
            "fecha": datetime(2024, 1, 1),
        }
    )
    assert seed_demo.main(["--replace"]) == 0
    assert ventas.count_documents({"dataset_id": DATASET_ID}) == 30_000
    assert ventas.count_documents({"dataset_id": "otro_dataset"}) == 1

    # 4) --dry-run no escribe nada.
    antes = ventas.count_documents({})
    assert seed_demo.main(["--dry-run"]) == 0
    assert ventas.count_documents({}) == antes

    # 5) Los metadatos describen lo realmente cargado.
    meta = metadatos.find_one({"dataset_id": DATASET_ID})
    assert meta is not None
    assert meta["filas"] == 30_000
    assert meta["origen"] == "sintetico"
    assert meta["moneda"] == "MXN"
    assert meta["dataset_version"] == 1
    assert meta["periodo"] == {"desde": "2024-01-01", "hasta": "2025-12-31"}
    assert meta["pedidos"] > 0


def test_el_dataset_cargado_no_tiene_lineas_duplicadas(mongo_db):
    assert seed_demo.main([]) == 0
    ventas = mongo_db[SALES_COLLECTION]
    total = ventas.count_documents({"dataset_id": DATASET_ID})
    distintos = len(ventas.distinct("linea_id", {"dataset_id": DATASET_ID}))
    assert total == distintos == 30_000
