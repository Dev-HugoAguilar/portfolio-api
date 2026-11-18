"""Deja la revisión decimal lista para regenerarse: la desactiva y borra el marcador.

Se usa solo en el mantenimiento de la publicación (no forma parte de la API). Al borrar el
marcador, ``republicar_precision`` vuelve a construir la revisión y la valida antes de activarla.
"""

from app.config import get_settings
from app.db.mongo import (
    M5_EVALUACION_SERIES,
    M5_PLANIFICACION,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PUBLICACION,
    init_database,
)

REVISION = "rev-ec9b21cd45"


def main() -> int:
    db = init_database(get_settings()).m5_db
    anterior = db[M5_PUBLICACION].find_one({"_id": REVISION})
    if anterior:
        db[M5_PUBLICACION].update_one(
            {"_id": REVISION}, {"$set": {"activa": False, "estado": "revalidando"}}
        )
    for coleccion in (M5_PREDICCIONES, M5_PREDICCIONES_SERIES, M5_PLANIFICACION, M5_EVALUACION_SERIES):
        borrados = db[coleccion].delete_many({"revision": REVISION}).deleted_count
        print(f"{coleccion}: {borrados} documentos de {REVISION} borrados")
    db[M5_PUBLICACION].delete_one({"_id": REVISION})
    print("Revisión desactivada y marcador eliminado: se regenerará y validará de nuevo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
