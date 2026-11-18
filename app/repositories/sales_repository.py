"""Repositorio de ventas: filtros y agregaciones en MongoDB.

Todas las agregaciones se ejecutan en el servidor. Python nunca descarga la
colección para calcular indicadores: solo recibe resultados ya agrupados.

Nota sobre ``$facet``: las ramas de un mismo ``$facet`` no forman una instantánea
transaccional. Aquí es aceptable porque **el dataset es inmutable** una vez
cargado: no hay escrituras concurrentes durante la vida de la consulta.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Sequence

from pymongo.database import Database

from app.db.mongo import DATASETS_COLLECTION, SALES_COLLECTION
from app.models.filters import DIMENSIONS, SalesFilters

logger = logging.getLogger(__name__)

#: Campo de etiqueta legible por dimensión (para las opciones de filtro).
OPTION_FIELDS: dict[str, tuple[str, str]] = {
    "regions": ("region_slug", "region"),
    "channels": ("canal_slug", "canal"),
    "categories": ("categoria_slug", "categoria"),
    "products": ("producto_id", "producto_nombre"),
    "customers": ("cliente_id", "cliente_nombre"),
}

#: Proyección del detalle (sin ``_id`` interno de Mongo).
LINE_PROJECTION = {
    "_id": 0,
    "linea_id": 1,
    "pedido_id": 1,
    "fecha": 1,
    "cliente_id": 1,
    "cliente_nombre": 1,
    "region": 1,
    "canal": 1,
    "producto_id": 1,
    "producto_nombre": 1,
    "categoria": 1,
    "cantidad": 1,
    "precio_unitario_cent": 1,
    "descuento_cent": 1,
    "coste_unitario_cent": 1,
    "venta_bruta_cent": 1,
    "venta_neta_cent": 1,
    "coste_cent": 1,
    "margen_cent": 1,
    "moneda": 1,
}


def _group_measures() -> dict[str, Any]:
    return {
        "lineas": {"$sum": 1},
        "venta_neta_cent": {"$sum": "$venta_neta_cent"},
        "margen_cent": {"$sum": "$margen_cent"},
    }


class SalesRepository:
    def __init__(self, db: Database, *, max_query_ms: int = 8_000, max_options: int = 250) -> None:
        self._sales = db[SALES_COLLECTION]
        self._datasets = db[DATASETS_COLLECTION]
        self._max_query_ms = max_query_ms
        self._max_options = max_options

    # ------------------------------------------------------------------ KPIs
    def totals(self, match: dict) -> dict:
        """Sumas agregadas del filtro: líneas, pedidos, clientes e importes."""
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": None,
                    "lineas": {"$sum": 1},
                    "unidades": {"$sum": "$cantidad"},
                    "venta_bruta_cent": {"$sum": "$venta_bruta_cent"},
                    "descuento_cent": {"$sum": "$descuento_cent"},
                    "venta_neta_cent": {"$sum": "$venta_neta_cent"},
                    "coste_cent": {"$sum": "$coste_cent"},
                    "margen_cent": {"$sum": "$margen_cent"},
                    "pedidos": {"$addToSet": "$pedido_id"},
                    "clientes": {"$addToSet": "$cliente_id"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "lineas": 1,
                    "unidades": 1,
                    "venta_bruta_cent": 1,
                    "descuento_cent": 1,
                    "venta_neta_cent": 1,
                    "coste_cent": 1,
                    "margen_cent": 1,
                    "pedidos": {"$size": "$pedidos"},
                    "clientes": {"$size": "$clientes"},
                }
            },
        ]
        filas = list(self._sales.aggregate(pipeline, allowDiskUse=True, maxTimeMS=self._max_query_ms))
        if not filas:
            return {
                "lineas": 0,
                "unidades": 0,
                "venta_bruta_cent": 0,
                "descuento_cent": 0,
                "venta_neta_cent": 0,
                "coste_cent": 0,
                "margen_cent": 0,
                "pedidos": 0,
                "clientes": 0,
            }
        return filas[0]

    # --------------------------------------------------------------- resumen
    def summary_bundle(self, match: dict, *, top_products: int = 8, top_customers: int = 40) -> dict:
        """Todo el resumen del dashboard en una sola ida al servidor."""
        pipeline = [
            {"$match": match},
            {
                "$facet": {
                    "totales": [
                        {
                            "$group": {
                                "_id": None,
                                "lineas": {"$sum": 1},
                                "unidades": {"$sum": "$cantidad"},
                                "venta_bruta_cent": {"$sum": "$venta_bruta_cent"},
                                "descuento_cent": {"$sum": "$descuento_cent"},
                                "venta_neta_cent": {"$sum": "$venta_neta_cent"},
                                "coste_cent": {"$sum": "$coste_cent"},
                                "margen_cent": {"$sum": "$margen_cent"},
                                "pedidos": {"$addToSet": "$pedido_id"},
                                "clientes": {"$addToSet": "$cliente_id"},
                            }
                        },
                        {
                            "$project": {
                                "_id": 0,
                                "lineas": 1,
                                "unidades": 1,
                                "venta_bruta_cent": 1,
                                "descuento_cent": 1,
                                "venta_neta_cent": 1,
                                "coste_cent": 1,
                                "margen_cent": 1,
                                "pedidos": {"$size": "$pedidos"},
                                "clientes": {"$size": "$clientes"},
                            }
                        },
                    ],
                    "serie": [
                        {
                            "$group": {
                                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$fecha"}},
                                "lineas": {"$sum": 1},
                                "venta_neta_cent": {"$sum": "$venta_neta_cent"},
                                "margen_cent": {"$sum": "$margen_cent"},
                            }
                        },
                        {"$sort": {"_id": 1}},
                    ],
                    "regiones": self._dimension_stages("region_slug", "region"),
                    "canales": self._dimension_stages("canal_slug", "canal"),
                    "productos": [
                        {
                            "$group": {
                                "_id": {
                                    "v": "$producto_id",
                                    "e": "$producto_nombre",
                                    "c": "$categoria",
                                },
                                "lineas": {"$sum": 1},
                                "unidades": {"$sum": "$cantidad"},
                                "venta_neta_cent": {"$sum": "$venta_neta_cent"},
                                "margen_cent": {"$sum": "$margen_cent"},
                            }
                        },
                        {"$sort": {"venta_neta_cent": -1}},
                        {"$limit": top_products},
                    ],
                    "clientes": [
                        {
                            "$group": {
                                "_id": {"v": "$cliente_id", "e": "$cliente_nombre"},
                                "lineas": {"$sum": 1},
                                "venta_neta_cent": {"$sum": "$venta_neta_cent"},
                                "margen_cent": {"$sum": "$margen_cent"},
                            }
                        },
                        {"$sort": {"venta_neta_cent": -1}},
                        {"$limit": top_customers},
                    ],
                }
            },
        ]
        resultado = list(self._sales.aggregate(pipeline, allowDiskUse=True, maxTimeMS=self._max_query_ms))
        return resultado[0] if resultado else {}

    @staticmethod
    def _dimension_stages(slug_field: str, label_field: str) -> list[dict]:
        return [
            {"$group": {"_id": {"v": f"${slug_field}", "e": f"${label_field}"}, **_group_measures()}},
            {"$sort": {"venta_neta_cent": -1}},
        ]

    # ---------------------------------------------------- opciones de filtro
    def option_rows(self, filters: SalesFilters) -> dict:
        """Opciones por dimensión, calculadas **sin** el filtro de esa dimensión.

        Cada rama del ``$facet`` aplica el resto de filtros: si el usuario ha
        elegido una región, las opciones de canal se calculan dentro de esa
        región, y las de región se calculan con los canales y categorías
        elegidos pero sin restringir la propia región.
        """
        base_match = {"dataset_id": filters.dataset_id, "fecha": filters.mongo_match()["fecha"]}
        facet: dict[str, list[dict]] = {}

        for dimension, (slug_field, label_field) in OPTION_FIELDS.items():
            facet[dimension] = [
                {"$match": filters.mongo_match(exclude=dimension)},
                {
                    "$group": {
                        "_id": {"v": f"${slug_field}", "e": f"${label_field}"},
                        "conteo": {"$sum": 1},
                    }
                },
                # Desempate por etiqueta: sin él, dos opciones con el mismo conteo
                # cambian de orden entre ejecuciones.
                {"$sort": {"conteo": -1, "_id.e": 1}},
                {"$limit": self._max_options},
            ]

        resultado = list(
            self._sales.aggregate(
                [{"$match": base_match}, {"$facet": facet}],
                allowDiskUse=True,
                maxTimeMS=self._max_query_ms,
            )
        )
        return resultado[0] if resultado else {}

    # -------------------------------------------------------------- etiquetas
    def labels_for(
        self, dataset_id: str, *, dimension: str, values: Sequence[str]
    ) -> dict[str, str]:
        """Nombres legibles de unos pocos valores concretos.

        Solo se usa para poder mostrar una selección activa que, con los filtros
        actuales, se ha quedado sin filas (conteo 0). Se consulta únicamente por
        esos valores, no por todo el dataset.
        """
        if not values:
            return {}
        slug_field, label_field = OPTION_FIELDS[dimension]
        cursor = (
            self._sales.find(
                {"dataset_id": dataset_id, slug_field: {"$in": list(values)}},
                projection={"_id": 0, slug_field: 1, label_field: 1},
            )
            .limit(len(values) * 4)
            .max_time_ms(self._max_query_ms)
        )
        etiquetas: dict[str, str] = {}
        for documento in cursor:
            etiquetas.setdefault(documento[slug_field], documento[label_field])
        return etiquetas

    # ---------------------------------------------------------------- detalle
    def count(self, match: dict) -> int:
        return self._sales.count_documents(match, maxTimeMS=self._max_query_ms)

    def page(
        self,
        match: dict,
        *,
        sort_field: str,
        direction: int,
        skip: int,
        limit: int,
    ) -> list[dict]:
        cursor = (
            self._sales.find(match, projection=LINE_PROJECTION)
            .sort([(sort_field, direction), ("linea_id", 1)])
            .skip(max(0, skip))
            .limit(max(1, limit))
            .max_time_ms(self._max_query_ms)
        )
        return list(cursor)

    def iter_lines(
        self,
        match: dict,
        *,
        sort_field: str,
        direction: int,
        limit: int,
    ) -> Iterator[dict]:
        cursor = (
            self._sales.find(match, projection=LINE_PROJECTION)
            .sort([(sort_field, direction), ("linea_id", 1)])
            .limit(limit)
            .max_time_ms(self._max_query_ms)
        )
        yield from cursor

    # --------------------------------------------------------------- metadatos
    def dataset_metadata(self, dataset_id: str) -> dict | None:
        return self._datasets.find_one({"dataset_id": dataset_id}, projection={"_id": 0})

    def dataset_bounds(self, dataset_id: str) -> tuple[Any, Any] | None:
        """Primera y última fecha del dataset, usando el índice compuesto."""
        primero = self._sales.find_one(
            {"dataset_id": dataset_id}, sort=[("fecha", 1)], projection={"fecha": 1}
        )
        if not primero:
            return None
        ultimo = self._sales.find_one(
            {"dataset_id": dataset_id}, sort=[("fecha", -1)], projection={"fecha": 1}
        )
        return primero["fecha"], ultimo["fecha"]

    # ---------------------------------------------------------------- explique
    def explain_sample(self, match: dict) -> dict:
        """Plan de ejecución de la consulta representativa (diagnóstico)."""
        explain = self._sales.find(match, projection={"linea_id": 1}).sort([("fecha", -1)]).explain()
        return explain
