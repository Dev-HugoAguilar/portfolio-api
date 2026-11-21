"""Pruebas del diseño de índices.

Estas pruebas comprueban **lo que el producto garantiza** sobre ``INDEX_SPECS``:
que en una base vacía los índices declarados existan con la forma documentada,
que ``ensure_indexes`` sea idempotente y que las consultas representativas usen
IXSCAN en lugar de COLLSCAN.

Contexto de la colección archivada: ``sales`` (el dataset sintético de «Ventas /
Rentabilidad») se archivó fuera del clúster y su índice **dejó de declararse**
(ver el comentario de ``app/db/indexes.py``): declararlo volvería a crear la
colección vacía al arrancar la API. Por eso no hay prueba que exija un índice en
``sales``; hay, en cambio, una que exige que ``ensure_indexes`` no lo cree.
"""

from __future__ import annotations

import json

import pytest

from app.db.indexes import INDEX_SPECS, ensure_indexes
from app.db.mongo import (
    DATASETS_COLLECTION,
    M5_AGREGADOS,
    SALES_COLLECTION,
)
from tests.dataset_fixture import METADATOS

pytestmark = pytest.mark.integracion


def _claves(indice: dict) -> list[tuple[str, int]]:
    """Claves del índice como lista de tuplas (pymongo devuelve un SON)."""
    return [(campo, indice["key"][campo]) for campo in indice["key"]]


def test_los_indices_declarados_existen(mongo_db):
    """Cada especificación de ``INDEX_SPECS`` está creada y con las claves dadas.

    Es la garantía que necesita el despliegue: la API crea sus índices al
    arrancar y la forma de cada uno debe coincidir con la declarada.
    """
    assert INDEX_SPECS, "no hay índices declarados"

    faltantes: list[str] = []
    distintos: list[str] = []
    for coleccion, nombre, claves, _opciones in INDEX_SPECS:
        existentes = {
            indice["name"]: indice
            for indice in mongo_db[coleccion].list_indexes()
        }
        if nombre not in existentes:
            faltantes.append(f"{coleccion}.{nombre}")
            continue
        if _claves(existentes[nombre]) != list(claves):
            distintos.append(
                f"{coleccion}.{nombre}: {_claves(existentes[nombre])} != {list(claves)}"
            )

    assert not faltantes, "índices declarados que no existen: " + ", ".join(faltantes)
    assert not distintos, "índices con claves distintas a las declaradas: " + "; ".join(distintos)


def test_ensure_indexes_es_idempotente(mongo_db):
    """Volver a llamar a ``ensure_indexes`` no crea nada de nuevo."""
    assert ensure_indexes(mongo_db) == []


def test_ensure_indexes_no_recrea_la_coleccion_archivada(mongo_db):
    """``sales`` no está en ``INDEX_SPECS`` ni recibe índices al arrancar."""
    assert SALES_COLLECTION not in {coleccion for coleccion, *_ in INDEX_SPECS}

    nombre = "sales_dataset_fecha"
    indice = next(
        (spec for spec in INDEX_SPECS if spec[1] == nombre),
        None,
    )
    assert indice is None, "el índice archivado no debe declararse"

    creados = ensure_indexes(mongo_db, solo=(SALES_COLLECTION,))
    assert creados == [], "ensure_indexes no debe crear índices para sales"


def test_la_consulta_por_clave_natural_usa_ixscan(mongo_db):
    """Una igualdad sobre la clave natural usa el índice declarado, no COLLSCAN."""
    mongo_db[DATASETS_COLLECTION].insert_one(dict(METADATOS))

    plan = mongo_db[DATASETS_COLLECTION].find(
        {"dataset_id": METADATOS["dataset_id"]}
    ).explain()
    texto = json.dumps(plan, default=str)

    assert "IXSCAN" in texto
    assert "COLLSCAN" not in texto


def test_la_consulta_agregada_por_dimension_usa_ixscan(mongo_db):
    """El patrón ``(version, dimension, clave)`` también resuelve por índice."""
    documentos = [
        {
            "version": "prueba-indices",
            "dimension": "tienda",
            "clave": f"clave-{indice}",
            "valor": indice,
        }
        for indice in range(3)
    ]
    mongo_db[M5_AGREGADOS].insert_many([dict(documento) for documento in documentos])
    try:
        plan = mongo_db[M5_AGREGADOS].find(
            {"version": "prueba-indices", "dimension": "tienda", "clave": "clave-1"}
        ).explain()
        texto = json.dumps(plan, default=str)

        assert "IXSCAN" in texto
        assert "COLLSCAN" not in texto
    finally:
        # Solo se retiran los documentos de esta prueba: nunca la colección.
        mongo_db[M5_AGREGADOS].delete_many({"version": "prueba-indices"})
