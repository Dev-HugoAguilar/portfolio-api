"""Importación del conjunto Walmart M5 a MongoDB local.

Los CSV **no** se versionan en el repositorio. Descárgalos de la competición:

    https://www.kaggle.com/competitions/m5-forecasting-accuracy/data

y colócalos en ``backend/data/m5`` (o en la carpeta que indique
``COMERCIAL_DATA_DIR``):

    sales_train_evaluation.csv    obligatorio (o sales_train_validation.csv)
    calendar.csv                  obligatorio
    sell_prices.csv               opcional; no se usa en esta entrega

Uso:
    python -m scripts.importar_m5                  # valida, importa y publica
    python -m scripts.importar_m5 --validar        # solo comprueba archivos y hashes
    python -m scripts.importar_m5 --estado         # versión publicada y su cobertura
    python -m scripts.importar_m5 --forzar         # reimporta aunque ya exista
    python -m scripts.importar_m5 --carpeta RUTA   # carpeta alternativa de CSV
    python -m scripts.importar_m5 --inventario     # tamaño de datos e índices
    python -m scripts.importar_m5 --exportar RUTA  # paquete compacto para una futura migración

Códigos de salida: 0 éxito · 1 validación fallida · 3 archivos no disponibles.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app.comercial import fuentes
from app.comercial import ingesta
from app.comercial.inventario import exportacion_compacta, inventario
from app.config import get_settings
from app.db.mongo import init_database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OK = 0
FALLO = 1
SIN_ARCHIVOS = 3


def _json(datos) -> None:
    print(json.dumps(datos, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Importa Walmart M5 en MongoDB local.")
    parser.add_argument("--carpeta", type=Path, default=None, help="Carpeta con los CSV de M5.")
    parser.add_argument("--validar", action="store_true", help="Solo valida archivos y hashes.")
    parser.add_argument("--estado", action="store_true", help="Muestra la versión publicada.")
    parser.add_argument("--forzar", action="store_true", help="Reimporta aunque la versión exista.")
    parser.add_argument("--inventario", action="store_true", help="Mide documentos, datos e índices.")
    parser.add_argument("--exportar", type=Path, default=None, help="Carpeta del paquete compacto.")
    parser.add_argument(
        "--limite-filas",
        type=int,
        default=None,
        help="Solo pruebas: importa las primeras N series y no publica.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    carpeta = args.carpeta or settings.comercial_dir()
    base = init_database(settings)

    if args.estado:
        _json(ingesta.estado_publico(base.m5_db))
        return OK

    if args.inventario:
        _json(inventario(base.m5_db))
        return OK

    if args.exportar:
        version = ingesta.version_actual(base.m5_db)
        if not version:
            _json({"estado": "pendiente", "mensaje": "Datos pendientes de importar"})
            return SIN_ARCHIVOS
        _json(exportacion_compacta(base.m5_db, version, args.exportar))
        return OK

    try:
        informacion = ingesta.validar_archivos(carpeta)
    except ingesta.ErrorImportacion as exc:
        _json(
            {
                "estado": "pendiente",
                "mensaje": "Datos pendientes de importar",
                "detalle": str(exc),
                "archivos_esperados": [a.nombre for a in fuentes.archivos_esperados()],
                "carpeta": str(carpeta),
                "accion": "Descarga los CSV en esa carpeta y vuelve a ejecutar el comando.",
            }
        )
        return SIN_ARCHIVOS

    if args.validar:
        _json(informacion)
        return OK

    try:
        manifiesto = ingesta.importar(
            base.m5_db,
            carpeta,
            forzar=args.forzar,
            limite_filas=args.limite_filas,
            publicar=args.limite_filas is None,
        )
    except ingesta.ErrorImportacion as exc:
        _json({"estado": "fallo", "detalle": str(exc)})
        return FALLO

    _json(
        {
            "version": manifiesto["version"],
            "estado": manifiesto["estado"],
            "cobertura": manifiesto["cobertura"],
            "conteos": manifiesto["conteos"],
            "validaciones": manifiesto["validaciones"],
            "carpeta": manifiesto["carpeta_origen"],
        }
    )
    return OK


if __name__ == "__main__":
    sys.exit(main())
