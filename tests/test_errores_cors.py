"""Errores de la API: cabeceras CORS, 503 de base de datos y reintento de lecturas.

Caso real que cubren estas pruebas: con el backend encendido, una consulta pesada
(`/atlas-vial/puntos`) recibió un cierre de conexión del clúster y la API devolvió
un 500 **sin** `Access-Control-Allow-Origin`. El navegador lo informaba como un
problema de CORS («No se pudo contactar con la API del Atlas vial»), que apuntaba
a la causa equivocada. Estas pruebas fijan el comportamiento correcto:

* un fallo interno responde JSON 500 y **conserva** las cabeceras CORS;
* un corte de MongoDB responde 503 (reintentable) y no 500;
* los orígenes no permitidos nunca reciben `Access-Control-Allow-Origin`;
* una lectura idempotente se repite ante un corte transitorio.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect, OperationFailure

from app.config import Settings
from app.db.mongo import reintentar_lectura
from app.main import create_app

ORIGEN = "http://127.0.0.1:8000"


def _cliente(monkeypatch) -> TestClient:
    """App sin ciclo de vida (no toca MongoDB) con dos rutas que fallan a propósito."""
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGEN)
    ajustes = Settings(
        allowed_origins=ORIGEN,
        mongodb_uri="mongodb://127.0.0.1:27017/pruebas_errores",
        mongodb_db="pruebas_errores",
    )
    app = create_app(ajustes)

    @app.get("/api/v1/fallo-interno")
    def fallo_interno() -> None:
        raise RuntimeError("fallo simulado")

    @app.get("/api/v1/fallo-mongo")
    def fallo_mongo() -> None:
        raise AutoReconnect("conexión cerrada por el servidor")

    # Sin `with`: no se ejecuta el lifespan y no se abre ninguna conexión real.
    return TestClient(app, raise_server_exceptions=False)


def test_fallo_interno_conserva_cabeceras_cors(monkeypatch):
    cliente = _cliente(monkeypatch)
    respuesta = cliente.get("/api/v1/fallo-interno", headers={"Origin": ORIGEN})
    assert respuesta.status_code == 500
    assert respuesta.headers["access-control-allow-origin"] == ORIGEN
    assert respuesta.json()["detail"].startswith("Error interno de la API")


def test_fallo_interno_preflight_y_origen_ajeno(monkeypatch):
    cliente = _cliente(monkeypatch)
    ajeno = cliente.get("/api/v1/fallo-interno", headers={"Origin": "http://127.0.0.1:9999"})
    assert ajeno.status_code == 500
    assert "access-control-allow-origin" not in ajeno.headers

    sin_origen = cliente.get("/api/v1/fallo-interno")
    assert sin_origen.status_code == 500
    assert "access-control-allow-origin" not in sin_origen.headers


def test_corte_de_mongodb_responde_503(monkeypatch):
    cliente = _cliente(monkeypatch)
    respuesta = cliente.get("/api/v1/fallo-mongo", headers={"Origin": ORIGEN})
    assert respuesta.status_code == 503
    assert respuesta.headers["access-control-allow-origin"] == ORIGEN
    assert "no respondió a tiempo" in respuesta.json()["detail"]


def test_reintentar_lectura_repite_solo_fallos_transitorios():
    intentos = {"n": 0}

    def lee() -> str:
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise AutoReconnect("conexión cerrada")
        return "datos"

    assert reintentar_lectura(lee, espera_s=0) == "datos"
    assert intentos["n"] == 3

    def siempre_falla() -> str:
        raise AutoReconnect("conexión cerrada")

    with pytest.raises(AutoReconnect):
        reintentar_lectura(siempre_falla, intentos=2, espera_s=0)

    llamadas = {"n": 0}

    def error_de_consulta() -> str:
        llamadas["n"] += 1
        raise OperationFailure("índice inexistente")

    # Un error lógico no se reintenta: se propaga a la primera.
    with pytest.raises(OperationFailure):
        reintentar_lectura(error_de_consulta, espera_s=0)
    assert llamadas["n"] == 1
