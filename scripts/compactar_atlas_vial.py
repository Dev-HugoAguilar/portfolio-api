"""Compacta el «Atlas vial» en Atlas: campos e índices que el tablero no usa.

El mapa y sus filtros necesitan folio, fecha/hora, ubicación, alcaldía, tipo de
evento, consecuencia, sexo, edad, tipo de usuario, vehículo, vialidad y
semáforo. Todo lo demás que venía en el archivo de la SSC se escribía en cada
punto y nunca se leía: se retira para liberar espacio en el clúster gratuito.

Qué hace, en orden y sin tocar datos ajenos:

1. Cuenta y suma las cifras del tablero **antes** (hechos, lesionados,
   fallecidos, con coordenadas, alcaldías) para poder conciliar.
2. Retira los campos sin uso de la versión publicada, por lotes.
3. Elimina los índices que ningún filtro aprovecha (quedan los que sirven las
   consultas reales).
4. Vuelve a contar y compara: si algo no cuadra, avisa y devuelve error.

Uso::

    python -m scripts.compactar_atlas_vial            # simulación
    python -m scripts.compactar_atlas_vial --aplicar  # escribe en Atlas

La copia original (CSV de la SSC) se conserva en ``data/atlas/versiones``.
"""

from __future__ import annotations

import argparse

from pymongo import MongoClient

from app.config import get_settings

HECHOS = "atlas_hechos"
ESTADO = "atlas_estado"

#: Campos que el archivo trae y que la API nunca lee ni el tablero muestra.
CAMPOS_SIN_USO = [
    "fecha_dt",  # fecha + hora ya están en sus propios campos
    "alcaldia_texto",  # alcaldía canónica es `alcaldia`
    "alcaldia_geometria",  # la geometría vive en el geojson, no en cada punto
    "zona_vial",
    "tipo_interseccion",
    "origen_norm",  # el filtro y la ficha usan `origen`
    "unidad_a_cargo",
    "trasladado_lesionados",
    "unidad_medica",
    "matricula_unidad_medica",
    "personas_totales",
    "personas_consecuencia",
    "personas_sexo_total",
    "personas_edad_texto",
    "vehiculo_numero",
]

#: Índices que ninguna consulta aprovecha. Se conservan:
#:   version_folio             → abrir la ficha de un hecho
#:   version_fecha_hora_folio  → orden estable de puntos, tabla y exportación
#:   version_ubicacion (2d)    → recorte por la zona visible del mapa
INDICES_SIN_USO = [
    "atlas_hechos_version_fecha",
    "atlas_hechos_version_alcaldia",
    "atlas_hechos_version_tipo",
]

LOTE = 2_000


def cifras(db, version: str) -> dict:
    """Cifras que el tablero publica, para conciliar antes y después."""
    base = {"version": version}
    pipeline = [
        {"$match": base},
        {
            "$group": {
                "_id": None,
                "hechos": {"$sum": 1},
                "lesionadas": {"$sum": "$personas_lesionadas"},
                "fallecidas": {"$sum": "$personas_fallecidas"},
                "con_coordenadas": {
                    "$sum": {"$cond": [{"$eq": ["$coord_estado", "ok"]}, 1, 0]}
                },
                "con_fecha": {"$sum": {"$cond": [{"$ne": ["$fecha", None]}, 1, 0]}},
                "con_hora": {"$sum": {"$cond": [{"$ne": ["$hora", None]}, 1, 0]}},
                "con_sexo": {"$sum": {"$cond": [{"$ne": ["$personas_sexo", None]}, 1, 0]}},
                "con_edad": {"$sum": {"$cond": [{"$ne": ["$personas_edad", None]}, 1, 0]}},
            }
        },
    ]
    fila = next(iter(db[HECHOS].aggregate(pipeline)), {})
    fila.pop("_id", None)
    alcaldias = db[HECHOS].distinct("alcaldia", base)
    fila["alcaldias"] = len([nombre for nombre in alcaldias if nombre])
    fila["fechas"] = len(db[HECHOS].distinct("fecha", base))
    fila["folios_distintos"] = len(db[HECHOS].distinct("folio", base))
    return fila


def tamano(db) -> dict:
    st = db.command("collStats", HECHOS)
    return {
        "docs": st.get("count"),
        "data_mib": round(st.get("size", 0) / 1048576, 2),
        "idx_mib": round(st.get("totalIndexSize", 0) / 1048576, 2),
    }


def retirar_campos(db, version: str, campos: list[str]) -> int:
    """Quita los campos por lotes para no reescribir la colección de golpe."""
    existentes = {"$or": [{campo: {"$exists": True}} for campo in campos]}
    tocados = 0
    while True:
        ids = [
            documento["_id"]
            for documento in db[HECHOS].find(
                {"version": version, "$and": [existentes]}, {"_id": 1}
            ).limit(LOTE)
        ]
        if not ids:
            break
        db[HECHOS].update_many({"_id": {"$in": ids}}, {"$unset": {c: "" for c in campos}})
        tocados += len(ids)
        print(f"  campos retirados en {tocados} documentos…", flush=True)
    return tocados


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Compacta el Atlas vial en Atlas")
    argumentos.add_argument("--aplicar", action="store_true", help="Escribe los cambios.")
    opciones = argumentos.parse_args()

    ajustes = get_settings()
    cliente = MongoClient(ajustes.mongodb_uri, serverSelectionTimeoutMS=15_000, compressors="zlib")
    db = cliente[ajustes.mongodb_db]
    version = (db[ESTADO].find_one({"_id": "activo"}) or {}).get("version")
    if not version:
        print("No hay versión activa del Atlas vial: no se toca nada.")
        cliente.close()
        return 1
    print(f"versión activa: {version}")

    antes = cifras(db, version)
    print("antes:", antes)
    print("tamaño antes:", tamano(db))

    if not opciones.aplicar:
        print("\nCampos a retirar:", ", ".join(CAMPOS_SIN_USO))
        print("Índices a eliminar:", ", ".join(INDICES_SIN_USO))
        print("Modo simulación: usa --aplicar para escribir.")
        cliente.close()
        return 0

    retirados = retirar_campos(db, version, CAMPOS_SIN_USO)
    print(f"documentos actualizados: {retirados}")

    existentes = {indice["name"] for indice in db[HECHOS].list_indexes()}
    for nombre in INDICES_SIN_USO:
        if nombre in existentes:
            db[HECHOS].drop_index(nombre)
            print("índice eliminado:", nombre)

    despues = cifras(db, version)
    print("después:", despues)
    print("tamaño después:", tamano(db))
    iguales = antes == despues
    print("conciliación:", "cifras idénticas" if iguales else "DIFERENCIAS")
    if not iguales:
        for clave in sorted(set(antes) | set(despues)):
            if antes.get(clave) != despues.get(clave):
                print(f"  {clave}: {antes.get(clave)} → {despues.get(clave)}")
    cliente.close()
    return 0 if iguales else 1


if __name__ == "__main__":
    raise SystemExit(main())
