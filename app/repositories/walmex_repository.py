"""Lecturas de «Pulso comercial · Walmex» sobre la versión activa de Atlas.

Todas las consultas filtran por la versión publicada y leen solo agregados: no hay
detalle por tienda, formato, producto ni cliente que servir.
"""

from __future__ import annotations

from typing import Any

from pymongo.database import Database

from app.db.mongo import (
    WALMEX_CATALOGO,
    WALMEX_ESTADO,
    WALMEX_FUENTES,
    WALMEX_HECHOS,
    WALMEX_IMPORTACIONES,
)
from app.models.walmex import WalmexFiltros


class WalmexRepository:
    def __init__(self, db: Database, *, max_query_ms: int = 8_000) -> None:
        self._db = db
        self._hechos = db[WALMEX_HECHOS]
        self._catalogo = db[WALMEX_CATALOGO]
        self._fuentes = db[WALMEX_FUENTES]
        self._importaciones = db[WALMEX_IMPORTACIONES]
        self._estado = db[WALMEX_ESTADO]
        self._max_ms = max_query_ms

    # ------------------------------------------------------------------ base
    def version_activa(self) -> str | None:
        estado = self._estado.find_one({"_id": "activo"})
        return (estado or {}).get("version")

    def estado(self) -> dict[str, Any] | None:
        return self._estado.find_one({"_id": "activo"}, {"_id": 0})

    def catalogo(self, version: str) -> dict[str, Any] | None:
        return self._catalogo.find_one({"version": version}, {"_id": 0})

    def importacion(self, version: str) -> dict[str, Any] | None:
        return self._importaciones.find_one({"version": version}, {"_id": 0})

    def fuentes(self, ids: list[str] | None = None) -> list[dict[str, Any]]:
        consulta: dict[str, Any] = {}
        if ids:
            consulta["id"] = {"$in": ids}
        return list(self._fuentes.find(consulta, {"_id": 0}))

    # ---------------------------------------------------------------- hechos
    def hechos(self, version: str, filtros: WalmexFiltros) -> list[dict[str, Any]]:
        consulta = {"version": version, **filtros.condiciones()}
        return list(
            self._hechos.find(
                consulta,
                {
                    "_id": 0,
                    "periodo": 1,
                    "orden": 1,
                    "mercado": 1,
                    "indicador": 1,
                    "base": 1,
                    "valor": 1,
                    "unidad": 1,
                    "fuente": 1,
                    "publicado": 1,
                    "derivado": 1,
                },
            )
            .sort([("orden", 1)])
            .max_time_ms(self._max_ms)
        )

    def valores(
        self, version: str, mercado: str, indicador: str, base: str | None = None
    ) -> list[dict[str, Any]]:
        consulta: dict[str, Any] = {
            "version": version,
            "mercado": mercado,
            "indicador": indicador,
        }
        if base:
            consulta["base"] = base
        return list(
            self._hechos.find(consulta, {"_id": 0, "periodo": 1, "orden": 1, "base": 1, "valor": 1})
            .sort([("orden", 1)])
            .max_time_ms(self._max_ms)
        )

    def periodos(self, version: str) -> list[dict[str, Any]]:
        return list(
            self._hechos.aggregate(
                [
                    {"$match": {"version": version}},
                    {"$group": {"_id": {"periodo": "$periodo", "orden": "$orden"}}},
                    {"$sort": {"_id.orden": 1}},
                ],
                maxTimeMS=self._max_ms,
            )
        )
