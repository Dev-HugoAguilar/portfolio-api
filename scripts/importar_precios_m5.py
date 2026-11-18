"""Importa ``sell_prices.csv`` (M5) a MongoDB local, de forma versionada e idempotente.

Uso:
    python -m scripts.importar_precios_m5 --validar     # solo valida, no escribe
    python -m scripts.importar_precios_m5               # valida, importa e informa
    python -m scripts.importar_precios_m5 --limite 500000  # piloto acotado

Códigos de salida: 0 éxito · 1 validación fallida · 3 archivo no disponible.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app.comercial import ingesta, precios
from app.config import get_settings
from app.db.mongo import init_database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OK = 0
FALLO = 1
SIN_ARCHIVO = 3


def _json(datos) -> None:
    print(json.dumps(datos, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Importa los precios de M5.")
    parser.add_argument("--carpeta", type=Path, default=None, help="Carpeta con sell_prices.csv.")
    parser.add_argument("--validar", action="store_true", help="Solo valida; no escribe en MongoDB.")
    parser.add_argument("--limite", type=int, default=None, help="Solo las primeras N filas (piloto).")
    args = parser.parse_args(argv)

    settings = get_settings()
    carpeta = args.carpeta or settings.comercial_dir()
    base = init_database(settings)
    db = base.m5_db
    version = ingesta.version_actual(db)
    if not version:
        _json({"estado": "pendiente", "mensaje": "Datos de ventas pendientes de importar"})
        return SIN_ARCHIVO

    try:
        datos = precios.leer_precios(carpeta, limite=args.limite)
    except precios.ErrorPrecios as exc:
        _json({"estado": "sin_archivo", "detalle": str(exc), "carpeta": str(carpeta)})
        return SIN_ARCHIVO

    if args.validar:
        try:
            validaciones = precios.validar_precios(db, version, datos)
        except precios.ErrorPrecios as exc:
            _json({"estado": "fallo", "detalle": str(exc)})
            return FALLO
        _json(
            {
                "validado": True,
                "version_ventas": version,
                "archivo": datos["archivo"],
                "hash": datos["hash"],
                "validaciones": validaciones,
            }
        )
        return OK

    try:
        manifiesto = precios.importar_precios(db, version, carpeta)
    except precios.ErrorPrecios as exc:
        _json({"estado": "fallo", "detalle": str(exc)})
        return FALLO

    _json(
        {
            "version_precios": manifiesto["version_precios"],
            "version_ventas": manifiesto["version_ventas"],
            "reutilizado": manifiesto.get("reutilizado", False),
            "archivo": manifiesto["archivo"],
            "hash": manifiesto["hash"],
            "validaciones": manifiesto["validaciones"],
            "documentos": manifiesto.get("documentos"),
            "almacenamiento": manifiesto.get("almacenamiento"),
        }
    )
    return OK


if __name__ == "__main__":
    sys.exit(main())
