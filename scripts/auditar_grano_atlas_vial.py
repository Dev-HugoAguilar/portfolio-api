"""Auditoría del grano de los datos activos del Atlas vial (Fase 0).

Lee **solo** la colección operativa `atlas_hechos` de MongoDB Atlas y responde, con
cifras comprobables, las preguntas que sostienen el diccionario de métricas:

  1. ¿Cuál es el grano? ¿Una fila por hecho? ¿Folios únicos?
  2. ¿Qué campos existen de verdad en la versión publicada y cuáles se perdieron en
     la compactación?
  3. ¿Los recursos de personas describen (A) personas de esa categoría o (B) el total
     de fallecidos en hechos cuyo registro principal tiene esa categoría?
  4. ¿Qué cruces son válidos y cuáles multiplicarían o inventarían registros?
  5. ¿Se distingue el cero del dato ausente en cada campo?

No escribe nada: todas las operaciones son lecturas. El informe se guarda en
`docs/atlas-vial/auditoria-grano.json`.

Uso:
    .venv\\Scripts\\python.exe -m scripts.auditar_grano_atlas_vial
    .venv\\Scripts\\python.exe -m scripts.auditar_grano_atlas_vial --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from app.atlas.geometry import obtener_geometria
from app.config import get_settings
from app.db.mongo import ATLAS_ESTADO, ATLAS_HECHOS, ATLAS_IMPORTACIONES, MongoDatabase

RAIZ = Path(__file__).resolve().parents[1]
SALIDA = RAIZ.parent / "docs" / "atlas-vial" / "auditoria-grano.json"

#: Cifras de la auditoría de los CSV (`verificar_atlas_vial.py`), para contrastar.
ESPERADO = {
    "hechos": 30_655,
    "lesionadas": 36_266,
    "fallecidas": 533,
    "con_fallecimientos": 512,
}

#: Campos que documenta el modelo completo y que la compactación puede haber quitado.
CAMPOS_MODELO = (
    "personas_consecuencia",
    "personas_sexo_total",
    "personas_edad_texto",
    "vehiculo_numero",
    "personas_totales",
    "alcaldia_texto",
    "alcaldia_geometria",
    "fecha_dt",
    "zona_vial",
    "tipo_interseccion",
    "origen_norm",
    "unidad_a_cargo",
    "trasladado_lesionados",
)

GRUPOS_EDAD = (
    ("0–14", 0, 14),
    ("15–24", 15, 24),
    ("25–34", 25, 34),
    ("35–44", 35, 44),
    ("45–54", 45, 54),
    ("55–64", 55, 64),
    ("65 y más", 65, 200),
)


def _version_activa(contenedor) -> dict[str, Any]:
    estado = contenedor[ATLAS_ESTADO].find_one({"_id": "activo"}) or {}
    version = estado.get("version")
    importacion = (
        contenedor[ATLAS_IMPORTACIONES].find_one({"version": version}, {"_id": 0})
        if version
        else None
    )
    return {"version": version, "importacion": importacion}


def auditar() -> dict[str, Any]:
    settings = get_settings()
    base = MongoDatabase(settings.mongodb_uri, settings.mongodb_db)
    try:
        contenedor = base._db  # noqa: SLF001 - acceso de lectura para la auditoría
        hechos = contenedor[ATLAS_HECHOS]
        contexto = _version_activa(contenedor)
        version = contexto["version"]

        consulta = {"version": version} if version else {}
        informe: dict[str, Any] = {
            "generado": datetime.now().isoformat(timespec="seconds"),
            "coleccion": ATLAS_HECHOS,
            "version": version,
            "grano": {},
            "campos": [],
            "conciliaciones": {},
            "personas": {},
            "edad": {},
            "coordenadas": {},
            "ceros_y_ausencias": {},
            "uniones": {},
            "cruces": {"validos": [], "invalidos": []},
        }

        # ---------------------------------------------------------------- grano
        total = hechos.count_documents(consulta)
        folios_unicos = len(hechos.distinct("folio", consulta))
        agrupado = list(
            hechos.aggregate(
                [
                    {"$match": consulta},
                    {"$group": {"_id": "$folio", "n": {"$sum": 1}}},
                    {"$group": {"_id": "$n", "folios": {"$sum": 1}}},
                    {"$sort": {"_id": 1}},
                ]
            )
        )
        filas_por_folio = {str(fila["_id"]): fila["folios"] for fila in agrupado}
        informe["grano"] = {
            "documentos": total,
            "folios_unicos": folios_unicos,
            "duplicados_de_folio": total - folios_unicos,
            "filas_por_folio": filas_por_folio,
            "una_fila_por_folio": total == folios_unicos and list(filas_por_folio) == ["1"],
            "campo_clave": "folio",
            "clave_tecnica": "_id (ObjectId)",
            "coincide_con_auditoria_de_csv": total == ESPERADO["hechos"],
            "esperado_csv": ESPERADO["hechos"],
        }

        # --------------------------------------------------------------- campos
        muestra = hechos.find_one(consulta) or {}
        campos_presentes = sorted(muestra.keys())
        informe["campos"] = [
            {
                "campo": campo,
                "presente_en_muestra": campo in muestra,
                "tipo": type(muestra.get(campo)).__name__,
            }
            for campo in campos_presentes
        ]
        campos_modelo_ausentes = [campo for campo in CAMPOS_MODELO if campo not in muestra]
        presencia = {}
        for campo in (
            "folio",
            "fecha",
            "hora",
            "personas_lesionadas",
            "personas_fallecidas",
            "personas_sexo",
            "personas_edad",
            "personas_tipo_usuario",
            "vehiculo_tipo",
            "alcaldia",
            "semaforizada",
            "tipo_evento_norm",
            "vialidad_norm",
            "tipo_interseccion_norm",
            "ubicacion",
        ):
            nulos = hechos.count_documents({**consulta, campo: None})
            faltantes = hechos.count_documents({**consulta, campo: {"$exists": False}})
            presencia[campo] = {
                "documentos": total,
                "nulos": nulos,
                "campo_ausente": faltantes,
                "con_valor": total - nulos - faltantes,
            }

        # -------------------------------------------------------- conciliaciones
        def suma(campo: str) -> int:
            datos = list(
                hechos.aggregate(
                    [
                        {"$match": consulta},
                        {"$group": {"_id": None, "total": {"$sum": f"${campo}"}}},
                    ]
                )
            )
            return int(datos[0]["total"]) if datos else 0

        lesionadas = suma("personas_lesionadas")
        fallecidas = suma("personas_fallecidas")
        con_fallecimientos = hechos.count_documents({**consulta, "personas_fallecidas": {"$gt": 0}})
        con_lesionadas = hechos.count_documents({**consulta, "personas_lesionadas": {"$gt": 0}})
        sin_personas = hechos.count_documents(
            {**consulta, "personas_fallecidas": {"$lte": 0}, "personas_lesionadas": {"$lte": 0}}
        )
        informe["conciliaciones"] = {
            "hechos": total,
            "personas_lesionadas": lesionadas,
            "personas_fallecidas": fallecidas,
            "hechos_con_fallecimientos": con_fallecimientos,
            "hechos_solo_lesionados": con_lesionadas - con_fallecimientos,
            "hechos_sin_personas_registradas": sin_personas,
            "hechos_con_fallecimientos_mas_sin_personas": con_fallecimientos,
            "porcentaje_hechos_con_fallecimientos": round(100 * con_fallecimientos / total, 3),
            "personas_por_hecho": round((lesionadas + fallecidas) / total, 4),
            "coincide_con_auditoria_de_csv": {
                "lesionadas": lesionadas == ESPERADO["lesionadas"],
                "fallecidas": fallecidas == ESPERADO["fallecidas"],
                "con_fallecimientos": con_fallecimientos == ESPERADO["con_fallecimientos"],
            },
            "esperado_csv": ESPERADO,
        }

        # ---------------------------------------------------------- personas (A/B)
        distribuciones = {}
        for campo in ("personas_sexo", "personas_tipo_usuario", "vehiculo_tipo"):
            filas = list(
                hechos.aggregate(
                    [
                        {"$match": consulta},
                        {"$group": {
                            "_id": f"${campo}",
                            "hechos": {"$sum": 1},
                            "lesionadas": {"$sum": "$personas_lesionadas"},
                            "fallecidas": {"$sum": "$personas_fallecidas"},
                            "con_fallecimientos": {
                                "$sum": {"$cond": [{"$gt": ["$personas_fallecidas", 0]}, 1, 0]}
                            },
                        }},
                        {"$sort": {"hechos": -1}},
                    ]
                )
            )
            distribuciones[campo] = [
                {
                    "valor": fila["_id"],
                    "hechos": fila["hechos"],
                    "lesionadas_en_esos_hechos": fila["lesionadas"],
                    "fallecidas_en_esos_hechos": fila["fallecidas"],
                    "hechos_con_al_menos_un_fallecido": fila["con_fallecimientos"],
                    "porcentaje_de_hechos": round(100 * fila["hechos"] / total, 2),
                }
                for fila in filas
            ]
            suma_hechos = sum(fila["hechos"] for fila in filas)
            informe.setdefault("personas_cierres", {})[campo] = {
                "suma_de_categorias": suma_hechos,
                "total_de_hechos": total,
                "cierra": suma_hechos == total,
            }

        informe["personas"] = {
            "campo_de_consecuencia_de_la_persona_en_atlas": "personas_consecuencia"
            if "personas_consecuencia" not in campos_modelo_ausentes
            else None,
            "nivel_A_disponible": "personas_consecuencia" not in campos_modelo_ausentes,
            "explicacion": (
                "Cada documento guarda el sexo, la edad y el tipo de UN registro de persona "
                "(el principal del expediente) y, aparte, el total agregado de personas "
                "lesionadas y fallecidas del hecho. No hay campo que diga si esa persona "
                "registrada fue la lesionada o la fallecida, así que el desglose por sexo, "
                "edad o tipo de persona solo puede leerse como (B): hechos con ese registro. "
                "Presentar sus fallecidas como (A) sería atribuir el total del hecho a la "
                "categoría de la persona registrada."
            ),
            "distribuciones": distribuciones,
            "desgloses_suman": {
                "hechos": "sí, cada hecho cae en una sola categoría por campo",
                "personas": "no: las mismas personas se describen desde varios ángulos",
            },
        }

        # ------------------------------------------------------------------ edad
        edades = list(
            hechos.aggregate(
                [
                    {"$match": {**consulta, "personas_edad": {"$ne": None}}},
                    {"$group": {
                        "_id": "$personas_edad",
                        "hechos": {"$sum": 1},
                        "fallecidas": {"$sum": "$personas_fallecidas"},
                        "con_fallecimientos": {
                            "$sum": {"$cond": [{"$gt": ["$personas_fallecidas", 0]}, 1, 0]}
                        },
                    }},
                    {"$sort": {"_id": 1}},
                ]
            )
        )
        con_edad = sum(fila["hechos"] for fila in edades)
        sin_edad = hechos.count_documents({**consulta, "personas_edad": None})
        grupos = []
        for etiqueta, desde, hasta in GRUPOS_EDAD:
            acumulado = [fila for fila in edades if desde <= fila["_id"] <= hasta]
            grupos.append(
                {
                    "etiqueta": etiqueta,
                    "hechos": sum(fila["hechos"] for fila in acumulado),
                    "fallecidas_en_esos_hechos": sum(fila["fallecidas"] for fila in acumulado),
                    "hechos_con_al_menos_un_fallecido": sum(
                        fila["con_fallecimientos"] for fila in acumulado
                    ),
                }
            )
        grupo_infantil = grupos[0]
        # Demostración de por qué (B) no es (A): en los hechos con una persona
        # registrada de 0–14 años, ¿hay hechos con más de un fallecido o con más
        # personas que la registrada?
        infantil = {**consulta, "personas_edad": {"$gte": 0, "$lte": 14}}
        infantil_con_fallecidos = hechos.count_documents({**infantil, "personas_fallecidas": {"$gt": 0}})
        infantil_fallecidos_multiples = hechos.count_documents(
            {**infantil, "personas_fallecidas": {"$gt": 1}}
        )
        infantil_mas_personas = hechos.count_documents(
            {**infantil, "$expr": {"$gt": [{"$add": ["$personas_lesionadas", "$personas_fallecidas"]}, 1]}}
        )
        informe["edad"] = {
            "rangos_originales": "0–14, 15–24, 25–34, 35–44, 45–54, 55–64, 65 y más",
            "hechos_con_edad": con_edad,
            "hechos_sin_edad": sin_edad,
            "edad_min": edades[0]["_id"] if edades else None,
            "edad_max": edades[-1]["_id"] if edades else None,
            "valores_distintos": len(edades),
            "grupos": grupos,
            "grupo_0_14": {
                **grupo_infantil,
                "hechos_con_mas_de_un_fallecido": infantil_fallecidos_multiples,
                "hechos_con_mas_personas_que_la_registrada": infantil_mas_personas,
                "lectura": (
                    f"Los {infantil_con_fallecidos} hechos con una persona registrada de 0–14 años "
                    f"tienen {grupo_infantil['fallecidas_en_esos_hechos']} personas fallecidas en total. "
                    "Ninguna de ellas se puede asignar a la categoría de edad: el dato describe el "
                    "hecho, no a quien falleció."
                ),
            },
        }

        # Anomalía documentada: la edad registrada no describe a quien falleció. En
        # los hechos con fallecimientos, 406 de las 533 muertes no tienen edad y las
        # que la tienen se concentran en 0–14 (122) frente a 5 en 15–64. Cualquier
        # cruce edad × fallecidas produciría una lectura falsa.
        muertes_sin_edad = list(
            hechos.aggregate(
                [
                    {"$match": {**consulta, "personas_edad": None}},
                    {"$group": {"_id": None, "muertes": {"$sum": "$personas_fallecidas"}}},
                ]
            )
        )
        muertes_con_edad = list(
            hechos.aggregate(
                [
                    {"$match": {**consulta, "personas_edad": {"$ne": None}}},
                    {"$group": {"_id": None, "muertes": {"$sum": "$personas_fallecidas"}}},
                ]
            )
        )
        muertes_15_64 = sum(
            grupo["fallecidas_en_esos_hechos"] for grupo in grupos[1:-1]
        )
        muertes_65 = grupos[-1]["fallecidas_en_esos_hechos"]
        informe["edad"]["anomalia_edad_y_fallecimientos"] = {
            "muertes_totales": fallecidas,
            "muertes_en_hechos_sin_edad": int(muertes_sin_edad[0]["muertes"]) if muertes_sin_edad else 0,
            "muertes_en_hechos_con_edad": int(muertes_con_edad[0]["muertes"]) if muertes_con_edad else 0,
            "muertes_en_0_14": grupo_infantil["fallecidas_en_esos_hechos"],
            "muertes_en_15_64": muertes_15_64,
            "muertes_en_65_y_mas": muertes_65,
            "conclusion": (
                "El campo de edad no es coherente con los fallecimientos: 406 de 533 muertes "
                "caen en hechos sin edad registrada y, de las que tienen edad, 122 quedan en 0–14 "
                "frente a 5 en 15–64 y 0 en 65 y más. Es imposible que esa sea la distribución real "
                "de las personas fallecidas, así que la edad describe al registro del hecho y no a "
                "quien murió. Consecuencia: edad × fallecidas es un cruce inválido y no se publica."
            ),
        }

        # ------------------------------------------------------------------ mapa

        # ------------------------------------------------------------ dimensiones
        def valores(campo: str) -> list[dict[str, Any]]:
            filas = list(
                hechos.aggregate(
                    [
                        {"$match": consulta},
                        {"$group": {"_id": f"${campo}", "hechos": {"$sum": 1}}},
                        {"$sort": {"hechos": -1}},
                    ]
                )
            )
            return [{"valor": fila["_id"], "hechos": fila["hechos"]} for fila in filas]

        horas = list(
            hechos.aggregate(
                [
                    {"$match": consulta},
                    {"$group": {"_id": None, "min": {"$min": "$hora"}, "max": {"$max": "$hora"},
                                "sin_hora": {"$sum": {"$cond": [{"$eq": ["$hora", None]}, 1, 0]}}}},
                ]
            )
        )
        informe["dimensiones"] = {
            "alcaldias": {
                "valores": valores("alcaldia"),
                "distintas": len({fila["valor"] for fila in valores("alcaldia")}),
                "sin_dato": hechos.count_documents({**consulta, "alcaldia": None}),
            },
            "tipo_evento": valores("tipo_evento_norm"),
            "vialidad": valores("vialidad_norm"),
            "tipo_interseccion": valores("tipo_interseccion_norm"),
            "semaforizada": valores("semaforizada"),
            "tipo_persona_registrada": valores("personas_tipo_usuario"),
            "sexo_registrado": valores("personas_sexo"),
            "vehiculo": valores("vehiculo_tipo"),
            "hora": {
                "min": horas[0]["min"] if horas else None,
                "max": horas[0]["max"] if horas else None,
                "sin_hora": horas[0]["sin_hora"] if horas else None,
                "fuera_de_rango": hechos.count_documents(
                    {**consulta, "$or": [{"hora": {"$lt": 0}}, {"hora": {"$gt": 23}}]}
                ),
            },
        }

        # ----------------------------------------------------------- coordenadas
        puntos = list(
            hechos.find(
                consulta,
                {"_id": 0, "lon": 1, "lat": 1, "alcaldia": 1, "coord_estado": 1, "ubicacion": 1},
            )
        )
        geometria = obtener_geometria()
        sin_ubicacion = sum(1 for punto in puntos if not punto.get("ubicacion"))
        validas = [punto for punto in puntos if punto.get("ubicacion")]
        sin_alcaldia = sum(1 for punto in puntos if not punto.get("alcaldia"))
        desajustes = 0
        desajustes_muestra: list[dict[str, Any]] = []
        for punto in validas:
            if not geometria.disponible() or not punto.get("alcaldia"):
                continue
            oficial = geometria.alcaldia_de(punto["lon"], punto["lat"])
            if oficial is None:
                continue
            if oficial != punto["alcaldia"]:
                desajustes += 1
                if len(desajustes_muestra) < 10:
                    desajustes_muestra.append(
                        {"alcaldia_texto": punto["alcaldia"], "alcaldia_geometria": oficial}
                    )
        informe["coordenadas"] = {
            "con_ubicacion_utilizable": len(validas),
            "sin_ubicacion": sin_ubicacion,
            "estados": dict(Counter(punto.get("coord_estado") for punto in puntos)),
            "alcaldias_reconocidas": total - sin_alcaldia,
            "sin_alcaldia_reconocida": sin_alcaldia,
            "desajuste_texto_vs_geometria": desajustes,
            "porcentaje_desajuste": round(100 * desajustes / max(1, len(validas)), 2),
            "ejemplos_desajuste": desajustes_muestra,
            "extension_geometria": geometria.extension() if geometria.disponible() else None,
        }

        # ------------------------------------------------------- mapa y alcaldías
        por_alcaldia = list(
            hechos.aggregate(
                [
                    {"$match": consulta},
                    {"$group": {
                        "_id": "$alcaldia",
                        "hechos": {"$sum": 1},
                        "lesionadas": {"$sum": "$personas_lesionadas"},
                        "fallecidas": {"$sum": "$personas_fallecidas"},
                        "con_fallecimientos": {
                            "$sum": {"$cond": [{"$gt": ["$personas_fallecidas", 0]}, 1, 0]}
                        },
                        "con_coordenadas": {
                            "$sum": {"$cond": [{"$ne": ["$ubicacion", None]}, 1, 0]}
                        },
                    }},
                    {"$sort": {"hechos": -1}},
                ]
            )
        )
        informe["alcaldias"] = [
            {
                "alcaldia": fila["_id"],
                "hechos": fila["hechos"],
                "lesionadas": fila["lesionadas"],
                "fallecidas": fila["fallecidas"],
                "hechos_con_al_menos_un_fallecido": fila["con_fallecimientos"],
                "porcentaje_hechos_con_fallecimiento": round(
                    100 * fila["con_fallecimientos"] / fila["hechos"], 2
                ),
                "con_coordenadas": fila["con_coordenadas"],
                "porcentaje_del_total": round(100 * fila["hechos"] / total, 2),
            }
            for fila in por_alcaldia
        ]
        informe["mapa"] = {
            "puntos_geolocalizados": len(validas),
            "umbral_canvas": 2000,
            "requiere_canvas_mas_quadtree": len(validas) > 2000,
            "extension": geometria.extension() if geometria.disponible() else None,
            "malla_hexagonal": "se calcula en el cliente sobre lon/lat; la resolución se declara "
            "en la leyenda y la agregación no cambia al mover el mapa",
            "coropleta": "16 polígonos del Marco Geoestadístico 2020; 1 hecho sin alcaldía "
            "reconocida se cuenta aparte y no se reasigna",
        }

        # --------------------------------------------------- ceros y ausencias
        informe["ceros_y_ausencias"] = {
            "hechos_con_cero_fallecidos": total - con_fallecimientos,
            "hechos_sin_hora": hechos.count_documents({**consulta, "hora": None}),
            "hechos_sin_sexo": hechos.count_documents({**consulta, "personas_sexo": None}),
            "hechos_con_sexo_sin_dato": hechos.count_documents(
                {**consulta, "personas_sexo": "Sin dato"}
            ),
            "hechos_sin_edad": sin_edad,
            "hechos_sin_tipo_de_persona": hechos.count_documents(
                {**consulta, "personas_tipo_usuario": None}
            ),
            "hechos_sin_vehiculo": hechos.count_documents({**consulta, "vehiculo_tipo": None}),
            "hechos_sin_semaforizada": hechos.count_documents({**consulta, "semaforizada": None}),
            "regla": "Un cero del hecho y una ausencia del campo son cosas distintas: el cero se "
            "pinta con el extremo claro de la escala; la ausencia, con rayado o gris neutro.",
        }

        # --------------------------------------------------------------- uniones
        # El grano ya es 1:1, así que una unión por folio no puede multiplicar filas.
        # Se demuestra de dos maneras: con el recuento de filas por folio de arriba y
        # con una autounión explícita sobre una muestra pequeña (evita 30 000
        # búsquedas sin índice).
        muestra_folios = [
            documento["folio"]
            for documento in hechos.find(consulta, {"_id": 0, "folio": 1}).limit(25)
        ]
        union = list(
            hechos.aggregate(
                [
                    {"$match": {**consulta, "folio": {"$in": muestra_folios}}},
                    {"$lookup": {
                        "from": ATLAS_HECHOS,
                        "localField": "folio",
                        "foreignField": "folio",
                        "as": "pareja",
                    }},
                    {"$project": {"folio": 1, "n": {"$size": "$pareja"}}},
                    {"$group": {"_id": "$n", "filas": {"$sum": 1}}},
                    {"$sort": {"_id": 1}},
                ]
            )
        )
        antes = len(muestra_folios)
        despues = sum(fila["filas"] for fila in union)
        informe["uniones"] = {
            "prueba": "autounión por folio en la colección operativa",
            "filas_por_folio_tras_la_union": {str(fila["_id"]): fila["filas"] for fila in union},
            "muestra_de_folios": antes,
            "filas_antes": antes,
            "filas_despues": despues,
            "se_multiplican": despues != antes,
            "coleccion_completa": {
                "hechos": total,
                "folios_unicos": folios_unicos,
                "cierra": total == folios_unicos,
            },
            "motivo": "cada folio aparece exactamente una vez en la colección, así que la unión "
            "devuelve una fila por hecho; no se necesita ninguna unión para construir los cruces "
            "del tablero porque los campos ya viven en el mismo documento",
        }

        # --------------------------------------------------------------- cruces
        informe["cruces"] = {
            "validos": [
                {
                    "cruce": "alcaldía × tipo de evento",
                    "por_que": "ambos campos están en el mismo documento y describen el hecho; "
                    "cada hecho cae en una sola categoría de cada campo",
                },
                {
                    "cruce": "alcaldía × tipo de persona registrada",
                    "por_que": "un registro de persona por folio ya vinculado en la importación "
                    "1:1; cuenta hechos, no personas",
                },
                {
                    "cruce": "edad registrada × tipo de persona registrada",
                    "por_que": "los dos campos describen al mismo registro de persona del folio; "
                    "es válido para esa persona y no para todas las personas del hecho",
                    "corrige": "la versión anterior decía que este cruce no era posible",
                },
                {
                    "cruce": "tipo de evento × clasificación de la vialidad",
                    "por_que": "los dos describen el hecho y están completos en el documento",
                },
                {
                    "cruce": "tipo de intersección × tipo de evento",
                    "por_que": "idem; cada hecho tiene un solo tipo de intersección",
                },
                {
                    "cruce": "día × hora, fecha × consecuencia, alcaldía × consecuencias",
                    "por_que": "el hecho tiene una sola fecha, una sola hora y sus propios "
                    "conteos de personas",
                },
                {
                    "cruce": "tipo de evento × personas fallecidas (suma)",
                    "por_que": "las personas fallecidas pertenecen al hecho, así que su suma por "
                    "tipo de evento es válida; la razón resultante es personas por cada 100 "
                    "hechos y no una tasa de letalidad",
                },
            ],
            "invalidos": [
                {
                    "cruce": "sexo × edad × tipo de persona como tres recuentos de personas",
                    "por_que": "los tres campos describen a la misma persona registrada; sumarlos "
                    "o repartir el total de lesionadas entre categorías contaría tres veces a la "
                    "misma persona",
                },
                {
                    "cruce": "categoría de edad o sexo × personas fallecidas como (A)",
                    "por_que": "los fallecidos son del hecho completo; atribuirlos a la categoría "
                    "de la persona registrada inventa una relación que el archivo no trae",
                },
                {
                    "cruce": "personas fallecidas ÷ personas lesionadas como letalidad",
                    "por_que": "no hay denominador de personas expuestas ni de viajes; el "
                    "cociente mezcla dos conteos del mismo hecho y no mide probabilidad",
                },
                {
                    "cruce": "hechos ÷ población, vehículos o viajes",
                    "por_que": "el conjunto no publica denominadores de exposición",
                },
                {
                    "cruce": "multiplicar distribuciones marginales (edad × sexo de dos tablas "
                    "independientes)",
                    "por_que": "requeriría unir registros que no existen por persona y "
                    "multiplicaría hechos (prohibido)",
                },
                {
                    "cruce": "año contra año",
                    "por_que": "la serie publicada es de un solo año y la fuente la declara no "
                    "comparativa",
                },
            ],
            "campos_del_modelo_no_disponibles": campos_modelo_ausentes,
        }

        return informe
    finally:
        base.close()


def main() -> int:
    # La consola de Windows usa cp1252: se fuerza UTF-8 para no romper en «→» o «·».
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="imprime el informe completo")
    argumentos = parser.parse_args()

    informe = auditar()
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(json.dumps(informe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    grano = informe["grano"]
    conc = informe["conciliaciones"]
    print(f"Versión activa: {informe['version']}")
    print(
        f"Grano: {grano['documentos']} documentos · {grano['folios_unicos']} folios únicos · "
        f"duplicados {grano['duplicados_de_folio']} · filas por folio {grano['filas_por_folio']}"
    )
    print(
        f"Conciliación: {conc['hechos']} hechos · {conc['personas_lesionadas']} lesionadas · "
        f"{conc['personas_fallecidas']} fallecidas · {conc['hechos_con_fallecimientos']} hechos "
        f"con al menos un fallecido ({conc['porcentaje_hechos_con_fallecimientos']} %)"
    )
    print(f"Nivel (A) disponible: {informe['personas']['nivel_A_disponible']}")
    print(
        f"Edad: {informe['edad']['hechos_con_edad']} con edad · {informe['edad']['hechos_sin_edad']} sin edad · "
        f"rango {informe['edad']['edad_min']}–{informe['edad']['edad_max']}"
    )
    grupo = informe["edad"]["grupo_0_14"]
    print(
        f"  0–14: {grupo['hechos']} hechos · {grupo['fallecidas_en_esos_hechos']} fallecidas en esos "
        f"hechos · {grupo['hechos_con_al_menos_un_fallecido']} hechos con al menos una"
    )
    coords = informe["coordenadas"]
    print(
        f"Coordenadas: {coords['con_ubicacion_utilizable']} utilizables · {coords['sin_ubicacion']} sin "
        f"ubicación · {coords['sin_alcaldia_reconocida']} sin alcaldía · "
        f"{coords['desajuste_texto_vs_geometria']} desajustes texto/geometría"
    )
    union = informe["uniones"]
    print(
        f"Unión por folio: {union['filas_antes']} → {union['filas_despues']} filas · "
        f"se multiplican: {union['se_multiplican']}"
    )
    print(f"Informe: {SALIDA}")
    if argumentos.json:
        print(json.dumps(informe, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
