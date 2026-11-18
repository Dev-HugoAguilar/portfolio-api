"""Consultas a los datos reales de la CNBV.

Todas las consultas filtran por ``dataset_id`` y ``vigente: True``: las
revisiones anteriores de un mismo periodo se conservan en la base pero nunca se
sirven, para no mezclar versiones de la misma cifra.
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo.database import Database

from app.cnbv import sources
from app.db.mongo import CNBV_BOLETINES, CNBV_HECHOS, CNBV_INDICADORES

logger = logging.getLogger(__name__)


def _match_base(dataset_id: str, **extra: Any) -> dict:
    filtro = {"dataset_id": dataset_id, "vigente": True}
    filtro.update(extra)
    return filtro


class CnbvRepository:
    def __init__(self, db: Database, *, max_query_ms: int = 8_000, max_docs: int = 5_000) -> None:
        self._hechos = db[CNBV_HECHOS]
        self._boletines = db[CNBV_BOLETINES]
        self._indicadores = db[CNBV_INDICADORES]
        self._max_query_ms = max_query_ms
        self._max_docs = max_docs

    # ------------------------------------------------------------- catálogo
    def boletines(self, dataset_id: str) -> list[dict]:
        return list(
            self._boletines.find(
                _match_base(dataset_id), projection={"_id": 0}
            ).sort("periodo", -1)
        )

    def boletin(self, dataset_id: str, periodo: str) -> dict | None:
        return self._boletines.find_one(
            _match_base(dataset_id, periodo=periodo), projection={"_id": 0}
        )

    def periodos(self, dataset_id: str) -> list[str]:
        return sorted(self._boletines.distinct("periodo", _match_base(dataset_id)))

    def existe_periodo(self, dataset_id: str, periodo: str) -> bool:
        return self._boletines.count_documents(
            _match_base(dataset_id, periodo=periodo), limit=1
        ) > 0

    def conceptos(self, dataset_id: str) -> list[dict]:
        """Catálogo de conceptos, con el nombre del periodo más reciente."""
        pipeline = [
            {"$match": _match_base(dataset_id)},
            {"$sort": {"periodo": -1}},
            {
                "$group": {
                    "_id": "$concepto_id",
                    "nombre": {"$first": "$concepto_nombre"},
                    "seccion": {"$first": "$seccion"},
                    "naturaleza": {"$first": "$naturaleza"},
                    "unidad": {"$first": "$unidad"},
                    "orden": {"$min": "$fila"},
                    "periodos": {"$addToSet": "$periodo"},
                }
            },
            {"$sort": {"orden": 1}},
            {
                "$project": {
                    "_id": 0,
                    "concepto_id": "$_id",
                    "nombre": 1,
                    "seccion": 1,
                    "naturaleza": 1,
                    "unidad": 1,
                    "orden": 1,
                    "periodos_con_dato": {"$size": "$periodos"},
                }
            },
        ]
        return list(self._hechos.aggregate(pipeline, allowDiskUse=True, maxTimeMS=self._max_query_ms))

    def concepto(self, dataset_id: str, concepto_id: str) -> dict | None:
        """Un concepto concreto, sin recorrer todo el catálogo.

        Se lee del hecho más reciente de ese concepto (usa el índice
        ``(dataset_id, concepto_id, institucion_id, periodo)``) y se cuenta en
        cuántos periodos aparece.
        """
        documento = self._hechos.find_one(
            _match_base(dataset_id, concepto_id=concepto_id),
            projection={
                "_id": 0,
                "concepto_id": 1,
                "concepto_nombre": 1,
                "seccion": 1,
                "naturaleza": 1,
                "unidad": 1,
                "fila": 1,
            },
            sort=[("periodo", -1)],
        )
        if documento is None:
            return None
        periodos = self._hechos.distinct(
            "periodo", _match_base(dataset_id, concepto_id=concepto_id)
        )
        return {
            "concepto_id": documento["concepto_id"],
            "nombre": documento["concepto_nombre"],
            "seccion": documento["seccion"],
            "naturaleza": documento["naturaleza"],
            "unidad": documento["unidad"],
            "orden": int(documento.get("fila", 0)),
            "periodos_con_dato": len(periodos),
        }

    def instituciones(self, dataset_id: str, periodo: str | None = None) -> list[dict]:
        match = _match_base(dataset_id)
        if periodo:
            match["periodo"] = periodo
        pipeline = [
            {"$match": match},
            {"$sort": {"periodo": 1}},
            {
                "$group": {
                    "_id": "$institucion_id",
                    "nombre": {"$last": "$institucion_nombre"},
                    "es_sistema": {"$max": "$es_sistema"},
                    "periodos": {"$addToSet": "$periodo"},
                }
            },
            {"$sort": {"es_sistema": -1, "nombre": 1}},
            {
                "$project": {
                    "_id": 0,
                    "institucion_id": "$_id",
                    "nombre": 1,
                    "es_sistema": 1,
                    "periodos_con_dato": {"$size": "$periodos"},
                }
            },
        ]
        return list(self._hechos.aggregate(pipeline, allowDiskUse=True, maxTimeMS=self._max_query_ms))

    def celda_ausentes(self, dataset_id: str, periodo: str) -> int:
        boletin = self.boletin(dataset_id, periodo)
        return int(boletin.get("celdas_ausentes", 0)) if boletin else 0

    # ----------------------------------------------------------------- datos
    def valores_periodo(self, dataset_id: str, periodo: str, concepto_id: str) -> list[dict]:
        cursor = (
            self._hechos.find(
                _match_base(dataset_id, periodo=periodo, concepto_id=concepto_id),
                projection={"_id": 0},
            )
            .sort("institucion_nombre", 1)
            .limit(self._max_docs)
            .max_time_ms(self._max_query_ms)
        )
        return list(cursor)

    def serie(
        self,
        dataset_id: str,
        concepto_id: str,
        instituciones: tuple[str, ...] = (),
        desde: str | None = None,
        hasta: str | None = None,
    ) -> list[dict]:
        filtro = _match_base(dataset_id, concepto_id=concepto_id)
        if instituciones:
            filtro["institucion_id"] = {"$in": list(instituciones)}
        if desde or hasta:
            rango: dict = {}
            if desde:
                rango["$gte"] = desde
            if hasta:
                rango["$lte"] = hasta
            filtro["periodo"] = rango
        cursor = (
            self._hechos.find(
                filtro,
                projection={
                    "_id": 0,
                    "periodo": 1,
                    "fecha": 1,
                    "institucion_id": 1,
                    "institucion_nombre": 1,
                    "es_sistema": 1,
                    "valor": 1,
                    "unidad": 1,
                },
            )
            .sort([("periodo", 1), ("institucion_nombre", 1)])
            .limit(self._max_docs)
            .max_time_ms(self._max_query_ms)
        )
        return list(cursor)

    def periodos_con_dato(self, dataset_id: str, concepto_id: str) -> list[str]:
        return sorted(
            self._hechos.distinct(
                "periodo", _match_base(dataset_id, concepto_id=concepto_id)
            )
        )

    def detalle(
        self,
        filtro: dict,
        *,
        orden: str,
        skip: int,
        limit: int,
    ) -> list[dict]:
        campo = {
            "valor": "valor",
            "institucion": "institucion_nombre",
            "concepto": "concepto_nombre",
            "periodo": "periodo",
        }[orden]
        cursor = (
            self._hechos.find(filtro, projection={"_id": 0})
            .sort([(campo, 1), ("periodo", 1), ("institucion_nombre", 1), ("fila", 1)])
            .skip(max(0, skip))
            .limit(max(1, limit))
            .max_time_ms(self._max_query_ms)
        )
        return list(cursor)

    def contar(self, filtro: dict) -> int:
        return self._hechos.count_documents(filtro, maxTimeMS=self._max_query_ms)

    # ------------------------------------------------------------ indicadores
    def indicadores_catalogo(self, dataset_id: str) -> list[dict]:
        pipeline = [
            {"$match": _match_base(dataset_id)},
            {
                "$group": {
                    "_id": "$indicador_id",
                    "nombre": {"$first": "$indicador_nombre"},
                    "nota": {"$first": "$nota"},
                    "unidad": {"$first": "$unidad"},
                    "periodos": {"$addToSet": "$periodo"},
                }
            },
            {"$sort": {"_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "indicador_id": "$_id",
                    "nombre": 1,
                    "nota": 1,
                    "unidad": 1,
                    "periodos_con_dato": {"$size": "$periodos"},
                }
            },
        ]
        return list(
            self._indicadores.aggregate(pipeline, allowDiskUse=True, maxTimeMS=self._max_query_ms)
        )

    def indicadores(self, dataset_id: str, periodo_boletin: str) -> list[dict]:
        cursor = (
            self._indicadores.find(
                _match_base(dataset_id, periodo_boletin=periodo_boletin),
                projection={"_id": 0},
            )
            .sort([("indicador_id", 1), ("periodo", 1), ("institucion_nombre", 1)])
            .max_time_ms(self._max_query_ms)
        )
        return list(cursor)

    # ----------------------------------------------------------------- apoyo
    def resumen_cobertura(self, dataset_id: str) -> dict:
        """Cobertura publicada: periodos, hechos, indicadores y filas del original.

        Las filas del archivo no se guardan en Atlas (el .xlsx se conserva fuera):
        su número sale de la ficha de cada boletín, que lo registra al importar.
        """
        periodos = self.periodos(dataset_id)
        filas_originales = int(
            sum(
                int(boletin.get("filas_originales") or 0)
                for boletin in self._boletines.find(
                    _match_base(dataset_id), {"filas_originales": 1}
                )
            )
        )
        return {
            "dataset_id": dataset_id,
            "periodos_importados": len(periodos),
            "periodo_min": periodos[0] if periodos else None,
            "periodo_max": periodos[-1] if periodos else None,
            "hechos": self._hechos.count_documents(_match_base(dataset_id)),
            "filas_originales": filas_originales,
            "indicadores": self._indicadores.count_documents(_match_base(dataset_id)),
            "fuente": sources.NOMBRE_FUENTE,
        }
