"""Empaqueta la base de publicación (demo) en un archivo comprimido y escribe su manifiesto.

El paquete es un ``mongodump`` del esquema elegido (`--archive --gzip`), porque el esquema ya está
compactado por diseño: bloques temporales, precios binarios en centavos y solo la revisión activa.
El manifiesto declara, sin secretos:

* versión de esquema del paquete, versión de datos, ejecución y revisión publicada;
* política de precisión y total del horizonte (para comprobar que no se alteró nada);
* cobertura temporal y comercial;
* colecciones con documentos, tamaño lógico y almacenamiento en origen;
* índices indispensables;
* tamaño del archivo, su hash SHA-256 y el hash del manifiesto.

Uso (desde ``backend/``):

    python -m scripts.exportar_demo_publicacion --base atlas_comercial_demo
    python -m scripts.exportar_demo_publicacion --base atlas_comercial_demo --salida ../paquete/demo
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.comercial.conexion import HOSTS_LOCALES
from app.config import get_settings
from app.db.mongo import (
    M5_BLOQUES,
    M5_CATALOGO,
    M5_ESTADO,
    M5_EVALUACION_SERIES,
    M5_PLANIFICACION,
    M5_PRECIOS,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PUBLICACION,
    init_database,
)

ESQUEMA_PAQUETE = "atlas-comercial-demo-1"
INDICES = {
    M5_BLOQUES: [[("version", 1), ("store_id", 1), ("item_id", 1)]],
    M5_PREDICCIONES: [[("revision", 1), ("origen_d", 1), ("dimension", 1)]],
    M5_PREDICCIONES_SERIES: [[("revision", 1), ("store_id", 1), ("item_id", 1)]],
    M5_EVALUACION_SERIES: [[("revision", 1), ("origen_d", 1), ("store_id", 1), ("item_id", 1)]],
    M5_PLANIFICACION: [
        [("revision", 1), ("store_id", 1), ("item_id", 1)],
        [("revision", 1), ("pronostico_28", -1)],
    ],
    M5_PRECIOS: [[("version_precios", 1), ("store_id", 1), ("wm_yr_wk", 1)]],
    "m5_precios_catalogo": [[("version_precios", 1), ("store_id", 1)]],
    "m5_agregados": [[("version", 1), ("dimension", 1), ("clave", 1)]],
}


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


def exportar(base: str, salida: Path) -> dict:
    settings = get_settings()
    uri = settings.comercial_mongodb_uri or ""
    if uri.startswith("mongodb+srv://"):
        raise SystemExit("mongodb+srv:// no está permitido en este incremento")
    host = (urlsplit(uri).hostname or "").lower()
    if host not in HOSTS_LOCALES:
        raise SystemExit("La exportación solo se admite desde MongoDB local")
    if not base.endswith(("_demo", "_pub", "_demo_test")):
        raise SystemExit("Solo se empaqueta una base de publicación (sufijo _demo o _pub)")
    if base == (settings.comercial_mongodb_db or "") and base != "atlas_comercial_demo":
        raise SystemExit("La base a exportar debe ser la base de publicación, no la de trabajo")

    cliente = init_database(settings)
    db = cliente.m5_db if cliente.m5_db.name == base else cliente.m5_db.client[base]
    manifiesto_bd = db["m5_demo_manifest"].find_one({"_id": ESQUEMA_PAQUETE})
    if not manifiesto_bd or manifiesto_bd.get("estado") != "validada":
        raise SystemExit("La base de publicación no está generada y validada")

    salida.mkdir(parents=True, exist_ok=True)
    archivo = salida / "atlas-comercial-demo.archive.gz"
    inicio = time.perf_counter()
    partes = urlsplit(uri)
    uri_paquete = f"mongodb://{partes.hostname}:{partes.port or 27017}/{base}"
    subprocess.run(
        [
            _tool("mongodump"),
            "--uri",
            uri_paquete,
            "--db",
            base,
            f"--archive={archivo}",
            "--gzip",
            "--quiet",
        ],
        check=True,
    )
    duracion = round(time.perf_counter() - inicio, 1)

    estado = db[M5_ESTADO].find_one({"version": manifiesto_bd["version"]}) or {}
    publicacion = db[M5_PUBLICACION].find_one({"activa": True}) or {}
    colecciones = []
    for nombre in sorted(db.list_collection_names()):
        stats = db.command("collStats", nombre)
        colecciones.append(
            {
                "coleccion": nombre,
                "documentos": int(stats.get("count", 0)),
                "bson_mb": round(stats.get("size", 0) / 1024 / 1024, 2),
                "storage_mb": round(stats.get("storageSize", 0) / 1024 / 1024, 2),
                "indices": [list(indice) for indice in db[nombre].index_information()],
            }
        )
    cobertura = (estado.get("cobertura") or {})
    manifiesto = {
        "esquema_paquete": ESQUEMA_PAQUETE,
        "generado_en": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
        "base_origen": base,
        "servidor": "local",
        "version_datos": manifiesto_bd["version"],
        "run_id": manifiesto_bd["run_id"],
        "revision_publicada": publicacion.get("_id"),
        "politica_precision": publicacion.get("politica"),
        "total_horizonte": publicacion.get("total_horizonte"),
        "cobertura": {
            "desde": cobertura.get("desde"),
            "hasta": cobertura.get("hasta"),
            "dias": cobertura.get("dias"),
        },
        "contrato": {
            "tiendas": 10,
            "estados": 3,
            "categorias": 3,
            "departamentos": 7,
            "articulos": 3049,
            "series_articulo_tienda": 30490,
            "cortes_finales": [1885, 1913],
            "semanas_plan": 4,
        },
        "colecciones": colecciones,
        "indices_indispensables": {nombre: claves for nombre, claves in INDICES.items()},
        "archivo": {
            "nombre": archivo.name,
            "bytes": archivo.stat().st_size,
            "mb": round(archivo.stat().st_size / 1024 / 1024, 2),
            "sha256": _sha256(archivo),
        },
        "duracion_exportacion_s": duracion,
        "validacion": manifiesto_bd.get("validacion"),
        "instrucciones": {
            "importar": "python -m scripts.importar_demo_publicacion --archivo <paquete>/atlas-comercial-demo.archive.gz --destino <base_destino>",
            "configurar_api": "COMERCIAL_MONGODB_URI=mongodb://HOST:PUERTO/<base_destino> y COMERCIAL_MONGODB_DB=<base_destino>",
            "nota": "El importador exige la base destino explícita y no borra bases ni colecciones ajenas al paquete.",
        },
    }
    texto = json.dumps(manifiesto, ensure_ascii=False, indent=1)
    (salida / "manifiesto.json").write_text(texto, encoding="utf-8")
    with gzip.open(salida / "manifiesto.json.gz", "wt", encoding="utf-8") as comprimido:
        comprimido.write(texto)
    manifiesto["archivo_manifiesto"] = {
        "nombre": "manifiesto.json",
        "bytes": (salida / "manifiesto.json").stat().st_size,
        "sha256": hashlib.sha256(texto.encode("utf-8")).hexdigest(),
    }
    (salida / "manifiesto.json").write_text(
        json.dumps(manifiesto, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return manifiesto


def main() -> int:
    parser = argparse.ArgumentParser(description="Empaqueta la base de publicación (demo)")
    parser.add_argument("--base", default="atlas_comercial_demo")
    parser.add_argument("--salida", default="../paquete-demo")
    args = parser.parse_args()
    manifiesto = exportar(args.base, Path(args.salida))
    print(
        f"Paquete: {manifiesto['archivo']['nombre']} · {manifiesto['archivo']['mb']} MB · "
        f"sha256 {manifiesto['archivo']['sha256'][:16]}… · exportado en "
        f"{manifiesto['duracion_exportacion_s']} s"
    )
    print(f"Manifiesto: {Path(args.salida) / 'manifiesto.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
