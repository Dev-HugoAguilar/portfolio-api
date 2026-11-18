"""Reinicia únicamente las colecciones CNBV de la base de datos actual.

Se usa solo para volver a importar desde cero (por ejemplo, tras corregir un
error del importador). Nunca toca colecciones de otros proyectos ni volúmenes:
solo vacía las colecciones ``cnbv_*`` de este dataset.
"""

from __future__ import annotations

import sys

from app.db.mongo import (
    CNBV_BOLETINES,
    CNBV_HECHOS,
    CNBV_INDICADORES,
    close_database,
    init_database,
)
from app.config import get_settings

COLECCIONES = (CNBV_BOLETINES, CNBV_HECHOS, CNBV_INDICADORES)


def main() -> int:
    settings = get_settings()
    db = init_database(settings)
    try:
        if not db.ping():
            print(f"[ERROR] MongoDB no responde en {settings.safe_mongo_host}")
            return 1
        print(f"Base de datos: {settings.mongodb_db}")
        for nombre in COLECCIONES:
            borrados = db.db[nombre].delete_many({}).deleted_count
            print(f"  {nombre}: {borrados} documentos eliminados")
        return 0
    finally:
        close_database()


if __name__ == "__main__":
    sys.exit(main())
