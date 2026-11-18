"""Publica la versión activa de «Pulso comercial · Walmex» en MongoDB Atlas.

Lee lo preparado fuera de Atlas (``data/walmex/preparado``), comprueba que no haya
validaciones bloqueantes fallidas y escribe una versión nueva:

    walmex_hechos        periodo × mercado × indicador × base
    walmex_catalogo      indicadores, mercados, periodos y combinaciones disponibles
    walmex_fuentes       documentos oficiales con URL, hash, fecha y páginas
    walmex_importaciones manifiesto de la versión (conteos, validaciones y tamaños)
    walmex_estado        puntero a la versión activa (cambio en una sola operación)

Reglas:
* Solo se publican datos agrupados: no hay filas por tienda, producto ni cliente.
* Se publica una versión nueva y el puntero cambia al final; la anterior se retira
  de las colecciones operativas en la misma operación para no acumular versiones.
* Antes y después se miden tamaños (colecciones, base y clúster) y el pico de la
  carga, y se comparan con el presupuesto del tablero.

Uso::

    python -m scripts.publicar_walmex                 # simulación
    python -m scripts.publicar_walmex --aplicar       # escribe en Atlas
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from app.config import get_settings

RAIZ = Path(__file__).resolve().parent.parent
PREPARADO = RAIZ / "data" / "walmex" / "preparado"

ESTADO = "walmex_estado"
HECHOS = "walmex_hechos"
CATALOGO = "walmex_catalogo"
FUENTES = "walmex_fuentes"
IMPORTACIONES = "walmex_importaciones"

#: Presupuesto del tablero dentro del clúster compartido (el real es ~0,2 MiB).
PRESUPUESTO_MIB = 5.0

DEFINICIONES: dict[str, dict[str, Any]] = {
    "total_ingresos": {
        "etiqueta": "Total de ingresos",
        "unidad": "millones_mxn",
        "tipo": "importe",
        "definicion": (
            "Ingresos consolidados del trimestre según el estado de resultados por "
            "región: incluye ventas netas y otros ingresos."
        ),
    },
    "total_ingresos_anio_anterior": {
        "etiqueta": "Total de ingresos del mismo trimestre del año anterior",
        "unidad": "millones_mxn",
        "tipo": "importe",
        "definicion": "Misma cifra del año anterior, publicada por la empresa en la misma tabla.",
    },
    "ventas_netas": {
        "etiqueta": "Ventas netas consolidadas",
        "unidad": "millones_mxn",
        "tipo": "importe",
        "definicion": (
            "Ventas netas del trimestre del estado de resultados consolidado. No incluye "
            "otros ingresos; no es GMV ni ventas de comercio electrónico."
        ),
    },
    "crecimiento_anual": {
        "etiqueta": "Crecimiento anual",
        "unidad": "porcentaje",
        "tipo": "variacion",
        "definicion": (
            "Variación porcentual frente al mismo trimestre del año anterior. La base "
            "«nominal_publicado» es la que reporta la empresa; «nominal_calculado» se obtiene "
            "de los importes; «moneda_constante_publicado» elimina el efecto del tipo de "
            "cambio y solo se publica para Centroamérica."
        ),
    },
    "mismas_tiendas": {
        "etiqueta": "Ventas a unidades iguales",
        "unidad": "porcentaje",
        "tipo": "variacion",
        "definicion": (
            "Variación de ventas a unidades iguales (mismas tiendas) que publica la empresa "
            "en su histórico de ventas y en el comunicado del trimestre."
        ),
    },
    "trafico": {
        "etiqueta": "Variación del tráfico",
        "unidad": "porcentaje",
        "tipo": "variacion",
        "definicion": (
            "Variación porcentual del tráfico que la empresa describe en los datos relevantes "
            "del comunicado. No es un conteo de visitas ni un volumen absoluto."
        ),
    },
    "ticket": {
        "etiqueta": "Variación del ticket",
        "unidad": "porcentaje",
        "tipo": "variacion",
        "definicion": (
            "Variación porcentual del ticket promedio publicada en los datos relevantes del "
            "comunicado. No es un importe por transacción."
        ),
    },
}

MERCADOS = {
    "consolidado": "Consolidado",
    "mexico": "México",
    "centroamerica": "Centroamérica",
}

NOTAS = [
    "Cada cifra proviene de un documento oficial de Walmex identificado en walmex_fuentes.",
    "Los importes están en millones de pesos nominales, tal como los publica la empresa.",
    "Comercio electrónico (ventas netas y GMV) no se publica en esta versión: la empresa "
    "cambia la definición entre trimestres y no hay cobertura comparable en todo el periodo.",
    "Tráfico y ticket se publican como variaciones de la empresa; no se usan como "
    "descomposición exacta del crecimiento (la identidad no se cumple en 8 de 18 trimestres).",
    "No hay metas ni guidance en el modelo: la comparación es contra el año anterior.",
]


def _orden_periodo(periodo: str) -> int:
    return int(f"20{periodo[2:]}") * 10 + int(periodo[0])


def _tamano(db, nombres: list[str]) -> dict[str, Any]:
    total = 0.0
    detalle = {}
    for nombre in nombres:
        if nombre not in db.list_collection_names():
            continue
        stats = db.command("collStats", nombre)
        mib = (stats.get("size", 0) + stats.get("totalIndexSize", 0)) / 1048576
        detalle[nombre] = {
            "docs": stats.get("count"),
            "data_mib": round(stats.get("size", 0) / 1048576, 4),
            "index_mib": round(stats.get("totalIndexSize", 0) / 1048576, 4),
            "total_mib": round(mib, 4),
        }
        total += mib
    return {"colecciones": detalle, "total_mib": round(total, 4)}


def _clusters(cliente) -> dict[str, Any]:
    filas = []
    total = 0.0
    for nombre in cliente.list_database_names():
        if nombre in ("admin", "local", "config"):
            continue
        try:
            stats = cliente[nombre].command("dbStats", scale=1)
        except Exception:
            continue
        logico = (stats.get("dataSize", 0) + stats.get("indexSize", 0)) / 1048576
        total += logico
        filas.append((nombre, round(logico, 2)))
    filas.sort(key=lambda par: -par[1])
    return {"bases": filas, "total_mib": round(total, 2), "margen_mib": round(512 - total, 2)}


def construir_documentos(hechos: list[dict], fuentes: list[dict], manifiesto: dict, version: str) -> dict[str, Any]:
    publicaciones = {
        fuente["trimestre"]: fuente.get("fecha_publicacion")
        for fuente in fuentes
        if fuente.get("trimestre")
    }
    documentos_hechos = []
    for hecho in hechos:
        documentos_hechos.append(
            {
                "version": version,
                "periodo": hecho["periodo"],
                "orden": _orden_periodo(hecho["periodo"]),
                "anio": int(f"20{hecho['periodo'][2:]}"),
                "trimestre": int(hecho["periodo"][0]),
                "mercado": hecho["mercado"],
                "indicador": hecho["indicador"],
                "base": hecho["base"],
                "valor": hecho["valor"],
                "unidad": hecho["unidad"],
                "moneda": "MXN" if hecho["unidad"] == "millones_mxn" else None,
                "fuente": hecho["fuente"],
                "publicado": bool(hecho["base"].endswith("publicado"))
                or hecho["base"] in ("unidades_iguales_publicado", "publicado_comunicado"),
                "derivado": hecho["base"].endswith("calculado"),
            }
        )

    periodos = []
    for periodo in sorted({hecho["periodo"] for hecho in hechos}, key=_orden_periodo):
        periodos.append(
            {
                "periodo": periodo,
                "orden": _orden_periodo(periodo),
                "anio": int(f"20{periodo[2:]}"),
                "trimestre": int(periodo[0]),
                "etiqueta": f"{periodo[0]}T {periodo[2:]}",
                "fecha_publicacion": publicaciones.get(periodo),
            }
        )

    combinaciones = sorted(
        {(hecho["mercado"], hecho["indicador"], hecho["base"]) for hecho in hechos},
        key=lambda triple: (triple[0], triple[1], triple[2]),
    )
    catalogo = {
        "version": version,
        "indicadores": [
            {"clave": clave, **datos, "bases": sorted({h["base"] for h in hechos if h["indicador"] == clave})}
            for clave, datos in DEFINICIONES.items()
        ],
        "mercados": [{"clave": clave, "etiqueta": etiqueta} for clave, etiqueta in MERCADOS.items()],
        "periodos": periodos,
        "combinaciones": [
            {"mercado": mercado, "indicador": indicador, "base": base}
            for mercado, indicador, base in combinaciones
        ],
        "ultimo_periodo": periodos[-1]["periodo"] if periodos else None,
        "cobertura": manifiesto.get("cobertura"),
        "conclusiones_validacion": manifiesto.get("conclusiones"),
        "notas": NOTAS,
        "alcance": {
            "unidad_importes": "millones de pesos mexicanos nominales",
            "unidad_variaciones": "porcentaje frente al mismo trimestre del año anterior",
            "detalle": "no se publica detalle por tienda, formato, producto ni cliente",
        },
    }
    return {
        "hechos": documentos_hechos,
        "catalogo": catalogo,
        "fuentes": fuentes,
        "importacion": {
            "version": version,
            "estado": "publicada",
            "creado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "periodos": len(periodos),
            "primer_periodo": periodos[0]["periodo"] if periodos else None,
            "ultimo_periodo": periodos[-1]["periodo"] if periodos else None,
            "hechos": len(documentos_hechos),
            "fuentes": len(fuentes),
            "validaciones": manifiesto.get("resumen", {}).get("validaciones"),
            "validaciones_bloqueantes_fallidas": manifiesto.get("resumen", {}).get(
                "validaciones_bloqueantes_fallidas"
            ),
            "informativas_no_cumplidas": manifiesto.get("resumen", {}).get(
                "informativas_no_cumplidas"
            ),
            "conclusiones": manifiesto.get("conclusiones"),
            "modelo": "walmex-v1",
        },
    }


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Publica Walmex en Atlas")
    argumentos.add_argument("--aplicar", action="store_true", help="Escribe en Atlas.")
    opciones = argumentos.parse_args()

    hechos = json.loads((PREPARADO / "hechos.json").read_text(encoding="utf-8"))
    fuentes = json.loads((PREPARADO / "fuentes.json").read_text(encoding="utf-8"))
    manifiesto = json.loads((PREPARADO / "manifiesto.json").read_text(encoding="utf-8"))

    resumen = manifiesto.get("resumen", {})
    if resumen.get("validaciones_bloqueantes_fallidas"):
        print("Hay validaciones bloqueantes fallidas: no se publica.")
        return 1

    ultimo = max((hecho["periodo"] for hecho in hechos), key=_orden_periodo)
    version = f"walmex-{ultimo.lower()}"
    documentos = construir_documentos(hechos, fuentes, manifiesto, version)
    print(f"versión: {version}")
    print(
        f"hechos: {len(documentos['hechos'])} · fuentes: {len(documentos['fuentes'])} · "
        f"periodos: {len(documentos['catalogo']['periodos'])} · "
        f"combinaciones: {len(documentos['catalogo']['combinaciones'])}"
    )

    ajustes = get_settings()
    cliente = MongoClient(ajustes.mongodb_uri, serverSelectionTimeoutMS=15_000, compressors="zlib")
    db = cliente[ajustes.mongodb_db]

    antes = _tamano(db, [HECHOS, CATALOGO, FUENTES, IMPORTACIONES, ESTADO])
    cluster_antes = _clusters(cliente)
    print("tamaño antes:", antes["total_mib"], "MiB · clúster:", cluster_antes["total_mib"], "MiB")

    if not opciones.aplicar:
        print("\nModo simulación: usa --aplicar para escribir en Atlas.")
        cliente.close()
        return 0

    # Se escribe la versión nueva en colecciones operativas y se retira la anterior
    # al final (el puntero cambia en una sola operación).
    for coleccion in (HECHOS, CATALOGO, FUENTES, IMPORTACIONES):
        db[coleccion].delete_many({"version": {"$ne": version}})

    pico = 0.0
    db[HECHOS].insert_many(documentos["hechos"])
    pico = max(pico, _tamano(db, [HECHOS])["total_mib"])
    db[CATALOGO].delete_many({})
    db[CATALOGO].insert_one(documentos["catalogo"])
    db[FUENTES].delete_many({})
    db[FUENTES].insert_many(documentos["fuentes"])
    db[IMPORTACIONES].delete_many({})
    db[IMPORTACIONES].insert_one(documentos["importacion"])
    pico = max(pico, _tamano(db, [HECHOS, CATALOGO, FUENTES, IMPORTACIONES])["total_mib"])

    db[ESTADO].replace_one(
        {"_id": "activo"},
        {
            "_id": "activo",
            "version": version,
            "publicado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hechos": len(documentos["hechos"]),
            "ultimo_periodo": documentos["catalogo"]["ultimo_periodo"],
        },
        upsert=True,
    )

    despues = _tamano(db, [HECHOS, CATALOGO, FUENTES, IMPORTACIONES, ESTADO])
    cluster_despues = _clusters(cliente)
    print("\ntamaño después:", json.dumps(despues, ensure_ascii=False, indent=1))
    print(f"pico durante la publicación: {round(pico, 4)} MiB")
    print(
        f"clúster: {cluster_antes['total_mib']} → {cluster_despues['total_mib']} MiB · "
        f"margen {cluster_despues['margen_mib']} MiB"
    )
    print(f"presupuesto del tablero: {PRESUPUESTO_MIB} MiB · usado {despues['total_mib']} MiB")

    # Comprobaciones posteriores a la publicación.
    total = db[HECHOS].count_documents({"version": version})
    print("hechos publicados:", total)
    for mercado in ("mexico", "centroamerica", "consolidado"):
        suma = list(
            db[HECHOS].aggregate(
                [
                    {
                        "$match": {
                            "version": version,
                            "mercado": mercado,
                            "indicador": "total_ingresos",
                            "periodo": ultimo,
                        }
                    },
                    {"$group": {"_id": None, "valor": {"$sum": "$valor"}}},
                ]
            )
        )
        print(f"  {ultimo} {mercado}: {suma[0]['valor'] if suma else 'sin dato'}")
    cliente.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
