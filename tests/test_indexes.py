"""Pruebas del diseño de índices: la consulta representativa debe usar IXSCAN.

Se cargan las 30.000 líneas reales del generador para que el planificador de
MongoDB decida con un tamaño de colección representativo.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.db.mongo import SALES_COLLECTION
from app.models.filters import SalesFilters
from app.repositories.sales_repository import SalesRepository
from app.services import demo_data

pytestmark = pytest.mark.integracion


@pytest.fixture()
def dataset_completo(mongo_db):
    documentos, _ = demo_data.generate_dataset()
    mongo_db[SALES_COLLECTION].insert_many(documentos)
    return mongo_db


def test_los_indices_declarados_existen(dataset_completo):
    indices = dataset_completo[SALES_COLLECTION].index_information()
    assert "sales_dataset_fecha" in indices
    assert list(indices["sales_dataset_fecha"]["key"]) == [("dataset_id", 1), ("fecha", 1)]


def test_la_consulta_representativa_usa_ixscan(dataset_completo):
    repo = SalesRepository(dataset_completo)
    filtros = SalesFilters(
        dataset_id=demo_data.DATASET_ID,
        date_from=date(2025, 1, 1),
        date_to=date(2025, 12, 31),
        regions=("norte",),
    )
    plan = repo.explain_sample(filtros.mongo_match())
    texto = json.dumps(plan, default=str)

    assert "IXSCAN" in texto
    assert "COLLSCAN" not in texto


def test_las_agregaciones_no_recorren_la_coleccion_completa(dataset_completo):
    repo = SalesRepository(dataset_completo)
    filtros = SalesFilters(
        dataset_id=demo_data.DATASET_ID,
        date_from=date(2024, 6, 1),
        date_to=date(2024, 6, 30),
    )
    pipeline = [{"$match": filtros.mongo_match()}]
    plan = dataset_completo.command(
        {
            "explain": {
                "aggregate": SALES_COLLECTION,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
    )

    texto = json.dumps(plan, default=str)
    assert "IXSCAN" in texto
    assert "COLLSCAN" not in texto
    # Un mes del dataset demostrativo son ~1.100 líneas, no 30.000.
    assert repo.count(filtros.mongo_match()) < 2_000
