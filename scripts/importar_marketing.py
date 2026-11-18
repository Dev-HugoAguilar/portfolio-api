"""Preparación del conjunto GA4 de marketing (BigQuery → copia original → Atlas).

La extracción es una tarea de **preparación**: cuando ya existe una versión válida
en MongoDB (Atlas), el tablero funciona sin BigQuery y sin credenciales de Google.

Uso:
    python -m scripts.importar_marketing                 # extrae y publica
    python -m scripts.importar_marketing --extraer       # solo extrae la copia original
    python -m scripts.importar_marketing --forzar        # republica aunque no cambie
    python -m scripts.importar_marketing --estado        # estado de la versión activa
    python -m scripts.importar_marketing --diagnostico   # comprueba el acceso a BigQuery
    python -m scripts.importar_marketing --desde-local RUTA
        # carga una copia original ya extraída (no consulta BigQuery)
    python -m scripts.importar_marketing --carpeta RUTA  # carpeta destino de la extracción

Códigos de salida: 0 éxito · 1 fallo de validación · 3 sin acceso a BigQuery.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app.config import get_settings
from app.db.mongo import init_database
from app.marketing import cliente as acceso
from app.marketing.ingesta import estado_publico, sincronizar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SALIDA_OK = 0
SALIDA_FALLO = 1
SALIDA_BLOQUEADO = 3


def _estado(settings) -> int:
    base = init_database(settings)
    print(json.dumps(estado_publico(base.marketing_db), ensure_ascii=False, indent=2, default=str))
    return SALIDA_OK


def _diagnostico(settings) -> int:
    estado = acceso.diagnosticar(settings.marketing_gcp_project, refrescar=True)
    if estado.disponible:
        print(f"BigQuery accesible por: {estado.via}")
        return SALIDA_OK
    print("BigQuery NO accesible en este entorno (la API no lo necesita).")
    print(f"  motivo: {estado.motivo}")
    for linea in (estado.detalle or {}).get("intentos", []):
        print(f"  intento: {linea}")
    print("  para habilitar la extracción:")
    for comando in acceso.REMEDIO:
        print(f"    {comando}")
    return SALIDA_BLOQUEADO


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Extracción del conjunto GA4 de marketing")
    argumentos.add_argument("--forzar", action="store_true", help="Republica aunque no cambie.")
    argumentos.add_argument("--estado", action="store_true", help="Solo muestra el estado.")
    argumentos.add_argument(
        "--diagnostico", action="store_true", help="Comprueba si hay acceso a BigQuery."
    )
    argumentos.add_argument(
        "--extraer",
        action="store_true",
        help="Solo extrae la copia original; no escribe la versión publicada.",
    )
    argumentos.add_argument(
        "--muestra",
        type=float,
        default=None,
        help="Porcentaje de usuarios a muestrear (determinista; 0 = completo).",
    )
    argumentos.add_argument(
        "--semilla",
        type=str,
        default=None,
        help="Semilla documentada del muestreo (por omisión, la de la configuración).",
    )
    argumentos.add_argument(
        "--reanudar",
        action="store_true",
        help="No vuelve a extraer las entidades que ya tienen copia original.",
    )
    argumentos.add_argument(
        "--desde-local",
        type=Path,
        help="Carpeta con una copia original ya extraída (no consulta BigQuery).",
    )
    argumentos.add_argument(
        "--carpeta",
        type=Path,
        help="Carpeta destino de la extracción (por omisión, data/marketing/extraccion-…).",
    )
    opciones = argumentos.parse_args()
    settings = get_settings()

    if opciones.estado:
        return _estado(settings)
    if opciones.diagnostico:
        return _diagnostico(settings)

    base = init_database(settings)
    resultado = sincronizar(
        base.marketing_db,
        forzar=opciones.forzar,
        desde_local=opciones.desde_local,
        carpeta=opciones.carpeta,
        solo_extraer=opciones.extraer,
        reanudar=opciones.reanudar,
        muestra_porcentaje=opciones.muestra,
        muestra_semilla=opciones.semilla,
    )
    print(json.dumps(resultado.publico(), ensure_ascii=False, indent=2, default=str))

    if resultado.exito:
        return SALIDA_OK
    if "BigQuery" in (resultado.motivo or ""):
        print(
            "\nNo se publicó nada. La aplicación sigue sirviendo la versión que ya "
            "estuviera publicada en MongoDB.",
            file=sys.stderr,
        )
        return SALIDA_BLOQUEADO
    return SALIDA_FALLO


if __name__ == "__main__":
    raise SystemExit(main())

