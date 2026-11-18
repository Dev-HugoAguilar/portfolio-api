"""Sincronización programada del Atlas vial (SSC · Ciudad de México).

Este es el mismo proceso que ejecuta la importación manual
(`scripts.importar_atlas_vial`), envuelto para un programador de tareas:

* escribe un registro rotativo en ``backend/logs/atlas-vial.log``;
* devuelve códigos de salida que el programador puede interpretar;
* nunca deja una versión inválida publicada: el importador solo cambia el
  puntero ``atlas_estado`` cuando los cinco archivos pasan la validación.

Uso:
    python -m scripts.sincronizar_atlas_vial
    python -m scripts.sincronizar_atlas_vial --forzar

Códigos de salida:
    0  versión publicada o contenido sin cambios (nada que hacer)
    1  fallo (catálogo inaccesible, descarga incompleta o validación fallida);
       la versión anterior sigue publicada
    2  ya había una sincronización en curso; no se ejecutó una segunda
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from app.atlas.importer import sincronizar
from app.config import BACKEND_DIR, get_settings
from app.db.mongo import init_database

RUTA_REGISTRO = BACKEND_DIR / "logs" / "atlas-vial.log"

SALIDA_OK = 0
SALIDA_FALLO = 1
SALIDA_BLOQUEADO = 2


def _configurar_registro() -> logging.Logger:
    RUTA_REGISTRO.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("atlas.sincronizacion")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        manejador = RotatingFileHandler(
            RUTA_REGISTRO,
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        manejador.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(manejador)
        consola = logging.StreamHandler(sys.stdout)
        consola.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(consola)
    return logger


def main() -> int:
    argumentos = argparse.ArgumentParser(
        description="Sincronización programada del Atlas vial"
    )
    argumentos.add_argument(
        "--forzar",
        action="store_true",
        help="Republica aunque la huella del contenido no cambie.",
    )
    opciones = argumentos.parse_args()

    logger = _configurar_registro()
    ajustes = get_settings()
    logger.info(
        "Inicio de sincronización (forzar=%s, dataset=%s, datos=%s)",
        opciones.forzar,
        ajustes.atlas_dataset_id,
        ajustes.atlas_data_dir,
    )

    base = init_database(ajustes)
    try:
        resultado = sincronizar(base.db, forzar=opciones.forzar)
    except Exception:  # noqa: BLE001 - el registro debe guardar el fallo completo
        logger.exception("La sincronización terminó con una excepción")
        return SALIDA_FALLO
    finally:
        from app.db.mongo import close_database

        close_database()

    resumen = resultado.publico()
    logger.info("Resultado: %s", json.dumps(resumen, ensure_ascii=False, default=str))

    if resultado.exito:
        if resultado.sin_cambios:
            logger.info("Sin cambios: se conserva la versión %s", resultado.version)
        else:
            logger.info("Versión publicada: %s", resultado.version)
        return SALIDA_OK

    if "en curso" in (resultado.motivo or "") or "bloqueo" in (resultado.motivo or "").lower():
        logger.warning("No se ejecutó: %s", resultado.motivo)
        return SALIDA_BLOQUEADO

    logger.error("Fallo de sincronización: %s", resultado.motivo)
    return SALIDA_FALLO


if __name__ == "__main__":
    raise SystemExit(main())
