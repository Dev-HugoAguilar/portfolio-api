"""Inventario de almacenamiento y preparación de la exportación compacta.

Se ejecuta **antes** de pensar en publicar en MongoDB Atlas: mide lo que hay y deja
un paquete pequeño con lo que las vistas públicas necesitan. No migra nada y no
se conecta a Atlas.

Paquete compacto (NDJSON + un manifiesto):

- ``catalogo``    tiendas, estados, categorías, departamentos y productos.
- ``agregados``   vectores diarios por dimensión (series y comparación A/B).
- ``pronosticos`` pronóstico final y backtesting de la versión elegida.
- ``evaluacion``  métricas de la evaluación fuera de muestra.
- ``metadatos``   cobertura, versiones y conteos.

Los bloques **producto × tienda** no se exportan por omisión: la concentración
pública se sirve con los agregados y, si se necesita el detalle, se decide
entonces. El detalle completo (millones de números) no viaja a Atlas.
"""

from __future__ import annotations

import json
from pathlib import Path

from pymongo.database import Database

from app.db.mongo import (
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_CALENDARIO,
    M5_CATALOGO,
    M5_DERIVADOS,
    M5_ESTADO,
    M5_EVALUACION,
    M5_IMPORTACIONES,
    M5_PRONOSTICOS,
)

COLECCIONES_PUBLICABLES = (M5_CATALOGO, M5_AGREGADOS, M5_PRONOSTICOS, M5_EVALUACION, M5_CALENDARIO)
"""Colecciones que necesitarían las vistas públicas en una futura migración."""

COLECCIONES_DETALLE = (M5_BLOQUES,)
"""Detalle producto × tienda: se queda en local salvo necesidad demostrada."""


def _stats(db: Database, nombre: str) -> dict:
    try:
        datos = db.command("collStats", nombre)
    except Exception:  # pragma: no cover - colección inexistente
        return {"coleccion": nombre, "documentos": 0, "bytes_datos": 0, "bytes_almacenamiento": 0,
                "bytes_indices": 0, "indices": 0}
    return {
        "coleccion": nombre,
        "documentos": datos.get("count", 0),
        "bytes_datos": datos.get("size", 0),
        "bytes_almacenamiento": datos.get("storageSize", 0),
        "bytes_indices": datos.get("totalIndexSize", 0),
        "indices": datos.get("nindexes", 0),
    }


def inventario(db: Database) -> dict:
    """Documentos, tamaño de datos, de almacenamiento e índices por colección."""
    colecciones = (
        M5_IMPORTACIONES,
        M5_ESTADO,
        M5_DERIVADOS,
        *COLECCIONES_PUBLICABLES,
        *COLECCIONES_DETALLE,
    )
    filas = [_stats(db, nombre) for nombre in dict.fromkeys(colecciones)]
    publicables = [f for f in filas if f["coleccion"] in COLECCIONES_PUBLICABLES]
    detalle = [f for f in filas if f["coleccion"] in COLECCIONES_DETALLE]
    return {
        "colecciones": filas,
        "total": {
            "documentos": sum(f["documentos"] for f in filas),
            "bytes_datos": sum(f["bytes_datos"] for f in filas),
            "bytes_almacenamiento": sum(f["bytes_almacenamiento"] for f in filas),
            "bytes_indices": sum(f["bytes_indices"] for f in filas),
        },
        "publicacion_prevista": {
            "colecciones": [f["coleccion"] for f in publicables],
            "documentos": sum(f["documentos"] for f in publicables),
            "bytes_almacenamiento": sum(f["bytes_almacenamiento"] for f in publicables),
        },
        "solo_local": {
            "colecciones": [f["coleccion"] for f in detalle],
            "documentos": sum(f["documentos"] for f in detalle),
            "bytes_almacenamiento": sum(f["bytes_almacenamiento"] for f in detalle),
        },
        "nota": (
            "Antes de una migración se comparará el tamaño real con el límite vigente "
            "del plan de Atlas. Este informe no migra ni publica nada."
        ),
    }


def exportacion_compacta(db: Database, version: str, carpeta: Path, *, limite_pronosticos: int = 200) -> dict:
    """Escribe el paquete NDJSON de una versión y devuelve el manifiesto."""
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    manifiesto_importacion = db[M5_IMPORTACIONES].find_one({"_id": version}) or {}
    puntero = db[M5_ESTADO].find_one({"_id": "actual"}) or {}

    def _volcar(nombre: str, coleccion: str, consulta: dict, limite: int | None = None) -> dict:
        ruta = carpeta / f"{nombre}.ndjson"
        documentos = 0
        with ruta.open("w", encoding="utf-8") as manejador:
            cursor = db[coleccion].find(consulta, {"_id": 0})
            if limite:
                cursor = cursor.limit(limite)
            for documento in cursor:
                manejador.write(json.dumps(documento, ensure_ascii=False, default=str) + "\n")
                documentos += 1
        return {"archivo": ruta.name, "documentos": documentos, "bytes": ruta.stat().st_size}

    archivos = [
        _volcar("catalogo", M5_CATALOGO, {"version": version}),
        _volcar("agregados", M5_AGREGADOS, {"version": version}),
        _volcar("pronosticos", M5_PRONOSTICOS, {"version": version}, limite_pronosticos),
        _volcar("evaluacion", M5_EVALUACION, {"version": version}),
        _volcar("calendario", M5_CALENDARIO, {}),
    ]
    metadatos = {
        "version": version,
        "es_version_publicada": puntero.get("version") == version,
        "cobertura": manifiesto_importacion.get("cobertura"),
        "conteos": manifiesto_importacion.get("conteos"),
        "esquema": manifiesto_importacion.get("esquema"),
        "unidad": manifiesto_importacion.get("unidad"),
        "fuente": manifiesto_importacion.get("fuente"),
        "capacidades": {
            "agregaciones": ["dia", "semana", "mes"],
            "niveles_pronostico": ["total", "tienda", "categoria"],
            "filtros": ["tiendas", "categorias", "departamentos", "productos", "desde", "hasta"],
        },
        "no_incluye": ["m5_bloques (detalle producto × tienda)"],
    }
    ruta_metadatos = carpeta / "metadatos.json"
    ruta_metadatos.write_text(json.dumps(metadatos, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    archivos.append({"archivo": ruta_metadatos.name, "documentos": 1, "bytes": ruta_metadatos.stat().st_size})
    return {
        "carpeta": str(carpeta),
        "version": version,
        "archivos": archivos,
        "bytes_totales": sum(a["bytes"] for a in archivos),
    }
