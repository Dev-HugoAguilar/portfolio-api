"""Compacta el «Comparativo bancario (CNBV)» en Atlas.

El tablero compara un concepto entre instituciones y sigue su serie; para eso
basta con **un hecho por institución, concepto y periodo**, los indicadores
publicados y la ficha de cada boletín. Lo que sobraba:

* ``cnbv_filas_originales``: las filas del .xlsx tal cual, ~6 MiB que ninguna
  consulta leía (solo se contaban). El archivo original se conserva en
  ``data/cnbv`` y su hash en la ficha del boletín; el número de filas se sigue
  publicando en la cobertura, leído de esa ficha.
* un índice compuesto que duplicaba el prefijo de otro: toda consulta de un
  periodo y un concepto queda cubierta por el índice de concepto+institución y
  por el de periodo.

Uso::

    python -m scripts.compactar_cnbv            # simulación
    python -m scripts.compactar_cnbv --aplicar  # escribe en Atlas
"""

from __future__ import annotations

import argparse

from pymongo import MongoClient

from app.config import get_settings

HECHOS = "cnbv_hechos"
FILAS = "cnbv_filas_originales"
BOLETINES = "cnbv_boletines"
INDICADORES = "cnbv_indicadores"

#: Índice redundante: (dataset_id, periodo, concepto_id, institucion_id) es un
#: prefijo reordenado de (dataset_id, concepto_id, institucion_id, periodo) y
#: toda consulta por periodo tiene además el índice (dataset_id, periodo, seccion).
INDICES_SIN_USO = ["cnbv_hechos_periodo_concepto"]

COLECCIONES = (HECHOS, INDICADORES, BOLETINES)


def cifras(db, dataset_id: str) -> dict:
    """Cifras que publica el tablero, para conciliar antes y después."""
    base = {"dataset_id": dataset_id, "vigente": True}
    periodos = sorted(db[HECHOS].distinct("periodo", base))
    sistema = db[HECHOS].find_one({**base, "es_sistema": True}, {"valor": 1})
    boletines = list(db[BOLETINES].find(base, {"filas_originales": 1, "hechos": 1}))
    return {
        "hechos": db[HECHOS].count_documents(base),
        "indicadores": db[INDICADORES].count_documents(base),
        "boletines": len(boletines),
        "periodos": len(periodos),
        "periodo_min": periodos[0] if periodos else None,
        "periodo_max": periodos[-1] if periodos else None,
        "instituciones": len(db[HECHOS].distinct("institucion_id", base)),
        "conceptos": len(db[HECHOS].distinct("concepto_id", base)),
        "filas_originales_declaradas": sum(int(b.get("filas_originales") or 0) for b in boletines),
        "primer_valor_sistema": round(float((sistema or {}).get("valor") or 0), 6),
    }


def tamano(db, nombre: str) -> dict:
    if nombre not in db.list_collection_names():
        return {"docs": 0, "data_mib": 0.0, "idx_mib": 0.0}
    st = db.command("collStats", nombre)
    return {
        "docs": st.get("count"),
        "data_mib": round(st.get("size", 0) / 1048576, 2),
        "idx_mib": round(st.get("totalIndexSize", 0) / 1048576, 2),
    }


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Compacta el comparativo bancario")
    argumentos.add_argument("--aplicar", action="store_true", help="Escribe los cambios.")
    opciones = argumentos.parse_args()

    ajustes = get_settings()
    cliente = MongoClient(ajustes.mongodb_uri, serverSelectionTimeoutMS=15_000, compressors="zlib")
    db = cliente[ajustes.mongodb_db]
    boletin = db[BOLETINES].find_one({"vigente": True}) or {}
    dataset_id = boletin.get("dataset_id")
    if not dataset_id:
        print("No hay boletines vigentes de la CNBV: no se toca nada.")
        cliente.close()
        return 1
    print(f"dataset: {dataset_id}")

    antes = cifras(db, dataset_id)
    print("antes:", antes)
    print("tamaño antes:", {nombre: tamano(db, nombre) for nombre in COLECCIONES + (FILAS,)})

    if not opciones.aplicar:
        print("\nColección a eliminar:", FILAS)
        print("Índices a eliminar:", ", ".join(INDICES_SIN_USO))
        print("Modo simulación: usa --aplicar para escribir.")
        cliente.close()
        return 0

    if FILAS in db.list_collection_names():
        filas_en_bd = db[FILAS].count_documents({"dataset_id": dataset_id})
        declaradas = antes["filas_originales_declaradas"]
        if filas_en_bd and filas_en_bd != declaradas:
            print(
                f"Las filas en la base ({filas_en_bd}) no coinciden con las declaradas "
                f"en los boletines ({declaradas}): no se elimina nada.",
                flush=True,
            )
            cliente.close()
            return 1
        db.drop_collection(FILAS)
        print(f"colección eliminada: {FILAS} ({filas_en_bd} filas)")

    existentes = {indice["name"] for indice in db[HECHOS].list_indexes()}
    for nombre in INDICES_SIN_USO:
        if nombre in existentes:
            db[HECHOS].drop_index(nombre)
            print("índice eliminado:", nombre)

    despues = cifras(db, dataset_id)
    print("después:", despues)
    print("tamaño después:", {nombre: tamano(db, nombre) for nombre in COLECCIONES})

    # La cobertura sigue publicando las filas del original, ahora desde la ficha.
    iguales = all(antes[clave] == despues[clave] for clave in antes)
    print("conciliación:", "cifras idénticas" if iguales else "DIFERENCIAS")
    if not iguales:
        for clave in sorted(antes):
            if antes[clave] != despues[clave]:
                print(f"  {clave}: {antes[clave]} → {despues[clave]}")
    cliente.close()
    return 0 if iguales else 1


if __name__ == "__main__":
    raise SystemExit(main())
