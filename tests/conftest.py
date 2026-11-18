"""Configuración de las pruebas.

Las pruebas de integración usan un **MongoDB real** pero en una base de datos
aislada y dedicada (``hugo_portfolio_test`` por defecto). Reglas de seguridad de
este suite:

* El nombre de la base debe terminar en ``_test``; si no, las pruebas abortan.
* Nunca se ejecuta ``drop_database`` ni ``drop_collection``: solo se vacían las
  dos colecciones de la base de pruebas antes y después de cada prueba.
* Si MongoDB no está disponible, las pruebas de integración se omiten con un
  mensaje claro en lugar de fallar.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

TEST_DB = os.environ.get("MONGODB_TEST_DB", "hugo_portfolio_test")
TEST_URI = os.environ.get("MONGODB_TEST_URI", f"mongodb://127.0.0.1:27017/{TEST_DB}")

# Deben fijarse antes de importar app.config (get_settings está cacheado).
os.environ["MONGODB_DB"] = TEST_DB
os.environ["MONGODB_URI"] = TEST_URI
os.environ.setdefault("LOG_LEVEL", "warning")
# La copia original de la extracción va a una carpeta temporal: las pruebas nunca
# escriben en la carpeta de datos real del proyecto.
os.environ.setdefault("MARKETING_DATA_DIR", tempfile.mkdtemp(prefix="marketing-test-"))
# El proyecto M5 tiene destino propio y nunca hereda MONGODB_URI. En pruebas
# apunta a la misma base aislada, que también termina en _test.
os.environ["COMERCIAL_MONGODB_URI"] = TEST_URI
os.environ["COMERCIAL_MONGODB_DB"] = TEST_DB

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pymongo import MongoClient  # noqa: E402
from pymongo.errors import PyMongoError  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.db.indexes import ensure_indexes  # noqa: E402
from app.db.mongo import (  # noqa: E402
    CNBV_BOLETINES,
    CNBV_FILAS,
    CNBV_HECHOS,
    CNBV_INDICADORES,
    DATASETS_COLLECTION,
    SALES_COLLECTION,
)
from app.main import create_app  # noqa: E402
from tests.dataset_fixture import LINEAS, METADATOS  # noqa: E402

if not TEST_DB.endswith("_test"):  # pragma: no cover - protección deliberada
    raise RuntimeError(
        f"La base de pruebas debe terminar en '_test' (recibido: {TEST_DB!r})."
    )


@pytest.fixture(scope="session")
def mongo_client():
    client = MongoClient(TEST_URI, serverSelectionTimeoutMS=1_500)
    try:
        client.admin.command("ping")
    except PyMongoError as error:
        client.close()
        pytest.skip(
            f"MongoDB no disponible en {TEST_URI} ({type(error).__name__}): "
            "se omiten las pruebas de integración."
        )
    yield client
    client.close()


@pytest.fixture(scope="session")
def settings(mongo_client) -> Settings:
    ajustes = get_settings()
    assert ajustes.mongodb_db == TEST_DB, "la app debe apuntar a la base de pruebas"
    return ajustes


COLECCIONES_CNBV = [CNBV_BOLETINES, CNBV_FILAS, CNBV_HECHOS, CNBV_INDICADORES]


@pytest.fixture(scope="session", autouse=True)
def limpiar_cnbv_al_inicio(mongo_client):
    """Deja vacías las colecciones CNBV de la base de pruebas al empezar.

    Las pruebas de la CNBV importan boletines reales; se limpian una sola vez para
    no arrastrar datos de ejecuciones anteriores. Nunca se borran bases ni
    colecciones de otros proyectos.
    """
    db = mongo_client[TEST_DB]
    for nombre in COLECCIONES_CNBV:
        db[nombre].delete_many({})
    yield


@pytest.fixture()
def mongo_db(mongo_client, settings):
    """Base de pruebas vacía (ventas sintéticas), con los índices creados.

    Solo se vacían las colecciones del dataset sintético: las de la CNBV las
    gestionan sus propias pruebas, que importan archivos reales y tardan varios
    segundos. Nunca se borran bases ni colecciones de otros proyectos.
    """
    db = mongo_client[TEST_DB]
    colecciones = [SALES_COLLECTION, DATASETS_COLLECTION]
    for nombre in colecciones:
        db[nombre].delete_many({})
    ensure_indexes(db)
    yield db
    for nombre in colecciones:
        db[nombre].delete_many({})


@pytest.fixture()
def sample_dataset(mongo_db) -> dict:
    """Carga el dataset de 6 líneas con resultados calculados a mano."""
    mongo_db[SALES_COLLECTION].insert_many([dict(linea) for linea in LINEAS])
    mongo_db[DATASETS_COLLECTION].insert_one(dict(METADATOS))
    return {"lineas": LINEAS, "metadatos": METADATOS}


@pytest.fixture()
def client(sample_dataset, settings):
    """Cliente HTTP con el ciclo de vida de la aplicación activo."""
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
