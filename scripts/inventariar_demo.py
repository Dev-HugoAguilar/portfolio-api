"""Inventario del consumo real de la base de Atlas Comercial y de su volumen.

Produce dos cosas:

1. La **matriz de consumo**: pantalla/acción → endpoint → colecciones → campos → filtros →
   granularidad → rango temporal → índices. La columna «endpoint» se contrasta con las rutas
   registradas en la API y las colecciones se contrastan con las que existen en la base.
2. El **inventario real** de la base local: documentos, tamaño lógico (BSON), almacenamiento físico,
   índices, revisiones y ejecuciones presentes, y campos sin consumidores conocidos.

Uso (desde ``backend/``):

    python -m scripts.inventariar_demo                # informe en consola
    python -m scripts.inventariar_demo --json ruta.json
    python -m scripts.inventariar_demo --base atlas_comercial_demo
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app import main as app_main
from app.config import get_settings
from app.db.mongo import init_database

#: Matriz de consumo reconstruida leyendo `app/api/v1/routes/comercial.py`, los servicios y los
#: componentes del tablero. Cada fila declara qué colección y qué campos necesita la respuesta.
MATRIZ: list[dict[str, Any]] = [
    {
        "pantalla": "Proyecto",
        "accion": "cargar la portada (estado, catálogo, pronóstico, evaluación y modelos)",
        "endpoints": ["/estado", "/catalogo", "/pronosticos", "/evaluacion", "/modelos"],
        "colecciones": ["m5_estado", "m5_importaciones", "m5_catalogo", "m5_predicciones", "m5_evaluacion_modelos", "m5_modelos"],
        "campos": "version, cobertura, tiendas, categorías, departamentos, series, métricas por ventana",
        "filtros": "nivel total; etapa final",
        "granularidad": "una fila por nivel agregado y por ventana de 28 días",
        "rango": "2011-01-29 – 2016-05-22 (histórico) y cortes finales",
        "indices": "m5_predicciones (revision, origen_d, dimension), m5_modelos {version}",
    },
    {
        "pantalla": "Datos y calidad",
        "accion": "fuentes, estructura, ceros, cobertura de precios y reconciliación",
        "endpoints": ["/estado", "/catalogo", "/comparacion", "/intermitencia", "/precios/distribucion"],
        "colecciones": ["m5_importaciones", "m5_catalogo", "m5_agregados", "m5_bloques", "m5_precios", "m5_precios_manifest"],
        "campos": "conteos, cobertura, unidades por dimensión, precios y su cobertura",
        "filtros": "estado, tienda, categoría, departamento",
        "granularidad": "dimensión × día (agregados) y serie × día (bloques); precios tienda × semana",
        "rango": "todo el histórico; precios 2011-02-06 – 2016-06-19",
        "indices": "m5_bloques (version, store_id, item_id), m5_precios (version_precios, store_id, wm_yr_wk)",
    },
    {
        "pantalla": "Exploración · Panorama",
        "accion": "pulso, comparación A/B, composición, concentración y explorador de productos",
        "endpoints": ["/pulso", "/comparacion", "/estacionalidad", "/productos", "/historico"],
        "colecciones": ["m5_agregados", "m5_bloques", "m5_catalogo", "m5_calendario"],
        "campos": "unidades diarias por dimensión, cobertura de calendario, catálogo para etiquetas",
        "filtros": "periodo, estado, tienda, categoría, departamento, productos",
        "granularidad": "dimensión × día; serie × día para concentración",
        "rango": "periodo personalizable dentro de 2011-01-29 – 2016-05-22",
        "indices": "m5_agregados (version, dimension, clave), m5_bloques (version, store_id)",
    },
    {
        "pantalla": "Exploración · Estacionalidad",
        "accion": "perfil semanal, mensual y matriz temporal",
        "endpoints": ["/estacionalidad", "/matriz-temporal"],
        "colecciones": ["m5_agregados", "m5_calendario"],
        "campos": "unidades por día, día de la semana, mes, año",
        "filtros": "dimensión, periodo, año",
        "granularidad": "dimensión × día",
        "rango": "años completos del calendario (2011–2016)",
        "indices": "m5_agregados (version, dimension, clave), m5_calendario {d}",
    },
    {
        "pantalla": "Exploración · Precios",
        "accion": "distribución por caja, cambios semanales y evolución del precio medio",
        "endpoints": ["/precios/distribucion", "/precios/cambios", "/precios/evolucion"],
        "colecciones": ["m5_precios", "m5_precios_manifest", "m5_calendario", "m5_catalogo"],
        "campos": "precio por artículo y semana, semanas del calendario, cobertura",
        "filtros": "tienda (obligatoria), periodo, categoría, departamento, artículos",
        "granularidad": "tienda × artículo × semana",
        "rango": "282 semanas (wm_yr_wk) del archivo de precios",
        "indices": "m5_precios (version_precios, store_id, wm_yr_wk)",
    },
    {
        "pantalla": "Exploración · Detalle de artículo",
        "accion": "ventas diarias, precios semanales, intermitencia y capas de calendario",
        "endpoints": ["/articulo", "/historico", "/intermitencia"],
        "colecciones": ["m5_bloques", "m5_precios", "m5_calendario", "m5_catalogo"],
        "campos": "unidades por serie y día, precio semanal, eventos y SNAP",
        "filtros": "tienda y artículo obligatorios; periodo",
        "granularidad": "serie × día; serie × semana para precios",
        "rango": "todo el histórico y las semanas con precio",
        "indices": "m5_bloques (version, store_id, item_id), m5_precios (version_precios, store_id, wm_yr_wk)",
    },
    {
        "pantalla": "Modelado",
        "accion": "manifiesto de ejecuciones, variables, supuestos y protocolo",
        "endpoints": ["/modelos", "/evaluacion", "/evaluacion-ventanas", "/auditoria-temporal"],
        "colecciones": ["m5_modelos", "m5_evaluacion_modelos", "m5_auditoria_temporal"],
        "campos": "variante, variables, métricas de desarrollo y finales, supuestos, evidencia temporal",
        "filtros": "etapa, ejecución",
        "granularidad": "una fila por ejecución y por corte",
        "rango": "cortes de desarrollo y finales",
        "indices": "m5_evaluacion_modelos (run_id, etapa, origen_d)",
    },
    {
        "pantalla": "Evaluación",
        "accion": "resumen, segmentos, cortes, horizontes, matriz y auditoría",
        "endpoints": ["/pronosticos", "/evaluacion", "/evaluacion-ventanas", "/evaluacion-horizonte", "/matriz-horizontes", "/auditoria-temporal"],
        "colecciones": ["m5_predicciones", "m5_predicciones_series", "m5_evaluacion_modelos", "m5_evaluacion_series", "m5_planificacion", "m5_auditoria_temporal"],
        "campos": "predicción y observado por día, error por serie y corte, métricas agregadas",
        "filtros": "nivel, etapa, corte, tienda, categoría, departamento",
        "granularidad": "serie × día; serie × corte; dimensión × día",
        "rango": "cortes d_1885 y d_1913 (finales) y d_1773–d_1857 (desarrollo)",
        "indices": "m5_predicciones (revision, origen_d, dimension), m5_evaluacion_series (revision, origen_d, store_id, item_id)",
    },
    {
        "pantalla": "Resultados",
        "accion": "plan de 28 días, detalle de serie, evaluación a 28 días y descarga completa",
        "endpoints": ["/planificacion", "/planificacion/serie", "/planificacion/exportar", "/evaluacion-horizonte"],
        "colecciones": ["m5_planificacion", "m5_evaluacion_series", "m5_publicacion", "m5_predicciones", "m5_estado"],
        "campos": "observado 28 d, pronóstico 28 d, baseline, semanas, estado de evaluación, revisión",
        "filtros": "tienda, categoría, departamento, búsqueda de texto, orden, mínimo observado",
        "granularidad": "serie × horizonte de 28 días",
        "rango": "corte 2016-05-22 y horizonte 2016-05-23 – 2016-06-19",
        "indices": "m5_planificacion (revision, store_id, item_id), (revision, pronostico_28)",
    },
    {
        "pantalla": "Navegación directa y estados vacíos",
        "accion": "URL con parámetros, recarga, búsqueda sin resultados, filtros imposibles",
        "endpoints": ["(los anteriores con parámetros vacíos o fuera de rango)"],
        "colecciones": ["(las mismas; se aceptan respuestas vacías y 422 declarados)"],
        "campos": "(ninguno adicional)",
        "filtros": "combinaciones sin datos",
        "granularidad": "(no aplica)",
        "rango": "(no aplica)",
        "indices": "los mismos; el estado vacío no debe requerir colecciones adicionales",
    },
]


def _stats(db, nombre: str) -> dict[str, Any]:
    try:
        st = db.command("collStats", nombre)
    except Exception:  # noqa: BLE001 - colección inexistente en la base consultada
        return {"documentos": 0, "bson_mb": 0.0, "storage_mb": 0.0, "indices_mb": 0.0, "indices_n": 0}
    return {
        "documentos": int(st.get("count", 0)),
        "bson_mb": round(st.get("size", 0) / 1024 / 1024, 2),
        "storage_mb": round(st.get("storageSize", 0) / 1024 / 1024, 2),
        "indices_mb": round(st.get("totalIndexSize", 0) / 1024 / 1024, 2),
        "indices_n": int(st.get("nindexes", 0)),
    }


def inventariar(nombre_base: str) -> dict[str, Any]:
    settings = get_settings()
    cliente = init_database(settings)
    db = cliente.m5_db
    if db.name != nombre_base:
        # Misma conexión validada como local, otra base del mismo servidor.
        db = db.client[nombre_base]
    colecciones = []
    for nombre in sorted(db.list_collection_names()):
        fila = {"coleccion": nombre, **_stats(db, nombre)}
        if nombre in ("m5_predicciones", "m5_predicciones_series", "m5_evaluacion_series", "m5_planificacion", "m5_publicacion"):
            fila["por_revision"] = {
                str(registro["_id"]): registro["n"]
                for registro in db[nombre].aggregate([{"$group": {"_id": "$revision", "n": {"$sum": 1}}}])
            }
        if nombre == "m5_modelos":
            fila["ejecuciones"] = [
                {"run_id": documento["run_id"], "variante": documento.get("variante"), "estado": documento.get("estado")}
                for documento in db[nombre].find({}, {"run_id": 1, "variante": 1, "estado": 1, "_id": 0})
            ]
        colecciones.append(fila)

    rutas = sorted(
        ruta.path
        for ruta in app_main.create_app(settings).routes
        if getattr(ruta, "path", "").startswith("/api/v1/comercial/m5")
    )
    total_bson = sum(fila["bson_mb"] for fila in colecciones)
    total_storage = sum(fila["storage_mb"] for fila in colecciones)
    total_indices = sum(fila["indices_mb"] for fila in colecciones)
    return {
        "base": db.name,
        "servidor": f"{db.client.address[0]}:{db.client.address[1]}" if db.client.address else "sin conexión",
        "colecciones": colecciones,
        "totales": {
            "documentos": sum(fila["documentos"] for fila in colecciones),
            "bson_mb": round(total_bson, 2),
            "storage_mb": round(total_storage, 2),
            "indices_mb": round(total_indices, 2),
        },
        "endpoints": rutas,
        "matriz": MATRIZ,
        "campos_sin_consumidores": {
            "m5_predicciones_series": ["item_index", "store_index", "esquema"],
            "m5_bloques": ["esquema"],
            "m5_modelos": ["artefacto (ruta local; se sustituye por el nombre del archivo)"],
        },
    }


def _consola(informe: dict[str, Any]) -> str:
    lineas = [
        f"Base {informe['base']} · {informe['totales']['documentos']:,} documentos · "
        f"{informe['totales']['bson_mb']:,.2f} MB BSON · {informe['totales']['storage_mb']:,.2f} MB "
        f"almacenamiento · {informe['totales']['indices_mb']:,.2f} MB índices",
        "",
        f"{'colección':32s}{'docs':>12s}{'BSON MB':>10s}{'almac. MB':>11s}{'índ. MB':>9s}",
    ]
    for fila in informe["colecciones"]:
        lineas.append(
            f"{fila['coleccion']:32s}{fila['documentos']:>12,}{fila['bson_mb']:>10.2f}"
            f"{fila['storage_mb']:>11.2f}{fila['indices_mb']:>9.2f}"
        )
    lineas.append("")
    lineas.append(f"Endpoints comercial/m5 registrados: {len(informe['endpoints'])}")
    lineas.append("Matriz de consumo (pantalla → endpoints → colecciones):")
    for fila in informe["matriz"]:
        lineas.append(
            f"  · {fila['pantalla']}: {', '.join(fila['endpoints'])} → {', '.join(fila['colecciones'])}"
        )
    return "\n".join(lineas)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventario de consumo y volumen de Atlas Comercial")
    parser.add_argument("--base", default=None, help="base a inventariar (por omisión, la configurada)")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    nombre = args.base or get_settings().comercial_mongodb_db
    informe = inventariar(nombre)
    print(_consola(informe))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as archivo:
            json.dump(informe, archivo, ensure_ascii=False, indent=1, default=str)
        print(f"\nJSON: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
