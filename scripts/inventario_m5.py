"""Inventario de almacenamiento y paquete compacto del Atlas comercial (M5).

No migra ni publica nada: mide el tamaño real para poder decidir después si cabe
en el plan de MongoDB Atlas.

Uso:
    python -m scripts.inventario_m5
    python -m scripts.inventario_m5 --exportar exportacion/m5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.comercial import ingesta
from app.comercial.inventario import exportacion_compacta, inventario
from app.config import get_settings
from app.db.mongo import init_database

OK = 0
SIN_DATOS = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mide el almacenamiento del proyecto M5.")
    parser.add_argument("--exportar", type=Path, default=None, help="Carpeta del paquete compacto.")
    args = parser.parse_args(argv)

    settings = get_settings()
    base = init_database(settings)
    print(json.dumps(inventario(base.m5_db), ensure_ascii=False, indent=2, default=str))

    if args.exportar:
        version = ingesta.version_actual(base.m5_db)
        if not version:
            print(json.dumps({"estado": "pendiente", "mensaje": "Datos pendientes de importar"}, ensure_ascii=False))
            return SIN_DATOS
        print(
            json.dumps(
                exportacion_compacta(base.m5_db, version, args.exportar),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    return OK


if __name__ == "__main__":
    sys.exit(main())
