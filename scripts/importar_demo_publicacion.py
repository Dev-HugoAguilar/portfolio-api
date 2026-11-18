"""Importa el paquete de publicación en una base destino explícita.

Reglas de seguridad (a propósito, sin excepciones):

* la base destino es **obligatoria** y debe llevar sufijo ``_demo``, ``_pub`` o ``_demo_test``;
* se rechaza ``mongodb+srv://`` y cualquier host que no sea local;
* **no** se borra ninguna base ni colección ajena: se restaura con ``mongorestore`` sin ``--drop``
  y solo con el espacio de nombres del paquete reescrito al destino;
* se comprueba el SHA-256 del archivo contra el manifiesto antes de restaurar;
* al terminar se valida el paquete contra su propio manifiesto.

Uso (desde ``backend/``):

    python -m scripts.importar_demo_publicacion --archivo ../paquete-demo --destino <base_destino>
    python -m scripts.importar_demo_publicacion --archivo <...>.archive.gz --destino <base> --solo-validar
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from app.comercial.conexion import HOSTS_LOCALES
from app.config import get_settings
from app.db.mongo import init_database
from scripts.validar_demo_publicacion import validar_base

SUFIJOS = ("_demo", "_pub", "_demo_test")


def _sha256(archivo: Path) -> str:
    digest = hashlib.sha256()
    with archivo.open("rb") as manejador:
        for bloque in iter(lambda: manejador.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


def _tool(nombre: str) -> str:
    ruta = shutil.which(nombre)
    if not ruta:
        raise SystemExit(f"No se encontró {nombre} en el PATH (herramientas de MongoDB)")
    return ruta


def _resolver_archivo(ruta: Path) -> tuple[Path, dict]:
    """Acepta el archivo o la carpeta del paquete y devuelve (archivo, manifiesto)."""
    if ruta.is_dir():
        carpeta = ruta
        manifiesto = json.loads((carpeta / "manifiesto.json").read_text(encoding="utf-8"))
        archivo = carpeta / manifiesto["archivo"]["nombre"]
    else:
        archivo = ruta
        manifiesto = json.loads((archivo.parent / "manifiesto.json").read_text(encoding="utf-8"))
    return archivo, manifiesto


def importar(archivo: Path, destino: str, *, solo_validar: bool = False) -> dict:
    settings = get_settings()
    uri = settings.comercial_mongodb_uri or ""
    if uri.startswith("mongodb+srv://"):
        raise SystemExit("mongodb+srv:// no está permitido en este incremento")
    partes = urlsplit(uri)
    host = (partes.hostname or "").lower()
    if host not in HOSTS_LOCALES:
        raise SystemExit(f"Destino no autorizado: solo MongoDB local ({', '.join(sorted(HOSTS_LOCALES))})")
    if not destino or not destino.endswith(SUFIJOS):
        raise SystemExit("Indica la base destino con sufijo _demo, _pub o _demo_test")
    if destino == "atlas_comercial_local":
        raise SystemExit("La base de trabajo no puede ser destino de la importación")

    archivo, manifiesto = _resolver_archivo(archivo)
    if not archivo.exists():
        raise SystemExit(f"No existe el archivo del paquete: {archivo}")
    huella = _sha256(archivo)
    if huella != manifiesto["archivo"]["sha256"]:
        raise SystemExit("El SHA-256 del paquete no coincide con el manifiesto: no se importa")

    cliente = init_database(settings)
    destino_db = cliente.m5_db.client[destino]

    duracion = 0.0
    if not solo_validar:
        inicio = time.perf_counter()
        subprocess.run(
            [
                _tool("mongorestore"),
                "--uri",
                f"mongodb://{host}:{partes.port or 27017}",
                f"--archive={archivo}",
                "--gzip",
                f"--nsFrom={manifiesto['base_origen']}.*",
                f"--nsTo={destino}.*",
                "--quiet",
            ],
            check=True,
        )
        duracion = round(time.perf_counter() - inicio, 1)

    validacion = validar_base(destino_db, manifiesto)
    return {
        "base_destino": destino,
        "archivo": archivo.name,
        "sha256_verificado": True,
        "duracion_importacion_s": duracion,
        "validacion": validacion,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Importa y valida el paquete de publicación")
    parser.add_argument("--archivo", required=True, help="carpeta del paquete o archivo .archive.gz")
    parser.add_argument("--destino", required=True, help="base destino explícita (sufijo _demo/_pub)")
    parser.add_argument("--solo-validar", action="store_true", help="no restaura; solo valida la base")
    args = parser.parse_args()

    informe = importar(Path(args.archivo), args.destino, solo_validar=args.solo_validar)
    validacion = informe["validacion"]
    print(
        f"Importado en {informe['base_destino']} · {informe['duracion_importacion_s']} s · "
        f"validación {'OK' if validacion['ok'] else 'CON HALLAZGOS'}"
    )
    for hallazgo in validacion["hallazgos"]:
        print(f"  · {hallazgo}")
    return 0 if validacion["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
