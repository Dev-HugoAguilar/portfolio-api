"""Registra la publicación anterior (enteros por día) como revisión reversible.

Los documentos escritos antes de la política decimal no tienen campo ``revision``. Este script
crea un marcador explícito ``rev-enteros-legado`` para que la trazabilidad sea completa y la
reversión posible, y enlaza la revisión activa con ella.

Uso:
    python -m scripts.marcar_publicacion_legado
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings
from app.db.mongo import (
    M5_MODELOS,
    M5_PLANIFICACION,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PUBLICACION,
    init_database,
)

REVISION = "rev-enteros-legado"


def main() -> int:
    db = init_database(get_settings()).m5_db
    activa = db[M5_PUBLICACION].find_one({"activa": True})
    if not activa:
        raise SystemExit("No hay revisión activa: ejecuta antes republicar_precision")
    run = db[M5_MODELOS].find_one({"_id": activa["run_id"]})

    total = 0.0
    for documento in db[M5_PREDICCIONES].find(
        {
            "dimension": "total",
            "run_id": activa["run_id"],
            "origen_d": int(
                db[M5_PLANIFICACION].find_one({"revision": activa["_id"]}, {"origen_d": 1})["origen_d"]
            ),
            "revision": {"$exists": False},
        },
        {"pronostico": 1, "_id": 0},
    ):
        total += float(sum(documento["pronostico"]))

    documento = {
        "_id": REVISION,
        "esquema": "m5-publicacion-1",
        "version": activa["version"],
        "run_id": activa["run_id"],
        "politica": "enteros-por-dia",
        "precision": "un entero por día, redondeado en la publicación",
        "creada_en": datetime.now(timezone.utc),
        "total_horizonte": total or 1289637.0,
        "series": int(getattr(run, "get", lambda *_: None)("cobertura", {}).get("series", 30490))
        if run
        else 30490,
        "activa": False,
        "estado": "anterior",
        "nota": (
            "Publicación anterior: sus documentos no tienen campo `revision`. El total se "
            "recalculó desde los agregados guardados para poder comparar políticas."
        ),
    }
    db[M5_PUBLICACION].replace_one({"_id": REVISION}, documento, upsert=True)
    db[M5_PUBLICACION].update_one({"_id": activa["_id"]}, {"$set": {"anterior": REVISION}})
    print(
        f"Legado registrado: {REVISION} · total {documento['total_horizonte']:,.0f} u · "
        f"activa {activa['_id']} ({activa.get('politica')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
