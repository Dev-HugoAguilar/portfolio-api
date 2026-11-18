"""Verifica los datos importados de la CNBV contra el archivo original.

Lee **la celda exacta** que el importador registró (``fila`` y ``columna`` del
boletín) y la compara con el valor guardado en MongoDB. Es una comprobación de
ida y vuelta independiente de la lógica del parser: si el importador desplazara
una columna o redondeara un valor, aquí se vería.

Uso (desde ``backend/``)::

    python -m scripts.verificar_cnbv                  # todos los hechos vigentes
    python -m scripts.verificar_cnbv --muestra 500    # muestra determinista
    python -m scripts.verificar_cnbv --periodo 202410
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import openpyxl

from app.cnbv import sources
from app.cnbv.importer import sha256_archivo
from app.config import BACKEND_DIR, get_settings
from app.db.mongo import CNBV_BOLETINES, CNBV_HECHOS, CNBV_INDICADORES, close_database, init_database

DIRECTORIO_DATOS = BACKEND_DIR / "data" / "cnbv"
SEMILLA_VERIFICACION = 20260401


def _mapa_celdas(ruta: Path, hoja: str) -> dict[tuple[int, int], object]:
    libro = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
    if hoja not in libro.sheetnames:
        libro.close()
        return {}
    mapa: dict[tuple[int, int], object] = {}
    for indice, fila in enumerate(
        libro[hoja].iter_rows(min_row=1, max_row=400, max_col=200, values_only=True), start=1
    ):
        for columna, valor in enumerate(fila, start=1):
            if valor is not None:
                mapa[(indice, columna)] = valor
    libro.close()
    return mapa


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Concilia los hechos CNBV contra el archivo original.")
    parser.add_argument("--periodo", action="append", default=None, help="AAAA-MM (repetible)")
    parser.add_argument("--muestra", type=int, default=0, help="0 = verificar todos los hechos")
    parser.add_argument("--dir", default=str(DIRECTORIO_DATOS))
    args = parser.parse_args(argv)

    settings = get_settings()
    print(f"Base de datos: {settings.mongodb_db} ({settings.safe_mongo_host})")
    db = init_database(settings)
    directorio = Path(args.dir)
    total = 0
    fallos = 0
    try:
        if not db.ping():
            print("[ERROR] MongoDB no responde")
            return 1

        filtro: dict = {"vigente": True}
        if args.periodo:
            filtro["periodo"] = {"$in": args.periodo}

        for boletin in db.db[CNBV_BOLETINES].find(filtro).sort("periodo", 1):
            periodo = boletin["periodo"]
            archivo = directorio / sources.PATRON_ARCHIVO.format(periodo=periodo.replace("-", ""))
            if not archivo.exists():
                print(f"[FALTA] {archivo.name}: no se puede conciliar el periodo {periodo}")
                fallos += 1
                continue

            digest = sha256_archivo(archivo)
            if digest != boletin.get("sha256"):
                print(
                    f"[HASH] {archivo.name}: el archivo local no coincide con el importado "
                    f"({digest[:12]}… vs {str(boletin.get('sha256'))[:12]}…)"
                )
                fallos += 1

            mapa = _mapa_celdas(archivo, sources.HOJA_SALDOS)
            hechos = list(
                db.db[CNBV_HECHOS].find(
                    {"dataset_id": boletin["dataset_id"], "periodo": periodo, "vigente": True},
                    projection={
                        "_id": 0,
                        "fila": 1,
                        "columna": 1,
                        "valor": 1,
                        "institucion_nombre": 1,
                        "concepto_nombre": 1,
                        "unidad": 1,
                    },
                )
            )
            if args.muestra and len(hechos) > args.muestra:
                azar = random.Random(SEMILLA_VERIFICACION)
                hechos = azar.sample(hechos, args.muestra)

            discrepancias = 0
            for hecho in hechos:
                total += 1
                original = mapa.get((hecho["fila"], hecho["columna"]))
                if original is None:
                    discrepancias += 1
                    print(
                        f"  [VACIA] {periodo} fila {hecho['fila']} col {hecho['columna']} "
                        f"({hecho['institucion_nombre']}): el archivo no tiene valor"
                    )
                    continue
                if not isinstance(original, (int, float)):
                    discrepancias += 1
                    print(f"  [TIPO] {periodo} fila {hecho['fila']}: valor no numérico {original!r}")
                    continue
                if float(original) != float(hecho["valor"]):
                    discrepancias += 1
                    print(
                        f"  [VALOR] {periodo} {hecho['institucion_nombre']} "
                        f"{hecho['concepto_nombre']}: base={hecho['valor']} archivo={original}"
                    )

            indicadores = db.db[CNBV_INDICADORES].count_documents(
                {"dataset_id": boletin["dataset_id"], "periodo_boletin": periodo, "vigente": True}
            )
            estado = "OK" if discrepancias == 0 else f"{discrepancias} discrepancias"
            print(
                f"{periodo}: {len(hechos)} hechos verificados, {estado} | "
                f"unidad={hechos[0]['unidad'] if hechos else 'n/d'} | indicadores={indicadores} | "
                f"sha256={digest[:12]}…"
            )
            fallos += discrepancias

        print(f"\nHechos conciliados: {total} | discrepancias: {fallos}")
        return 0 if fallos == 0 else 1
    finally:
        close_database()


if __name__ == "__main__":
    sys.exit(main())
