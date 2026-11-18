"""Sondas de salud.

* ``/live``: el proceso responde. No toca MongoDB.
* ``/ready``: además comprueba que MongoDB conteste un ``ping``.

Ninguna de las dos expone credenciales, cadenas de conexión ni detalles internos
del servidor: solo un estado y la versión de la aplicación.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import DatabaseDep, SettingsDep
from app.models.responses import HealthLive, HealthReady

router = APIRouter(prefix="/health", tags=["Salud"])


@router.get("/live", response_model=HealthLive, summary="Vitalidad del proceso")
def live(settings: SettingsDep) -> HealthLive:
    return HealthLive(status="ok", version=settings.app_version)


@router.get(
    "/ready",
    response_model=HealthReady,
    summary="Disponibilidad (incluye MongoDB)",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": HealthReady,
            "description": "MongoDB no está disponible.",
        }
    },
)
def ready(response: Response, db: DatabaseDep, settings: SettingsDep) -> HealthReady:
    if not db.ping():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthReady(status="unavailable", mongo="down", version=settings.app_version)
    return HealthReady(status="ready", mongo="ok", version=settings.app_version)
