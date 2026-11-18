"""Consultas de MongoDB del Atlas vial.

Todas las agregaciones parten de la **versión publicada** y del mismo documento
de filtros, de modo que el mapa, las gráficas, la tabla y la exportación no
pueden discrepar. Nunca se hace ``$lookup`` entre recursos: los cinco archivos
tienen una fila por folio y el importador ya los cruza 1:1, así que no hay forma
de multiplicar hechos ni víctimas.

Regla de los cálculos: los conteos de personas se hacen **sumando los campos del
archivo de hechos** (``personas_lesionadas``, ``personas_fallecidas``), nunca
multiplicando por los desgloses de personas.
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo.database import Database

from app.atlas import rejilla
from app.db.mongo import ATLAS_HECHOS, ATLAS_IMPORTACIONES, reintentar_lectura
from app.models.atlas import AtlasFiltros

logger = logging.getLogger(__name__)

#: Proyección de la tabla y de la exportación (sin datos personales innecesarios).
CAMPOS_TABLA = {
    "_id": 0,
    "folio": 1,
    "fecha": 1,
    "hora": 1,
    "alcaldia": 1,
    "tipo_evento_norm": 1,
    "colonia": 1,
    "punto_1": 1,
    "punto_2": 1,
    "personas_lesionadas": 1,
    "personas_fallecidas": 1,
    "lat": 1,
    "lon": 1,
    "coord_estado": 1,
}

#: Campos ordenables de la tabla (lista blanca).
ORDEN_TABLA = {
    "fecha": "fecha",
    "hora": "hora",
    "alcaldia": "alcaldia",
    "tipo_evento": "tipo_evento_norm",
    "lesionadas": "personas_lesionadas",
    "fallecidas": "personas_fallecidas",
    "folio": "folio",
}


class AtlasRepository:
    def __init__(self, db: Database, max_query_ms: int = 8_000) -> None:
        self._db = db
        self._coleccion = db[ATLAS_HECHOS]
        self._max_ms = max_query_ms

    # ------------------------------------------------------------------ base
    def version_publicada(self) -> str | None:
        estado = self._db["atlas_estado"].find_one({"_id": "activo"})
        return (estado or {}).get("version")

    def importacion(self, version: str) -> dict[str, Any] | None:
        return self._db[ATLAS_IMPORTACIONES].find_one({"version": version}, {"_id": 0})

    def ultima_importacion(self) -> dict[str, Any] | None:
        return self._db[ATLAS_IMPORTACIONES].find_one({}, {"_id": 0}, sort=[("creado_en", -1)])

    def condiciones(self, version: str, filtros: AtlasFiltros, incluir_zona: bool = True) -> dict[str, Any]:
        base = {"version": version}
        base.update(filtros.condiciones(incluir_zona=incluir_zona))
        return base

    # ------------------------------------------------------------- indicadores
    def resumen(self, version: str, filtros: AtlasFiltros) -> dict[str, Any]:
        consulta = self.condiciones(version, filtros)
        cursor = self._coleccion.aggregate(
            [
                {"$match": consulta},
                {
                    "$group": {
                        "_id": None,
                        "hechos": {"$sum": 1},
                        "lesionadas": {"$sum": "$personas_lesionadas"},
                        "fallecidas": {"$sum": "$personas_fallecidas"},
                        "con_fallecimientos": {
                            "$sum": {"$cond": [{"$gt": ["$personas_fallecidas", 0]}, 1, 0]}
                        },
                        "con_lesionados": {
                            "$sum": {"$cond": [{"$gt": ["$personas_lesionadas", 0]}, 1, 0]}
                        },
                        "sin_victimas": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$lte": ["$personas_lesionadas", 0]},
                                            {"$lte": ["$personas_fallecidas", 0]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                        "con_coordenadas": {
                            "$sum": {"$cond": [{"$eq": ["$coord_estado", "ok"]}, 1, 0]}
                        },
                        "fuera_de_cdmx": {
                            "$sum": {"$cond": [{"$eq": ["$coord_estado", "fuera_de_cdmx"]}, 1, 0]}
                        },
                        "sin_coordenadas": {
                            "$sum": {
                                "$cond": [
                                    {"$in": ["$coord_estado", ["ausente", "no_numerica", "fuera_de_rango"]]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "con_sexo": {
                            "$sum": {
                                "$cond": [
                                    {"$and": [{"$ne": ["$personas_sexo", None]}, {"$ne": ["$personas_sexo", "Sin dato"]}]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "con_edad": {
                            "$sum": {"$cond": [{"$ne": ["$personas_edad", None]}, 1, 0]}
                        },
                        "con_tipo_usuario": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$ne": ["$personas_tipo_usuario", None]},
                                            {"$ne": ["$personas_tipo_usuario", "Sin dato"]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                        "con_vehiculo": {
                            "$sum": {"$cond": [{"$ne": ["$vehiculo_tipo", None]}, 1, 0]}
                        },
                        "alcaldias": {"$addToSet": "$alcaldia"},
                        "primera_fecha": {"$min": "$fecha"},
                        "ultima_fecha": {"$max": "$fecha"},
                        "sin_fecha": {"$sum": {"$cond": [{"$eq": ["$fecha", None]}, 1, 0]}},
                        "sin_hora": {"$sum": {"$cond": [{"$eq": ["$hora", None]}, 1, 0]}},
                    }
                },
            ],
            maxTimeMS=self._max_ms,
        )
        datos = next(cursor, None) or {}
        datos.pop("_id", None)
        datos["alcaldias"] = sorted(nombre for nombre in datos.get("alcaldias", []) if nombre)
        return datos

    # ---------------------------------------------------------------- territorio
    def por_alcaldia(self, version: str, filtros: AtlasFiltros) -> list[dict[str, Any]]:
        cursor = self._coleccion.aggregate(
            [
                {"$match": self.condiciones(version, filtros)},
                {
                    "$group": {
                        "_id": "$alcaldia",
                        "hechos": {"$sum": 1},
                        "lesionadas": {"$sum": "$personas_lesionadas"},
                        "fallecidas": {"$sum": "$personas_fallecidas"},
                        "con_fallecimientos": {
                            "$sum": {"$cond": [{"$gt": ["$personas_fallecidas", 0]}, 1, 0]}
                        },
                        "con_coordenadas": {
                            "$sum": {"$cond": [{"$eq": ["$coord_estado", "ok"]}, 1, 0]}
                        },
                    }
                },
                {"$sort": {"hechos": -1}},
            ],
            maxTimeMS=self._max_ms,
        )
        return [
            {
                "alcaldia": documento["_id"],
                "hechos": documento["hechos"],
                "lesionadas": documento["lesionadas"],
                "fallecidas": documento["fallecidas"],
                "con_fallecimientos": documento["con_fallecimientos"],
                "con_coordenadas": documento["con_coordenadas"],
            }
            for documento in cursor
        ]

    def puntos(self, version: str, filtros: AtlasFiltros, *, celda: float | None, limite: int) -> dict[str, Any]:
        """Puntos del mapa: agrupados por celda (concentración) o individuales.

        Se agrupa en el servidor para no enviar decenas de miles de nodos al
        navegador: con ``celda`` el resultado son cajas agregadas con su conteo.
        """
        consulta = self.condiciones(version, filtros)
        consulta["coord_estado"] = "ok"
        if celda and celda > 0:
            cursor = self._coleccion.aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": {
                                "lon": {"$multiply": [{"$floor": {"$divide": ["$lon", celda]}}, celda]},
                                "lat": {"$multiply": [{"$floor": {"$divide": ["$lat", celda]}}, celda]},
                            },
                            "hechos": {"$sum": 1},
                            "lesionadas": {"$sum": "$personas_lesionadas"},
                            "fallecidas": {"$sum": "$personas_fallecidas"},
                        }
                    },
                    {"$sort": {"hechos": -1}},
                    {"$limit": limite},
                ],
                maxTimeMS=self._max_ms,
            )
            cajas = [
                {
                    "lon": documento["_id"]["lon"],
                    "lat": documento["_id"]["lat"],
                    "hechos": documento["hechos"],
                    "lesionadas": documento["lesionadas"],
                    "fallecidas": documento["fallecidas"],
                }
                for documento in cursor
            ]
            return {"tipo": "celdas", "celda": celda, "cajas": cajas, "total": sum(caja["hechos"] for caja in cajas)}

        cursor = (
            self._coleccion.find(
                consulta,
                {
                    "_id": 0,
                    "folio": 1,
                    "lat": 1,
                    "lon": 1,
                    "fecha": 1,
                    "hora": 1,
                    "tipo_evento_norm": 1,
                    "alcaldia": 1,
                    "personas_lesionadas": 1,
                    "personas_fallecidas": 1,
                },
            )
            .sort([("fecha", 1), ("hora", 1)])
            .limit(limite)
        )
        puntos = [
            {
                "folio": documento.get("folio"),
                "lat": documento.get("lat"),
                "lon": documento.get("lon"),
                "fecha": documento.get("fecha"),
                "hora": documento.get("hora"),
                "tipo_evento": documento.get("tipo_evento_norm"),
                "alcaldia": documento.get("alcaldia"),
                "lesionadas": documento.get("personas_lesionadas", 0),
                "fallecidas": documento.get("personas_fallecidas", 0),
            }
            for documento in cursor
        ]
        total = self._coleccion.count_documents(consulta, maxTimeMS=self._max_ms)
        return {"tipo": "puntos", "puntos": puntos, "total": total, "limite": limite}

    def puntos_completos(self, version: str, filtros: AtlasFiltros, limite: int) -> dict[str, Any]:
        """Todos los puntos geolocalizados del conjunto filtrado, en arrays.

        Se envían como listas paralelas de primitivos (y no como objetos) porque
        el mapa los necesita todos para agrupar por zoom en el navegador: así el
        navegador no pide datos nuevos al mover el mapa ni al cambiar el zoom.
        El folio va incluido: es la clave que abre el detalle de cada hecho.
        """
        consulta = self.condiciones(version, filtros)
        consulta["coord_estado"] = "ok"

        def leer() -> tuple[list[str], list[float], list[float], list[int], list[int], int]:
            cursor = (
                self._coleccion.find(
                    consulta,
                    {
                        "_id": 0,
                        "folio": 1,
                        "lat": 1,
                        "lon": 1,
                        "personas_lesionadas": 1,
                        "personas_fallecidas": 1,
                    },
                )
                # El clúster gratuito limita la memoria de ordenación a 32 MB: con
                # 30 655 puntos hay que permitir el uso de disco en el servidor.
                .sort([("fecha", 1), ("hora", 1), ("folio", 1)])
                .limit(limite)
                .allow_disk_use(True)
            )
            folios: list[str] = []
            lats: list[float] = []
            lons: list[float] = []
            lesionadas: list[int] = []
            fallecidas: list[int] = []
            for documento in cursor:
                folios.append(documento.get("folio") or "")
                lats.append(round(documento.get("lat") or 0.0, 5))
                lons.append(round(documento.get("lon") or 0.0, 5))
                lesionadas.append(documento.get("personas_lesionadas") or 0)
                fallecidas.append(documento.get("personas_fallecidas") or 0)
            total = self._coleccion.count_documents(consulta, maxTimeMS=self._max_ms)
            return folios, lats, lons, lesionadas, fallecidas, total

        # Es la lectura más larga de la API (30 655 documentos, ~1,2 MB): si Atlas
        # cierra la conexión a mitad del cursor, se repite en lugar de devolver 500.
        folios, lats, lons, lesionadas, fallecidas, total = reintentar_lectura(leer)
        return {
            "folios": folios,
            "lat": lats,
            "lon": lons,
            "lesionadas": lesionadas,
            "fallecidas": fallecidas,
            "total": total,
            "mostrados": len(folios),
            "truncado": total > len(folios),
        }

    # ----------------------------------------------------------- colonias
    def colonias(self, version: str, filtros: AtlasFiltros) -> dict[str, Any]:
        """Conteos por colonia declarada en la fuente, con cobertura.

        Se agrupa por `(alcaldía, colonia)` y no solo por nombre: hay nombres
        repetidos en alcaldías distintas y fusionarlos inventaría una colonia.
        No se usan polígonos ni proximidad: la colonia es la **declarada** en el
        archivo de hechos, y los registros sin colonia se informan aparte.
        """
        consulta = self.condiciones(version, filtros)
        acumulado = {
            "_id": {"alcaldia": "$alcaldia", "colonia": "$colonia"},
            "hechos": {"$sum": 1},
            "lesionadas": {"$sum": {"$ifNull": ["$personas_lesionadas", 0]}},
            "fallecidas": {"$sum": {"$ifNull": ["$personas_fallecidas", 0]}},
        }
        filas: list[dict[str, Any]] = []
        for documento in self._coleccion.aggregate(
            [{"$match": consulta}, {"$group": acumulado}, {"$sort": {"hechos": -1, "_id.alcaldia": 1, "_id.colonia": 1}}],
            maxTimeMS=self._max_ms,
            allowDiskUse=True,
        ):
            colonia = (documento["_id"].get("colonia") or "").strip()
            if not colonia:
                continue
            filas.append(
                {
                    "alcaldia": (documento["_id"].get("alcaldia") or "").strip() or None,
                    "colonia": colonia,
                    "hechos": documento["hechos"],
                    "lesionadas": documento["lesionadas"],
                    "fallecidas": documento["fallecidas"],
                }
            )

        total, total_lesionadas, total_fallecidas = 0, 0, 0
        sin_colonia, sin_colonia_lesionadas, sin_colonia_fallecidas = 0, 0, 0
        for documento in self._coleccion.aggregate(
            [
                {"$match": consulta},
                {
                    "$group": {
                        "_id": {
                            "$cond": [
                                {
                                    "$gt": [
                                        {"$strLenCP": {"$trim": {"input": {"$ifNull": ["$colonia", ""]}}}},
                                        0,
                                    ]
                                },
                                "con_colonia",
                                "sin_colonia",
                            ]
                        },
                        "hechos": {"$sum": 1},
                        "lesionadas": {"$sum": {"$ifNull": ["$personas_lesionadas", 0]}},
                        "fallecidas": {"$sum": {"$ifNull": ["$personas_fallecidas", 0]}},
                    }
                },
            ],
            maxTimeMS=self._max_ms,
        ):
            if documento["_id"] == "sin_colonia":
                sin_colonia = documento["hechos"]
                sin_colonia_lesionadas = documento["lesionadas"]
                sin_colonia_fallecidas = documento["fallecidas"]
            total += documento["hechos"]
            total_lesionadas += documento["lesionadas"]
            total_fallecidas += documento["fallecidas"]

        asignados = sum(fila["hechos"] for fila in filas)
        lesionadas_asignadas = sum(fila["lesionadas"] for fila in filas)
        fallecidas_asignadas = sum(fila["fallecidas"] for fila in filas)
        filas.sort(key=lambda fila: (-fila["hechos"], fila["alcaldia"] or "", fila["colonia"]))
        return {
            "colonias": filas,
            "total": total,
            "total_lesionadas": total_lesionadas,
            "total_fallecidas": total_fallecidas,
            "cobertura": {
                "asignados": asignados,
                "lesionadas_asignadas": lesionadas_asignadas,
                "fallecidas_asignadas": fallecidas_asignadas,
                "sin_colonia": sin_colonia,
                "lesionadas_sin_colonia": sin_colonia_lesionadas,
                "fallecidas_sin_colonia": sin_colonia_fallecidas,
                "fuente": "colonia declarada en el archivo de hechos",
                "poligonos": False,
            },
        }

    # ------------------------------------------------------------- celdas
    def celdas(self, version: str, filtros: AtlasFiltros) -> dict[str, Any]:
        """Conteo de hechos por celda de la malla métrica estable.

        La retícula se calcula en el servidor con `lat`/`lon` (MongoDB no
        proyecta a UTM) y solo se envían al navegador los conteos por celda y la
        geometría de las diez mayores: nunca se descargan documentos completos.
        Los hechos sin coordenada utilizable se informan por categorías
        mutuamente excluyentes para poder reconciliar con el total.
        """
        consulta = self.condiciones(version, filtros)

        cobertura = {
            documento["_id"]: documento["n"]
            for documento in self._coleccion.aggregate(
                [
                    {"$match": consulta},
                    {"$group": {"_id": "$coord_estado", "n": {"$sum": 1}}},
                ],
                maxTimeMS=self._max_ms,
            )
        }
        total = sum(cobertura.values())
        ubicables = cobertura.get("ok", 0)

        conteos: dict[tuple[int, int], int] = {}
        cursor = self._coleccion.find(
            {**consulta, "coord_estado": "ok"},
            {"_id": 0, "lat": 1, "lon": 1},
        )
        for documento in cursor:
            try:
                ix, iy = rejilla.celda_de(float(documento["lat"]), float(documento["lon"]))
            except (KeyError, TypeError, ValueError):
                continue
            conteos[(ix, iy)] = conteos.get((ix, iy), 0) + 1

        incluidas = sum(conteos.values())
        ordenadas = sorted(conteos.items(), key=lambda par: (-par[1], par[0][0], par[0][1]))
        mayores = [
            {
                "id": rejilla.identificador(ix, iy),
                "ix": ix,
                "iy": iy,
                "hechos": hechos,
                "anillo": rejilla.geometria_celda(ix, iy),
            }
            for (ix, iy), hechos in ordenadas[:10]
        ]

        return {
            "malla": rejilla.describir(),
            "total": total,
            "ubicables": ubicables,
            "incluidas": incluidas,
            "excluidos": {
                "ausente": cobertura.get("ausente", 0),
                "no_numerica": cobertura.get("no_numerica", 0),
                "fuera_de_rango": cobertura.get("fuera_de_rango", 0),
                "fuera_de_cdmx": cobertura.get("fuera_de_cdmx", 0),
            },
            "celdas_con_registros": len(conteos),
            "celdas": [[ix, iy, hechos] for (ix, iy), hechos in ordenadas],
            "mayores": mayores,
            "sin_asignacion_por_proximidad": True,
        }

    # ------------------------------------------------------------------ tiempo
    def por_periodo(self, version: str, filtros: AtlasFiltros, formato: str) -> list[dict[str, Any]]:
        """Serie temporal con la agrupación pedida (día, semana o mes).

        El formato de la fecha se resuelve en Mongo con ``$dateToString`` sobre
        la cadena ISO ``fecha`` (AAAA-MM-DD), así que no depende de zonas
        horarias y no hay que convertir nada en el navegador.
        """
        if formato == "mes":
            expresion = {"$substrBytes": ["$fecha", 0, 7]}
        elif formato == "semana":
            expresion = {
                "$concat": [
                    {"$toString": {"$isoWeekYear": {"$dateFromString": {"dateString": "$fecha"}}}},
                    "-S",
                    {
                        "$cond": [
                            {"$lt": [{"$isoWeek": {"$dateFromString": {"dateString": "$fecha"}}}, 10]},
                            {"$concat": ["0", {"$toString": {"$isoWeek": {"$dateFromString": {"dateString": "$fecha"}}}}]},
                            {"$toString": {"$isoWeek": {"$dateFromString": {"dateString": "$fecha"}}}},
                        ]
                    },
                ]
            }
        else:
            expresion = "$fecha"

        cursor = self._coleccion.aggregate(
            [
                {"$match": self.condiciones(version, filtros)},
                {
                    "$group": {
                        "_id": expresion,
                        "hechos": {"$sum": 1},
                        "lesionadas": {"$sum": "$personas_lesionadas"},
                        "fallecidas": {"$sum": "$personas_fallecidas"},
                        "con_fallecimientos": {
                            "$sum": {"$cond": [{"$gt": ["$personas_fallecidas", 0]}, 1, 0]}
                        },
                    }
                },
                {"$sort": {"_id": 1}},
            ],
            maxTimeMS=self._max_ms,
        )
        return [
            {
                "periodo": documento["_id"],
                "hechos": documento["hechos"],
                "lesionadas": documento["lesionadas"],
                "fallecidas": documento["fallecidas"],
                "con_fallecimientos": documento["con_fallecimientos"],
            }
            for documento in cursor
        ]

    def matriz_dia_hora(self, version: str, filtros: AtlasFiltros) -> list[dict[str, Any]]:
        cursor = self._coleccion.aggregate(
            [
                {"$match": {**self.condiciones(version, filtros), "hora": {"$ne": None}, "dia_semana": {"$ne": None}}},
                {
                    "$group": {
                        "_id": {"dia": "$dia_semana", "hora": "$hora"},
                        "hechos": {"$sum": 1},
                        "lesionadas": {"$sum": "$personas_lesionadas"},
                        "fallecidas": {"$sum": "$personas_fallecidas"},
                    }
                },
                {"$sort": {"_id.dia": 1, "_id.hora": 1}},
            ],
            maxTimeMS=self._max_ms,
        )
        return [
            {
                "dia_semana": documento["_id"]["dia"],
                "hora": documento["_id"]["hora"],
                "hechos": documento["hechos"],
                "lesionadas": documento["lesionadas"],
                "fallecidas": documento["fallecidas"],
            }
            for documento in cursor
        ]

    # ------------------------------------------------------------ distribuciones
    def distribucion(self, version: str, filtros: AtlasFiltros, campo: str) -> list[dict[str, Any]]:
        cursor = self._coleccion.aggregate(
            [
                {"$match": self.condiciones(version, filtros)},
                {
                    "$group": {
                        "_id": f"${campo}",
                        "hechos": {"$sum": 1},
                        "lesionadas": {"$sum": "$personas_lesionadas"},
                        "fallecidas": {"$sum": "$personas_fallecidas"},
                    }
                },
                {"$sort": {"hechos": -1}},
            ],
            maxTimeMS=self._max_ms,
        )
        return [
            {
                "valor": documento["_id"],
                "hechos": documento["hechos"],
                "lesionadas": documento["lesionadas"],
                "fallecidas": documento["fallecidas"],
            }
            for documento in cursor
        ]

    def cruce(
        self,
        version: str,
        filtros: AtlasFiltros,
        campo_fila: str,
        campo_columna: str,
    ) -> list[dict[str, Any]]:
        """Distribución conjunta de dos campos del **mismo** documento de hecho.

        No hay uniones que puedan duplicar registros: tipo de evento, clasificación
        vial, tipo de intersección y semáforo viven en el documento del hecho, así
        que cada hecho cuenta exactamente una vez. Los valores no publicados
        (`None`) se devuelven tal cual para distinguirlos del cero.
        """
        cursor = self._coleccion.aggregate(
            [
                {"$match": self.condiciones(version, filtros)},
                {
                    "$group": {
                        "_id": {"fila": f"${campo_fila}", "columna": f"${campo_columna}"},
                        "hechos": {"$sum": 1},
                        "lesionadas": {"$sum": "$personas_lesionadas"},
                        "fallecidas": {"$sum": "$personas_fallecidas"},
                    }
                },
                {"$sort": {"hechos": -1}},
            ],
            maxTimeMS=self._max_ms,
        )
        return [
            {
                "fila": documento["_id"]["fila"],
                "columna": documento["_id"]["columna"],
                "hechos": documento["hechos"],
                "lesionadas": documento["lesionadas"],
                "fallecidas": documento["fallecidas"],
            }
            for documento in cursor
        ]

    def edades(self, version: str, filtros: AtlasFiltros) -> list[dict[str, Any]]:
        cursor = self._coleccion.aggregate(
            [
                {"$match": self.condiciones(version, filtros)},
                {
                    "$group": {
                        "_id": "$personas_edad",
                        "hechos": {"$sum": 1},
                        "lesionadas": {"$sum": "$personas_lesionadas"},
                        "fallecidas": {"$sum": "$personas_fallecidas"},
                    }
                },
                {"$sort": {"_id": 1}},
            ],
            maxTimeMS=self._max_ms,
        )
        return [
            {
                "edad": documento["_id"],
                "hechos": documento["hechos"],
                "lesionadas": documento["lesionadas"],
                "fallecidas": documento["fallecidas"],
            }
            for documento in cursor
        ]

    # ---------------------------------------------------------------- tabla
    def tabla(
        self,
        version: str,
        filtros: AtlasFiltros,
        *,
        orden: str,
        descendente: bool,
        pagina: int,
        tamano: int,
    ) -> dict[str, Any]:
        consulta = self.condiciones(version, filtros)
        campo = ORDEN_TABLA.get(orden, "fecha")
        cursor = (
            self._coleccion.find(consulta, CAMPOS_TABLA)
            .sort([(campo, -1 if descendente else 1), ("folio", 1)])
            .skip((pagina - 1) * tamano)
            .limit(tamano)
        )
        total = self._coleccion.count_documents(consulta, maxTimeMS=self._max_ms)
        return {"filas": [_fila(documento) for documento in cursor], "total": total}

    def filas_exportar(self, version: str, filtros: AtlasFiltros, limite: int):
        consulta = self.condiciones(version, filtros)
        cursor = (
            self._coleccion.find(consulta, CAMPOS_TABLA)
            .sort([("fecha", 1), ("hora", 1), ("folio", 1)])
            .limit(limite)
        )
        for documento in cursor:
            yield _fila(documento)

    def evento(self, version: str, folio: str) -> dict[str, Any] | None:
        documento = self._coleccion.find_one(
            {"version": version, "folio": folio},
            {"_id": 0},
        )
        return documento

    # --------------------------------------------------------------- catálogo
    def valores_filtro(self, version: str) -> dict[str, list[str]]:
        """Dominios reales de los filtros (solo lo que existe en la versión)."""
        consulta = {"version": version}

        def distintos(campo: str) -> list[str]:
            valores = self._coleccion.distinct(campo, consulta, maxTimeMS=self._max_ms)
            return sorted(valor for valor in valores if valor not in (None, "", "Sin dato"))

        return {
            "alcaldias": distintos("alcaldia"),
            "tipos_evento": distintos("tipo_evento_norm"),
            "vialidades": distintos("vialidad_norm"),
            "tipos_usuario": distintos("personas_tipo_usuario"),
            "tipos_vehiculo": distintos("vehiculo_tipo"),
            "sexos": distintos("personas_sexo"),
            "intersecciones": distintos("tipo_interseccion_norm"),
        }


def _fila(documento: dict[str, Any]) -> dict[str, Any]:
    """Fila de tabla/exportación: solo lo necesario para el análisis."""
    lesionadas = documento.get("personas_lesionadas", 0) or 0
    fallecidas = documento.get("personas_fallecidas", 0) or 0
    if documento.get("con_fallecimientos") is None:
        consecuencia = "con_fallecimientos" if fallecidas > 0 else "solo_lesionados"
    else:
        consecuencia = (
            "con_fallecimientos" if documento["con_fallecimientos"] > 0 else "solo_lesionados"
        )
    return {
        "folio": documento.get("folio"),
        "fecha": documento.get("fecha"),
        "hora": documento.get("hora"),
        "alcaldia": documento.get("alcaldia"),
        "tipo_evento": documento.get("tipo_evento_norm"),
        "ubicacion": _ubicacion(documento),
        "lesionadas": lesionadas,
        "fallecidas": fallecidas,
        "consecuencia": consecuencia,
        "lat": documento.get("lat"),
        "lon": documento.get("lon"),
        "coord_estado": documento.get("coord_estado"),
    }


def _ubicacion(documento: dict[str, Any]) -> str:
    partes = [documento.get("punto_1"), documento.get("punto_2"), documento.get("colonia")]
    texto = " · ".join(parte for parte in partes if parte)
    return texto or "Sin ubicación en el expediente"

