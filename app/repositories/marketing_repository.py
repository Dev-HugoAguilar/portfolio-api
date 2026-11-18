"""Consultas de MongoDB del tablero «Marketing y clientes».

Todas las agregaciones parten de la **versión publicada** y del mismo documento de
filtros, así que las tarjetas, la serie, la adquisición, el embudo, las cohortes,
la tabla y la exportación no pueden discrepar.

Regla clave: **los usuarios no se suman por día**. Se cuentan con
``distinct("usuario")`` sobre la colección de usuarios (guardados como hash con
sal), que es la única forma correcta de contar personas distintas en un rango.
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo.database import Database

from app.db.mongo import (
    MARKETING_AFINIDAD,
    MARKETING_ARTICULOS,
    MARKETING_FLUJO,
    MARKETING_TIEMPOS,
    MARKETING_CATEGORIAS,
    MARKETING_COHORTES,
    MARKETING_COMPRAS,
    MARKETING_EMBUDO,
    MARKETING_EVENTOS,
    MARKETING_IMPORTACIONES,
    MARKETING_RESUMEN,
    MARKETING_SESIONES,
    MARKETING_USUARIOS,
)
from app.models.marketing import ORDEN_TABLA, MarketingFiltros

logger = logging.getLogger(__name__)

#: Proyección de la tabla y de la exportación: sin identificadores de persona.
CAMPOS_TABLA = {
    "_id": 0,
    "id": 1,
    "fecha": 1,
    "hora": 1,
    "evento": 1,
    "evento_etiqueta": 1,
    "dispositivo": 1,
    "pais": 1,
    "canal": 1,
    "fuente": 1,
    "medio": 1,
    "articulo": 1,
    "categoria": 1,
    "ingresos_usd": 1,
}

#: La afinidad se calcula una vez por compra sobre **todo** el periodo publicado:
#: sus pares no se pueden recalcular por día ni por canal sin volver a guardar las
#: líneas de artículo, que el modelo compacto no conserva. La respuesta lo declara
#: para que el tablero no dé a entender que responde a los filtros.
ALCANCE_AFINIDAD = {
    "aplica_filtros": False,
    "descripcion": (
        "Compras conjuntas de todas las compras del periodo publicado: no cambia con "
        "los filtros de fecha, canal, dispositivo ni país."
    ),
}


def _alcance_afinidad(
    importacion: dict[str, Any] | None, aplica_filtros: bool = False
) -> dict[str, Any]:
    """Alcance de la afinidad con el periodo que cubre y su cobertura.

    En el modelo compacto ``aplica_filtros`` es ``False``: los pares se publican
    calculados una vez. En una versión con detalle de artículos sí se puede
    recalcular por filtros, y entonces se declara lo contrario.
    """
    periodo = (importacion or {}).get("periodo") or {}
    return {
        **ALCANCE_AFINIDAD,
        "aplica_filtros": aplica_filtros,
        "descripcion": (
            ALCANCE_AFINIDAD["descripcion"]
            if not aplica_filtros
            else "Compras conjuntas del rango filtrado: se recalculan desde el "
            "detalle de artículos de esta versión."
        ),
        "periodo": {"desde": periodo.get("desde"), "hasta": periodo.get("hasta")},
        "responde_a_filtros": aplica_filtros,
        "por_que": (
            "Los pares se calculan por compra sobre las líneas de artículo; esas "
            "líneas ya no se guardan, así que la afinidad se publica calculada una "
            "vez para todo el periodo."
            if not aplica_filtros
            else "Esta versión conserva las líneas de artículo, así que la afinidad "
            "se calcula con los mismos filtros que el resto de la vista."
        ),
    }


class MarketingRepository:
    def __init__(self, db: Database, max_query_ms: int = 8_000) -> None:
        self._db = db
        self._max_ms = max_query_ms

    # ------------------------------------------------------------------ base
    def version_publicada(self) -> str | None:
        estado = self._db["marketing_estado"].find_one({"_id": "activo"})
        return (estado or {}).get("version")

    def importacion(self, version: str) -> dict[str, Any] | None:
        return self._db[MARKETING_IMPORTACIONES].find_one({"version": version}, {"_id": 0})

    def condiciones(self, version: str, filtros: MarketingFiltros) -> dict[str, Any]:
        consulta = {"version": version}
        consulta.update(filtros.condiciones())
        return consulta

    # --------------------------------------------------------------- catálogo
    def valores_filtro(self, version: str) -> dict[str, Any]:
        consulta = {"version": version}

        def distintos(coleccion: str, campo: str) -> list[str]:
            valores = self._db[coleccion].distinct(campo, consulta, maxTimeMS=self._max_ms)
            return sorted(valor for valor in valores if valor)

        fechas = self._db[MARKETING_RESUMEN].distinct("fecha", consulta, maxTimeMS=self._max_ms)
        fechas = sorted(fecha for fecha in fechas if fecha)
        return {
            "canales": distintos(MARKETING_RESUMEN, "canal"),
            "dispositivos": distintos(MARKETING_RESUMEN, "dispositivo"),
            "paises": distintos(MARKETING_RESUMEN, "pais"),
            "periodo": {"desde": fechas[0] if fechas else None, "hasta": fechas[-1] if fechas else None},
            "dias_observados": len(fechas),
        }

    # ------------------------------------------------------------- indicadores
    def metricas(self, version: str, filtros: MarketingFiltros) -> dict[str, Any]:
        """Agregado del rango: eventos y sesiones de la actividad; compras de su colección.

        Las compras, los compradores y los ingresos salen de la colección de compras
        **ya deduplicadas sobre todo el periodo**: sumar los agregados diarios
        contaría dos veces la misma transacción y a quien compra en varios días.
        """
        consulta = self.condiciones(version, filtros)
        cursor = self._db[MARKETING_RESUMEN].aggregate(
            [
                {"$match": consulta},
                {
                    "$group": {
                        "_id": None,
                        "eventos": {"$sum": "$eventos"},
                        "sesiones": {"$sum": "$sesiones"},
                        "eventos_sin_sesion": {"$sum": "$eventos_sin_sesion"},
                        "filas": {"$sum": 1},
                    }
                },
            ],
            maxTimeMS=self._max_ms,
        )
        datos = next(cursor, None) or {}
        datos.pop("_id", None)
        for clave in ("eventos", "sesiones", "eventos_sin_sesion", "filas"):
            datos.setdefault(clave, 0)

        compras = next(
            iter(
                self._db[MARKETING_COMPRAS].aggregate(
                    [
                        {"$match": consulta},
                        {
                            "$group": {
                                "_id": None,
                                "compras": {"$sum": 1},
                                "ingresos_usd": {"$sum": "$ingresos_usd"},
                                "compras_sin_importe": {
                                    "$sum": {"$cond": [{"$lte": ["$ingresos_usd", 0]}, 1, 0]}
                                },
                            }
                        },
                    ],
                    maxTimeMS=self._max_ms,
                )
            ),
            {},
        )
        datos["compras"] = int(compras.get("compras") or 0)
        datos["ingresos_usd"] = round(float(compras.get("ingresos_usd") or 0), 2)
        datos["compras_sin_importe"] = int(compras.get("compras_sin_importe") or 0)
        return datos

    def usuarios_distintos(self, version: str, filtros: MarketingFiltros, solo_compradores: bool = False) -> int:
        """Usuarios distintos contados en el motor, sin traer la lista a Python.

        El índice ``(version, <dimensión>, usuario)`` permite resolverlo con un
        recorrido de índice, así que el coste no crece con el tamaño del documento.
        """
        if solo_compradores:
            return self.facetas_usuarios(version, filtros)["compradores"]
        return self.facetas_usuarios(version, filtros)["usuarios"]

    def facetas_usuarios(self, version: str, filtros: MarketingFiltros) -> dict[str, Any]:
        """Una sola pasada para todos los cortes de usuarios.

        Agrupa por día, canal y usuario y decide en Python: usuarios distintos,
        compradores distintos, y los cortes por canal y por día. Reemplaza cuatro
        agregaciones separadas (una por lectura) por una sola.
        """
        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_USUARIOS].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": {
                                "fecha": "$fecha",
                                "canal": "$canal",
                                "usuario": "$usuario",
                            },
                            "compro": {"$max": "$compro"},
                        }
                    },
                ],
                maxTimeMS=max(self._max_ms, 20_000),
                batchSize=50_000,
                allowDiskUse=True,
            )
        )
        usuarios: set[str] = set()
        compradores: set[str] = set()
        por_canal: dict[str, dict[str, Any]] = {}
        por_dia: dict[str, dict[str, Any]] = {}
        for fila in filas:
            usuario = fila["_id"].get("usuario")
            canal = fila["_id"].get("canal") or "Sin información"
            fecha = fila["_id"].get("fecha")
            usuarios.add(usuario)
            compro = bool(fila.get("compro"))
            if compro:
                compradores.add(usuario)
            # Se guardan conjuntos, no contadores: el canal y el día tienen filas por
            # usuario y día, y sumarlas contaría dos veces a quien vuelve.
            canal_datos = por_canal.setdefault(canal, {"usuarios": set(), "compradores": set()})
            canal_datos["usuarios"].add(usuario)
            if compro:
                canal_datos["compradores"].add(usuario)
            if fecha:
                dia = por_dia.setdefault(fecha, {"usuarios": set(), "compradores": set()})
                dia["usuarios"].add(usuario)
                if compro:
                    dia["compradores"].add(usuario)
        return {
            "usuarios": len(usuarios),
            "compradores": len(compradores),
            "por_canal": {
                canal: {
                    "usuarios": len(datos["usuarios"]),
                    "compradores": len(datos["compradores"]),
                }
                for canal, datos in por_canal.items()
            },
            "por_dia": {
                fecha: {
                    "usuarios": len(datos["usuarios"]),
                    "compradores": len(datos["compradores"]),
                }
                for fecha, datos in por_dia.items()
            },
        }

    def serie(
        self,
        version: str,
        filtros: MarketingFiltros,
        facetas: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Serie diaria: eventos, sesiones, compras e ingresos por día.

        Los usuarios del día se cuentan con el corte por día de ``facetas_usuarios``
        (una sola pasada compartida) y las compras vienen de su colección
        deduplicada (una compra, un día).
        """
        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_RESUMEN].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": "$fecha",
                            "eventos": {"$sum": "$eventos"},
                            "sesiones": {"$sum": "$sesiones"},
                        }
                    },
                    {"$sort": {"_id": 1}},
                ],
                maxTimeMS=self._max_ms,
            )
        )

        compras_por_dia = {
            documento["_id"]: documento
            for documento in self._db[MARKETING_COMPRAS].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": "$fecha",
                            "compras": {"$sum": 1},
                            "ingresos_usd": {"$sum": "$ingresos_usd"},
                        }
                    },
                ],
                maxTimeMS=self._max_ms,
            )
        }

        if facetas is None:
            facetas = self.facetas_usuarios(version, filtros)
        por_dia = facetas.get("por_dia") or {}

        return [
            {
                "fecha": fila["_id"],
                "eventos": fila.get("eventos", 0),
                "sesiones": fila.get("sesiones", 0),
                "compras": (compras_por_dia.get(fila["_id"]) or {}).get("compras", 0),
                "ingresos_usd": round(
                    float((compras_por_dia.get(fila["_id"]) or {}).get("ingresos_usd") or 0), 2
                ),
                "usuarios": (por_dia.get(fila["_id"]) or {}).get("usuarios", 0),
                "compradores": (por_dia.get(fila["_id"]) or {}).get("compradores", 0),
            }
            for fila in filas
        ]

    # ------------------------------------------------------------- adquisición
    def adquisicion(
        self,
        version: str,
        filtros: MarketingFiltros,
        facetas: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Por canal de adquisición: usuarios, compradores, compras e ingresos.

        El canal es el de la **primera actividad observada** del usuario, así que
        cada persona cae en un solo canal y las filas suman el total exacto.
        """
        consulta = self.condiciones(version, filtros)
        base = list(
            self._db[MARKETING_RESUMEN].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": "$canal",
                            "eventos": {"$sum": "$eventos"},
                            "sesiones": {"$sum": "$sesiones"},
                        }
                    },
                    {"$sort": {"sesiones": -1}},
                ],
                maxTimeMS=self._max_ms,
            )
        )

        facetas = facetas or self.facetas_usuarios(version, filtros)
        por_canal = facetas.get("por_canal") or {}
        usuarios = {canal: datos["usuarios"] for canal, datos in por_canal.items()}
        compradores = {canal: datos["compradores"] for canal, datos in por_canal.items()}

        compras = {
            documento["_id"]: documento
            for documento in self._db[MARKETING_COMPRAS].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": "$canal",
                            "compras": {"$sum": 1},
                            "ingresos_usd": {"$sum": "$ingresos_usd"},
                        }
                    },
                ],
                maxTimeMS=self._max_ms,
            )
        }

        canales = {fila["_id"] for fila in base}
        canales.update(usuarios)
        canales.update(compras)
        filas = []
        for canal in canales:
            evento = next((fila for fila in base if fila["_id"] == canal), {})
            dinero = compras.get(canal) or {}
            filas.append(
                {
                    "canal": canal or "Sin información",
                    "eventos": evento.get("eventos", 0),
                    "sesiones": evento.get("sesiones", 0),
                    "compras": int(dinero.get("compras") or 0),
                    "ingresos_usd": round(float(dinero.get("ingresos_usd") or 0), 2),
                    "usuarios": usuarios.get(canal, 0),
                    "compradores": compradores.get(canal, 0),
                }
            )
        filas.sort(key=lambda fila: (-fila["usuarios"], -fila["sesiones"], fila["canal"] or ""))
        return filas

    # ------------------------------------------------ respaldos con detalle
    # ------------------------------------------------- adquisición → compra
    def sankey_sesiones(self, version: str, filtros: MarketingFiltros) -> dict[str, Any]:
        """Sesiones por canal de adquisición, dispositivo y compra observada.

        Lee el agregado ``marketing_flujo`` (día × canal × dispositivo × país ×
        resultado); si la versión no está compactada, calcula el flujo desde el
        detalle de sesiones. Cada sesión aporta una vez a cada transición, así que
        las columnas cuadran, y el agregado cuenta aparte las sesiones que usaron un
        dispositivo distinto del de adquisición (se atribuyen al de adquisición).
        """
        if not self._db[MARKETING_FLUJO].count_documents({"version": version}, limit=1):
            return self._sankey_desde_sesiones(version, filtros)
        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_FLUJO].find(consulta, {"_id": 0}).limit(20_000)
        )
        # El agregado no repite dimensiones normalizadas por documento: se agrupa aquí.
        acumulado: dict[tuple[str, str, bool], int] = {}
        contradictorias = 0
        for fila in filas:
            clave = (
                fila.get("canal") or "Sin información",
                fila.get("dispositivo") or "Sin información",
                bool(fila.get("compro")),
            )
            acumulado[clave] = acumulado.get(clave, 0) + int(fila.get("sesiones") or 0)
            contradictorias += int(fila.get("dispositivo_distinto") or 0)
        total = sum(acumulado.values())
        con_compra = sum(sesiones for (_, _, compro), sesiones in acumulado.items() if compro)
        return {
            "filas": [
                {
                    "canal": canal,
                    "dispositivo": dispositivo,
                    "con_compra": compro,
                    "sesiones": sesiones,
                }
                for (canal, dispositivo, compro), sesiones in acumulado.items()
            ],
            "total_sesiones": total,
            "sesiones_con_compra": con_compra,
            "sesiones_dispositivo_distinto": contradictorias,
        }

    def _sankey_desde_sesiones(self, version: str, filtros: MarketingFiltros) -> dict[str, Any]:
        """Calcula el flujo desde el detalle de sesiones (versión no compactada)."""
        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_SESIONES].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": {
                                "canal": "$canal",
                                "dispositivo": "$dispositivo",
                                "compro": "$compro",
                            },
                            "sesiones": {"$sum": 1},
                        }
                    },
                ],
                maxTimeMS=self._max_ms,
            )
        )
        total = sum(fila.get("sesiones", 0) for fila in filas)
        con_compra = sum(f.get("sesiones", 0) for f in filas if (f["_id"].get("compro") or 0))
        contradicciones = self._db[MARKETING_SESIONES].count_documents(
            {**consulta, "$expr": {"$ne": ["$dispositivo", "$dispositivo_sesion"]}},
            maxTimeMS=self._max_ms,
        )
        return {
            "filas": [
                {
                    "canal": f["_id"].get("canal") or "Sin información",
                    "dispositivo": f["_id"].get("dispositivo") or "Sin información",
                    "con_compra": bool(f["_id"].get("compro")),
                    "sesiones": int(f.get("sesiones") or 0),
                }
                for f in filas
            ],
            "total_sesiones": int(total),
            "sesiones_con_compra": int(con_compra),
            "sesiones_dispositivo_distinto": int(contradicciones),
        }

    def _transiciones_desde_sesiones(self, version: str, filtros: MarketingFiltros) -> list[dict[str, Any]]:
        """Calcula los tramos desde el detalle de sesiones (versión no compactada)."""
        consulta = self.condiciones(version, filtros)
        resultado: list[dict[str, Any]] = []
        for indice in range(3):
            desde = {"$arrayElemAt": ["$pasos", indice]}
            hasta = {"$arrayElemAt": ["$pasos", indice + 1]}
            filas = list(
                self._db[MARKETING_SESIONES].aggregate(
                    [
                        {"$match": consulta},
                        {"$project": {"desde": {"$toDate": desde}, "hasta": {"$toDate": hasta}}},
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$ne": ["$desde", None]},
                                        {"$ne": ["$hasta", None]},
                                        {"$gte": ["$hasta", "$desde"]},
                                    ]
                                }
                            }
                        },
                        {"$project": {"horas": {"$divide": [{"$subtract": ["$hasta", "$desde"]}, 3_600_000]}}},
                        {
                            "$bucket": {
                                "groupBy": "$horas",
                                "boundaries": [0, 0.25, 0.5, 1, 2, 4, 8, 24],
                                "default": "mas de 24 h",
                                "output": {"sesiones": {"$sum": 1}},
                            }
                        },
                    ],
                    maxTimeMS=self._max_ms,
                )
            )
            resultado.append({"paso": indice + 1, "buckets": filas})
        return resultado

    def _afinidad_desde_articulos(
        self, version: str, filtros: MarketingFiltros, limite: int, minimo: int
    ) -> dict[str, Any]:
        """Calcula la afinidad desde las líneas de artículo (versión no compactada)."""
        from itertools import combinations

        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_ARTICULOS].aggregate(
                [{"$match": consulta}, {"$group": {"_id": "$compra", "categorias": {"$addToSet": "$categoria"}}}],
                maxTimeMS=self._max_ms,
            )
        )
        por_categoria: dict[str, int] = {}
        pares: dict[tuple[str, str], int] = {}
        for fila in filas:
            categorias = sorted({c for c in fila.get("categorias") or [] if c})
            for categoria in categorias:
                por_categoria[categoria] = por_categoria.get(categoria, 0) + 1
            for a, b in combinations(categorias, 2):
                pares[(a, b)] = pares.get((a, b), 0) + 1
        ordenadas = sorted(por_categoria.items(), key=lambda par: (-par[1], par[0]))
        visibles = {categoria for categoria, _ in ordenadas[:limite]}
        return {
            "categorias": [
                {"categoria": c, "compras": n, "incluida": c in visibles} for c, n in ordenadas
            ],
            "pares": [
                {
                    "a": a,
                    "b": b,
                    "compras_conjuntas": conteo,
                    "compras_a": por_categoria.get(a, 0),
                    "compras_b": por_categoria.get(b, 0),
                    "pct_de_a": _porcentaje(conteo, por_categoria.get(a, 0)),
                    "pct_de_b": _porcentaje(conteo, por_categoria.get(b, 0)),
                }
                for (a, b), conteo in sorted(pares.items(), key=lambda par: -par[1])
                if a in visibles and b in visibles and conteo >= minimo
            ],
            "cobertura": {
                "pares_posibles": len(pares),
                "pares_representados": len(
                    [1 for (a, b), c in pares.items() if a in visibles and b in visibles and c >= minimo]
                ),
                "categorias_distintas": len(por_categoria),
                "categorias_representadas": len(visibles),
                "categorias_excluidas": max(0, len(por_categoria) - len(visibles)),
                "minimo_compras_conjuntas": minimo,
                "limite_categorias": limite,
                "compras_analizadas": len(filas),
            },
            "alcance": _alcance_afinidad(self.importacion(version), aplica_filtros=True),
        }

    # --------------------------------------------------------- sesiones/recorridos
    def embudo_por_dimension(
        self, version: str, filtros: MarketingFiltros, dimension: str
    ) -> list[dict[str, Any]]:
        """Sesiones por paso y valor de una dimensión (canal o dispositivo)."""
        campo = "canal" if dimension == "canal" else "dispositivo"
        if dimension not in ("canal", "dispositivo"):
            raise ValueError(f"Dimensión no admitida: {dimension}")
        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_EMBUDO].aggregate(
                [
                    {"$match": consulta},
                    {"$group": {"_id": {"valor": f"${campo}", "paso": "$paso"}, "sesiones": {"$sum": "$sesiones"}}},
                ],
                maxTimeMS=self._max_ms,
            )
        )
        tabla: dict[str, dict[int, int]] = {}
        for fila in filas:
            valor = fila["_id"].get("valor") or "Sin información"
            tabla.setdefault(valor, {})[fila["_id"].get("paso") or 0] = fila.get("sesiones", 0)
        return [
            {"valor": valor, "pasos": dict(sorted(pasos.items()))}
            for valor, pasos in sorted(tabla.items(), key=lambda par: -sum(par[1].values()))
        ]

    def transiciones(self, version: str, filtros: MarketingFiltros) -> list[dict[str, Any]]:
        """Tiempo entre etapas desde los agregados precalculados fuera de Atlas.

        El detalle por sesión ya no se guarda: los tramos se calculan en la
        preparación con los timestamps reales y las reglas del embudo. El agregado
        guarda fecha, canal, dispositivo y país, así que responde a los mismos
        filtros que el resto de la vista; ``tramo`` es el límite inferior del
        intervalo en horas (la etiqueta la pone el servicio).
        """
        if not self._db[MARKETING_TIEMPOS].count_documents({"version": version}, limit=1):
            return self._transiciones_desde_sesiones(version, filtros)
        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_TIEMPOS].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": {"transicion": "$transicion", "tramo": "$tramo"},
                            "sesiones": {"$sum": "$sesiones"},
                        }
                    },
                ],
                maxTimeMS=self._max_ms,
            )
        )
        orden = ["vista_carrito", "carrito_pago", "pago_compra"]
        resultado: list[dict[str, Any]] = []
        for paso, clave in enumerate(orden, start=1):
            tramos = [
                {"_id": fila["_id"]["tramo"], "sesiones": fila["sesiones"]}
                for fila in filas
                if fila["_id"]["transicion"] == clave
            ]
            resultado.append({"paso": paso, "buckets": tramos})
        return resultado

    def perfil_conversion(self, version: str, filtros: MarketingFiltros) -> list[dict[str, Any]]:
        """N1…N4 por canal de adquisición, desde el embudo agregado.

        El embudo ya exige el orden y la ventana documentados, así que estas cifras
        coinciden con las de la vista de conversión.
        """
        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_EMBUDO].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": {"canal": "$canal", "paso": "$paso"},
                            "sesiones": {"$sum": "$sesiones"},
                        }
                    },
                ],
                maxTimeMS=self._max_ms,
            )
        )
        por_canal: dict[str, dict[int, int]] = {}
        global_por_paso: dict[int, int] = {}
        for fila in filas:
            canal = fila["_id"].get("canal") or "Sin información"
            paso = int(fila["_id"].get("paso") or 0)
            sesiones = int(fila.get("sesiones") or 0)
            por_canal.setdefault(canal, {})[paso] = sesiones
            global_por_paso[paso] = global_por_paso.get(paso, 0) + sesiones
        canales = [
            {"canal": canal, "pasos": {paso: pasos[paso] for paso in sorted(pasos)}}
            for canal, pasos in sorted(por_canal.items(), key=lambda par: -par[1].get(1, 0))
        ]
        return {"canales": canales, "global": global_por_paso}

    def detalle_canal(self, version: str, filtros: MarketingFiltros) -> dict[str, Any]:
        """Evolución, dispositivos y países del canal seleccionado."""
        consulta = self.condiciones(version, filtros)
        serie = list(
            self._db[MARKETING_RESUMEN].aggregate(
                [
                    {"$match": consulta},
                    {"$group": {"_id": "$fecha", "sesiones": {"$sum": "$sesiones"}, "eventos": {"$sum": "$eventos"}}},
                    {"$sort": {"_id": 1}},
                ],
                maxTimeMS=self._max_ms,
            )
        )
        dispositivos = list(
            self._db[MARKETING_USUARIOS].aggregate(
                [
                    {"$match": consulta},
                    {"$group": {"_id": {"dispositivo": "$dispositivo", "usuario": "$usuario"}}},
                    {"$group": {"_id": "$_id.dispositivo", "usuarios": {"$sum": 1}}},
                    {"$sort": {"usuarios": -1}},
                ],
                maxTimeMS=self._max_ms,
            )
        )
        paises = list(
            self._db[MARKETING_USUARIOS].aggregate(
                [
                    {"$match": consulta},
                    {"$group": {"_id": {"pais": "$pais", "usuario": "$usuario"}}},
                    {"$group": {"_id": "$_id.pais", "usuarios": {"$sum": 1}}},
                    {"$sort": {"usuarios": -1}},
                    {"$limit": 8},
                ],
                maxTimeMS=self._max_ms,
            )
        )
        return {
            "serie": [{"fecha": fila["_id"], **{k: v for k, v in fila.items() if k != "_id"}} for fila in serie],
            "dispositivos": [
                {"dispositivo": fila["_id"] or "Sin información", "usuarios": fila["usuarios"]}
                for fila in dispositivos
            ],
            "paises": [
                {"pais": fila["_id"] or "Sin información", "usuarios": fila["usuarios"]}
                for fila in paises
            ],
        }

    # -------------------------------------------------------------- productos
    def productos(self, version: str, filtros: MarketingFiltros, limite: int = 10) -> dict[str, Any]:
        """Categorías con sus compras, artículos, unidades e importe de línea.

        El detalle por artículo ya no se almacena: estas medidas se agregaron fuera
        de Atlas y viven en la colección de categorías. ``compras`` cuenta compras
        distintas que incluyen la categoría (conteo solapado entre categorías).
        """
        consulta = self.condiciones(version, filtros)
        categorias = list(
            self._db[MARKETING_CATEGORIAS].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": "$categoria",
                            "compras": {"$sum": "$compras"},
                            "articulos": {"$sum": "$articulos"},
                            "unidades": {"$sum": "$unidades"},
                            "ingresos_linea_usd": {"$sum": "$ingresos_linea_usd"},
                        }
                    },
                    {"$sort": {"compras": -1, "unidades": -1}},
                    {"$limit": limite},
                ],
                maxTimeMS=self._max_ms,
            )
        )
        return {
            "articulos": [],
            "categorias": [
                {
                    "categoria": fila["_id"] or "Sin información",
                    "compras": fila.get("compras", 0),
                    "articulos": fila.get("articulos", 0),
                    "unidades": fila.get("unidades", 0),
                    "ingresos_linea_usd": round(float(fila.get("ingresos_linea_usd") or 0), 2),
                }
                for fila in categorias
            ],
        }

    def afinidad_categorias(
        self, version: str, filtros: MarketingFiltros, limite: int = 8, minimo: int = 5
    ) -> dict[str, Any]:
        """Compras conjuntas por par de categorías, precalculadas fuera de Atlas."""
        if not self._db[MARKETING_AFINIDAD].count_documents({"version": version}, limit=1):
            return self._afinidad_desde_articulos(version, filtros, limite, minimo)
        pares = list(
            self._db[MARKETING_AFINIDAD].find({"version": version}, {"_id": 0}).limit(20_000)
        )
        compras_por_categoria: dict[str, int] = {}
        for par in pares:
            compras_por_categoria[par["a"]] = par.get("compras_a", 0)
            compras_por_categoria[par["b"]] = par.get("compras_b", 0)
        ordenadas = sorted(compras_por_categoria.items(), key=lambda par: (-par[1], par[0]))
        visibles = {categoria for categoria, _ in ordenadas[:limite]}
        filas = [
            {
                "a": par["a"],
                "b": par["b"],
                "compras_conjuntas": par["compras_conjuntas"],
                "compras_a": par["compras_a"],
                "compras_b": par["compras_b"],
                "pct_de_a": _porcentaje(par["compras_conjuntas"], par["compras_a"]),
                "pct_de_b": _porcentaje(par["compras_conjuntas"], par["compras_b"]),
            }
            for par in sorted(pares, key=lambda par: -par["compras_conjuntas"])
            if par["a"] in visibles and par["b"] in visibles and par["compras_conjuntas"] >= minimo
        ]
        importacion = self.importacion(version) or {}
        return {
            "categorias": [
                {"categoria": categoria, "compras": compras, "incluida": categoria in visibles}
                for categoria, compras in ordenadas
            ],
            "pares": filas,
            "cobertura": {
                "pares_posibles": len(pares),
                "pares_representados": len(filas),
                "categorias_distintas": len(compras_por_categoria),
                "categorias_representadas": len(visibles),
                "categorias_excluidas": max(0, len(compras_por_categoria) - len(visibles)),
                "minimo_compras_conjuntas": minimo,
                "limite_categorias": limite,
                "compras_analizadas": int((importacion.get("cifras") or {}).get("compras") or 0),
            },
            "alcance": _alcance_afinidad(importacion),
        }

    def embudo(self, version: str, filtros: MarketingFiltros) -> list[dict[str, Any]]:
        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_EMBUDO].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": "$paso",
                            "sesiones": {"$sum": "$sesiones"},
                            "sesiones_evento": {"$sum": "$sesiones_evento"},
                            "eventos": {"$sum": "$eventos"},
                            "etiqueta": {"$first": "$etiqueta"},
                        }
                    },
                    {"$sort": {"_id": 1}},
                ],
                maxTimeMS=self._max_ms,
            )
        )
        return [
            {
                "paso": fila["_id"],
                "etiqueta": fila.get("etiqueta") or f"Paso {fila['_id']}",
                "sesiones": fila.get("sesiones", 0),
                "sesiones_evento": fila.get("sesiones_evento", 0),
                "eventos": fila.get("eventos", 0),
            }
            for fila in filas
        ]

    def embudo_por_canal(self, version: str, filtros: MarketingFiltros) -> list[dict[str, Any]]:
        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_EMBUDO].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": {"canal": "$canal", "paso": "$paso"},
                            "sesiones": {"$sum": "$sesiones"},
                        }
                    },
                    {"$sort": {"_id.paso": 1}},
                ],
                maxTimeMS=self._max_ms,
            )
        )
        return [
            {
                "canal": fila["_id"].get("canal") or "Sin información",
                "paso": fila["_id"].get("paso"),
                "sesiones": fila.get("sesiones", 0),
            }
            for fila in filas
        ]

    # ----------------------------------------------------------------- cohortes
    def cohortes(self, version: str, filtros: MarketingFiltros, dimension: str) -> list[dict[str, Any]]:
        """Celdas de la matriz: cohorte, semana y usuarios activos / compradores.

        La dimensión filtra **la definición** de la cohorte (el valor de la primera
        actividad observada), no la actividad de las semanas siguientes.
        """
        consulta = {"version": version, "dimension": dimension}
        if filtros.canales and dimension == "canal":
            consulta["valor"] = {"$in": filtros.canales}
        if filtros.dispositivos and dimension == "dispositivo":
            consulta["valor"] = {"$in": filtros.dispositivos}
        if filtros.paises and dimension == "pais":
            consulta["valor"] = {"$in": filtros.paises}
        if filtros.desde:
            consulta["cohorte"] = {"$gte": filtros.desde.isoformat()}
        if filtros.hasta:
            consulta["cohorte"] = {**consulta.get("cohorte", {}), "$lte": filtros.hasta.isoformat()}

        filas = list(
            self._db[MARKETING_COHORTES].find(
                consulta,
                {
                    "_id": 0,
                    "cohorte": 1,
                    "indice": 1,
                    "valor": 1,
                    "usuarios": 1,
                    "compradores": 1,
                },
            )
            .sort([("cohorte", 1), ("indice", 1)])
            .limit(40_000)
        )
        return filas

    def valores_cohorte(self, version: str, dimension: str) -> list[str]:
        valores = self._db[MARKETING_COHORTES].distinct(
            "valor",
            {"version": version, "dimension": dimension},
            maxTimeMS=self._max_ms,
        )
        return sorted(valor for valor in valores if valor)

    # --------------------------------------------------------------- categorías
    def categorias(self, version: str, filtros: MarketingFiltros, limite: int = 15) -> list[dict[str, Any]]:
        consulta = self.condiciones(version, filtros)
        filas = list(
            self._db[MARKETING_CATEGORIAS].aggregate(
                [
                    {"$match": consulta},
                    {
                        "$group": {
                            "_id": "$categoria",
                            "eventos": {"$sum": "$eventos"},
                            "compras": {"$sum": "$compras"},
                        }
                    },
                    {"$sort": {"compras": -1, "eventos": -1}},
                    {"$limit": limite},
                ],
                maxTimeMS=self._max_ms,
            )
        )
        return [
            {
                "categoria": fila["_id"] or "Sin información",
                "eventos": fila.get("eventos", 0),
                "compras": fila.get("compras", 0),
            }
            for fila in filas
        ]

    # -------------------------------------------------------------------- tabla
    def tabla(
        self,
        version: str,
        filtros: MarketingFiltros,
        *,
        orden: str,
        descendente: bool,
        pagina: int,
        tamano: int,
    ) -> dict[str, Any]:
        consulta = self.condiciones(version, filtros)
        busqueda = filtros.condiciones_busqueda()
        if busqueda:
            consulta = {"$and": [consulta, busqueda]}
        campo = ORDEN_TABLA.get(orden, "fecha")
        cursor = (
            self._db[MARKETING_EVENTOS]
            .find(consulta, CAMPOS_TABLA)
            .sort([(campo, -1 if descendente else 1), ("id", 1)])
            .skip((pagina - 1) * tamano)
            .limit(tamano)
        )
        total = self._db[MARKETING_EVENTOS].count_documents(consulta, maxTimeMS=self._max_ms)
        return {"filas": [_fila(documento) for documento in cursor], "total": total}

    def filas_exportar(self, version: str, filtros: MarketingFiltros, limite: int):
        consulta = self.condiciones(version, filtros)
        busqueda = filtros.condiciones_busqueda()
        if busqueda:
            consulta = {"$and": [consulta, busqueda]}
        cursor = (
            self._db[MARKETING_EVENTOS]
            .find(consulta, CAMPOS_TABLA)
            .sort([("fecha", 1), ("id", 1)])
            .limit(limite)
        )
        for documento in cursor:
            yield _fila(documento)


def _porcentaje(numerador: int, denominador: int) -> float | None:
    """Porcentaje con denominador explícito; ``None`` si el denominador es cero."""
    if not denominador:
        return None
    return round(100 * numerador / denominador, 1)


def _fila(documento: dict[str, Any]) -> dict[str, Any]:
    """Fila de tabla y exportación: lo necesario para el análisis, sin personas."""
    return {
        "fecha": documento.get("fecha"),
        "hora": documento.get("hora"),
        "evento": documento.get("evento"),
        "evento_etiqueta": documento.get("evento_etiqueta") or documento.get("evento"),
        "dispositivo": documento.get("dispositivo"),
        "pais": documento.get("pais"),
        "canal": documento.get("canal"),
        "fuente": documento.get("fuente"),
        "medio": documento.get("medio"),
        "articulo": documento.get("articulo"),
        "categoria": documento.get("categoria"),
        "ingresos_usd": documento.get("ingresos_usd") or 0.0,
    }


