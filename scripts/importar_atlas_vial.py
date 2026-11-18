"""Importación manual del Atlas vial (SSC · Ciudad de México).

Uso:
    python -m scripts.importar_atlas_vial                # descarga y publica
    python -m scripts.importar_atlas_vial --forzar       # republica aunque no cambie
    python -m scripts.importar_atlas_vial --desde-local D:\\Descargas --sin-red
    python -m scripts.importar_atlas_vial --estado       # solo consulta el estado

El procedimiento es el mismo que usa la sincronización programada
(`scripts.sincronizar_atlas_vial`): resolver el catálogo oficial, descargar con
validación, procesar en una zona temporal y publicar de forma atómica. Si algo
falla, la versión publicada anterior sigue siendo la que sirve la API.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app.atlas import parser as atlas_parser
from app.atlas import sources
from app.atlas.geometry import obtener_geometria
from app.atlas.importer import estado_publico, sincronizar
from app.config import get_settings
from app.db.mongo import init_database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _estado(settings) -> None:
    base = init_database(settings)
    print(json.dumps(estado_publico(base.db), ensure_ascii=False, indent=2, default=str))


def _importar_local(origen: Path, settings, forzar: bool) -> int:
    """Importa los cinco CSV ya descargados (sin red), útil para auditar."""
    carpeta = settings.atlas_dir() / "versiones" / "local"
    carpeta.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for clave, definicion in sources.RECURSOS.items():
        archivo = origen / f"{definicion['uid']}.csv"
        if not archivo.exists():
            print(f"Falta el archivo {archivo}", file=sys.stderr)
            return 2
        destino = carpeta / f"{clave}.csv"
        destino.write_bytes(archivo.read_bytes())
        hashes[clave] = sources.sha256_de(destino)
    print("Archivos copiados a", carpeta)
    print(json.dumps(hashes, indent=2))
    print(
        "Nota: la importación local solo audita los archivos; la publicación usa "
        "siempre el catálogo oficial (scripts.importar_atlas_vial)."
    )
    tablas = {clave: atlas_parser.leer_tabla(carpeta / f"{clave}.csv", clave) for clave in sources.RECURSOS}
    hechos, incidencias = atlas_parser.construir_hechos(tablas, obtener_geometria())
    cifras = atlas_parser.resumen_legible(hechos, incidencias)
    print(json.dumps({"cifras": cifras, "incidencias": incidencias}, ensure_ascii=False, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Importación del Atlas vial")
    parser.add_argument("--forzar", action="store_true", help="Republica aunque la huella no cambie.")
    parser.add_argument("--estado", action="store_true", help="Solo muestra el estado actual.")
    parser.add_argument(
        "--desde-local",
        type=Path,
        help="Carpeta con los cinco CSV ya descargados (auditoría sin publicar).",
    )
    argumentos = parser.parse_args()
    settings = get_settings()

    if argumentos.estado:
        _estado(settings)
        return 0

    if argumentos.desde_local:
        return _importar_local(argumentos.desde_local, settings, argumentos.forzar)

    base = init_database(settings)
    resultado = sincronizar(base.db, forzar=argumentos.forzar)
    print(json.dumps(resultado.publico(), ensure_ascii=False, indent=2, default=str))
    return 0 if resultado.exito else 1


if __name__ == "__main__":
    raise SystemExit(main())
