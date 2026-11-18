"""Inspección temporal del estado de la base local del proyecto (no escribe nada)."""

import io
import sys

from pymongo import MongoClient

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
db = MongoClient("mongodb://127.0.0.1:27017")["atlas_comercial_local"]

print("colecciones:", sorted(db.list_collection_names()))
print()
agrupado = {"_id": {"dim": "$dimension", "ver": "$version"}, "n": {"$sum": 1}, "ejemplo": {"$first": "$clave"}}
for doc in db.m5_agregados.aggregate([{"$group": agrupado}, {"$sort": {"_id.dim": 1}}]):
    print(doc)

print()
print("bloques", db.m5_bloques.count_documents({}))
bloque = db.m5_bloques.find_one({})
print("bloque:", {k: (f"len={len(v)}" if k == "unidades" else v) for k, v in bloque.items()})
largos = list(
    db.m5_bloques.aggregate(
        [{"$project": {"item_id": 1, "store_id": 1, "d_inicio": 1, "n": {"$size": "$unidades"}}}, {"$sort": {"d_inicio": 1}}, {"$limit": 1}]
    )
)
print("primer bloque:", largos)
maximos = list(
    db.m5_bloques.aggregate([{"$project": {"d_fin": {"$add": ["$d_inicio", {"$size": "$unidades"}]}}}, {"$group": {"_id": None, "max": {"$max": "$d_fin"}, "min": {"$min": "$d_inicio"}}}])
)
print("cobertura bloques:", maximos)

print()
print("precios docs", db.m5_precios.count_documents({}))
precio = db.m5_precios.find_one({})
print("precio:", {k: (f"len={len(v)}" if k in ("items", "precios") else v) for k, v in precio.items()})
print(
    "semanas precio:",
    db.m5_precios.distinct("wm_yr_wk")[:3],
    "...",
    db.m5_precios.distinct("wm_yr_wk")[-3:],
)
print("n semanas precio:", len(db.m5_precios.distinct("wm_yr_wk")))

print()
print("catalogo:", list(db.m5_catalogo.aggregate([{"$group": {"_id": "$tipo", "n": {"$sum": 1}}}])))
print("calendario:", db.m5_calendario.find_one(sort=[("d", 1)]))
print("calendario n:", db.m5_calendario.count_documents({}))
print("derivados:", list(db.m5_derivados.aggregate([{"$group": {"_id": "$tipo", "n": {"$sum": 1}}}])))
