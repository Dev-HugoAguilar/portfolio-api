"""Importa boletines reales de la CNBV (Banca Múltiple) a MongoDB.

Uso (desde ``backend/``)::

    python -m scripts.importar_cnbv --descargar                 # periodos por omisión
    python -m scripts.importar_cnbv --periodo 202410 --descargar
    python -m scripts.importar_cnbv --archivo data/cnbv/BE_BM_202410.xlsx --periodo 202410
    python -m scripts.importar_cnbv --dry-run --periodo 202410   # solo valida, no escribe

Reglas: nunca borra colecciones ni documentos de otras fuentes; solo escribe en
las colecciones ``cnbv_*`` del dataset ``cnbv_banca_multiple_boletin``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.cnbv import sources
from app.cnbv.importer import descargar_boletin, importar_boletin, sha256_archivo
from app.cnbv.parser import ErrorFormatoFuente, parsear_boletin
from app.config import BACKEND_DIR, get_settings
from app.db.indexes import ensure_indexes
from app.db.mongo import close_database, init_database

DIRECTORIO_DATOS = BACKEND_DIR / "data" / "cnbv"


def _imprimir_reporte(reporte: dict) -> None:
    print(f"\n=== {reporte['archivo']} ({reporte['periodo']}) ===")
    print(f"  url        : {reporte['url']}")
    print(f"  sha256     : {reporte['sha256']}")
    print(f"  bytes      : {reporte['bytes']:,}")
    print(f"  estado     : {reporte['estado']} (revisión {reporte['revision']})")
    print(f"  hechos     : {reporte['hechos_nuevos']} nuevos, {reporte['duplicados']} duplicados")
    print(f"  originales : {reporte['filas_originales']} filas | {reporte['indicadores']} indicadores")
    print(f"  rechazados : {reporte['rechazados']}")
    for rechazo in reporte["rechazados_detalle"]:
        print(f"      - fila {rechazo.get('fila')}: {rechazo.get('motivo')} :: {rechazo.get('concepto')}")
    print(f"  ausentes   : {reporte['celdas_ausentes']} celdas vacías (no se convierten en cero)")
    print(f"  instituciones/conceptos: {reporte['instituciones']} / {reporte['conceptos']}")
    for aviso in reporte["avisos"]:
        print(f"  aviso      : {aviso}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Importa boletines CNBV de Banca Múltiple.")
    parser.add_argument("--periodo", action="append", default=None, help="AAAAMM (repetible)")
    parser.add_argument("--archivo", default=None, help="archivo local a importar")
    parser.add_argument("--dir", default=str(DIRECTORIO_DATOS), help="directorio de archivos")
    parser.add_argument("--descargar", action="store_true", help="descarga los boletines que falten")
    parser.add_argument("--dry-run", action="store_true", help="valida sin escribir en MongoDB")
    parser.add_argument("--reporte", default=None, help="ruta del reporte JSON")
    parser.add_argument("--forzar", action="store_true", help="reimporta aunque el hash coincida")
    args = parser.parse_args(argv)

    directorio = Path(args.dir)
    periodos = args.periodo or list(sources.PERIODOS_POR_OMISION)

    reportes: list[dict] = []

    if args.dry_run:
        archivos: list[tuple[str, Path]] = []
        if args.archivo:
            periodo = args.periodo[0] if args.periodo else ""
            archivos.append((periodo, Path(args.archivo)))
        else:
            for periodo in periodos:
                nombre = sources.PATRON_ARCHIVO.format(periodo=periodo)
                archivos.append((periodo, directorio / nombre))
        for periodo, ruta in archivos:
            if not ruta.exists():
                print(f"[FALTA] {ruta}")
                continue
            try:
                resultado = parsear_boletin(ruta, periodo)
            except (ErrorFormatoFuente, ValueError) as error:
                print(f"[ERROR] {ruta.name}: {error}")
                continue
            print(
                f"[dry-run] {ruta.name}: {len(resultado.hechos)} hechos, "
                f"{len(resultado.filas_crudas)} filas originales, "
                f"{len(resultado.indicadores)} indicadores, "
                f"{len(resultado.rechazados)} rechazados, {resultado.ausentes} ausentes, "
                f"sha256={sha256_archivo(ruta)[:16]}…"
            )
            for aviso in resultado.avisos:
                print(f"    aviso: {aviso}")
        return 0

    settings = get_settings()
    print(f"Base de datos: {settings.mongodb_db} ({settings.safe_mongo_host})")
    db = init_database(settings)
    try:
        if not db.ping():
            print(f"[ERROR] MongoDB no responde en {settings.safe_mongo_host}")
            return 1
        ensure_indexes(db.db)

        if args.archivo:
            periodo = args.periodo[0] if args.periodo else ""
            ruta = Path(args.archivo)
            if not ruta.exists():
                print(f"[ERROR] no existe el archivo {ruta}")
                return 1
            reporte = importar_boletin(db.db, ruta, periodo, forzar=args.forzar)
            reportes.append(reporte.resumen())
        else:
            for periodo in periodos:
                try:
                    ruta = descargar_boletin(periodo, directorio) if args.descargar else (
                        directorio / sources.PATRON_ARCHIVO.format(periodo=periodo)
                    )
                    if not ruta.exists():
                        print(f"[FALTA] {ruta} (usa --descargar)")
                        continue
                    fuente = sources.archivo_fuente(periodo)
                    reporte = importar_boletin(
                        db.db, ruta, periodo, url=fuente.url, forzar=args.forzar
                    )
                    reportes.append(reporte.resumen())
                except Exception as error:  # noqa: BLE001 - se informa y se sigue
                    print(f"[ERROR] {periodo}: {type(error).__name__}: {error}")
                    reportes.append({"periodo": periodo, "estado": "error", "detalle": str(error)})

        for reporte in reportes:
            if reporte.get("estado") == "error":
                print(f"\n=== {reporte['periodo']} ===\n  ERROR: {reporte.get('detalle')}")
                continue
            _imprimir_reporte(reporte)

        total = db.db["cnbv_hechos"].count_documents({"vigente": True})
        periodos_cargados = sorted(
            db.db["cnbv_boletines"].distinct("periodo", {"vigente": True})
        )
        print(f"\nHechos vigentes en MongoDB: {total:,}")
        print(f"Periodos vigentes: {', '.join(periodos_cargados) or 'ninguno'}")

        if args.reporte:
            destino = Path(args.reporte)
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(
                json.dumps(reportes, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"Reporte escrito en {destino}")
        return 0
    finally:
        close_database()


if __name__ == "__main__":
    sys.exit(main())
