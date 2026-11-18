"""Compacta la versión activa de marketing en Atlas.

Toma la copia original fuera de Atlas (CSV.gz del extracto) y reconstruye los
resultados que el tablero necesita, sustituyendo el detalle por agregados:

    sesiones (17 906)  →  flujo (día×canal×dispositivo×país×resultado) + tiempos
    artículos (719)    →  categorías por compra + afinidad de compras conjuntas
    eventos (23 059)   →  se retira: la tabla de detalle deja de existir
    categorías (10 598 filas por día y dimensión)  →  una fila por compra y categoría

Los cálculos se hacen **antes** de tocar Atlas y solo se escriben agregados. Las
colecciones de detalle se eliminan únicamente después de comprobar que los
agregados cubren las mismas cifras (conciliación incluida en la salida).
"""

from __future__ import annotations

import argparse
import csv
import gzip
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from app.config import get_settings
from app.marketing.ingesta import _canal

#: Colecciones de agregados nuevos.
FLUJO = "marketing_flujo"
TIEMPOS = "marketing_tiempos"
AFINIDAD = "marketing_afinidad"
CATEGORIAS = "marketing_categorias"

#: Colecciones de detalle que el modelo compacto no necesita.
DETALLE = ["marketing_sesiones", "marketing_articulos", "marketing_eventos"]

TRANSICIONES = {1: "vista_carrito", 2: "carrito_pago", 3: "pago_compra"}
TRAMOS = (0, 0.25, 0.5, 1, 2, 4, 8, 24)


def _leer(carpeta: Path, nombre: str):
    with gzip.open(carpeta / f"{nombre}.csv.gz", "rt", encoding="utf-8", newline="") as archivo:
        for fila in csv.DictReader(archivo):
            yield fila


def _horas(desde: str | None, hasta: str | None) -> float | None:
    """Horas entre dos marcas de tiempo en **microsegundos** del extracto."""
    if not desde or not hasta:
        return None
    try:
        return (int(hasta) - int(desde)) / 3_600_000_000
    except (TypeError, ValueError):
        return None


def _tramo(horas: float) -> float:
    """Límite inferior del tramo en horas (0, 0.25, … 24).

    Se guarda el número, no la etiqueta: la API decide el texto y así el agregado
    no repite cadenas ni puede desalinearse de la definición del embudo.
    """
    anterior = TRAMOS[0]
    for limite in TRAMOS[1:]:
        if horas < limite:
            return float(anterior)
        anterior = limite
    return float(TRAMOS[-1])


def construir(carpeta: Path) -> dict:
    flujo: Counter = Counter()
    distintos: Counter = Counter()
    tiempos: Counter = Counter()
    sesiones = 0
    con_compra = 0
    for fila in _leer(carpeta, "sesiones"):
        sesiones += 1
        canal = _canal(fila.get("fuente"), fila.get("medio"))
        dispositivo = fila.get("dispositivo") or "Sin información"
        pais = fila.get("pais") or "Sin información"
        compro = 1 if fila.get("t4") else 0
        con_compra += compro
        clave_flujo = (fila.get("fecha"), canal, dispositivo, pais, compro)
        flujo[clave_flujo] += 1
        # Sesiones cuyo dispositivo de sesión difiere del de adquisición: la
        # atribución las asigna igualmente al de adquisición, y el tablero lo declara.
        if (fila.get("dispositivo_sesion") or dispositivo) != dispositivo:
            distintos[clave_flujo] += 1
        for paso, clave in TRANSICIONES.items():
            horas = _horas(fila.get(f"t{paso}"), fila.get(f"t{paso + 1}"))
            if horas is not None and horas >= 0:
                tiempos[(fila.get("fecha"), canal, dispositivo, pais, clave, _tramo(horas))] += 1

    # Afinidad y categorías: se trabaja **por compra**, no por línea. Una compra se
    # atribuye a las dimensiones de su primera línea, de modo que sumar filas nunca
    # cuenta dos veces la misma compra y los filtros siguen dando cifras exactas.
    lineas: dict[str, list[dict]] = defaultdict(list)
    for fila in _leer(carpeta, "articulos"):
        clave = fila.get("clave_compra")
        if clave:
            lineas[clave].append(fila)

    categorias_por_compra: dict[str, set[str]] = defaultdict(set)
    por_categoria: Counter = Counter()
    pares: Counter = Counter()
    categorias: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for clave in sorted(lineas):
        filas = sorted(lineas[clave], key=lambda fila: (fila.get("fecha") or "", fila.get("session_id") or ""))
        primera = filas[0]
        dimensiones = (
            primera.get("fecha"),
            _canal(primera.get("fuente"), primera.get("medio")),
            primera.get("dispositivo") or "Sin información",
            primera.get("pais") or "Sin información",
        )
        acumulado: dict[str, dict[str, Any]] = {}
        for fila in filas:
            categoria = (fila.get("categoria") or "").strip() or "Sin información"
            categorias_por_compra[clave].add(categoria)
            datos_categoria = acumulado.setdefault(
                categoria, {"articulos": set(), "unidades": 0, "ingresos": 0.0}
            )
            if fila.get("articulo"):
                datos_categoria["articulos"].add(fila["articulo"])
            datos_categoria["unidades"] += int(float(fila.get("unidades") or 0))
            datos_categoria["ingresos"] += float(fila.get("ingresos_linea_usd") or 0)
        for categoria, datos_categoria in acumulado.items():
            documento = categorias.setdefault(
                (*dimensiones, categoria),
                {"compras": 0, "articulos": 0, "unidades": 0, "ingresos_linea_usd": 0.0},
            )
            documento["compras"] += 1
            documento["articulos"] += len(datos_categoria["articulos"])
            documento["unidades"] += datos_categoria["unidades"]
            documento["ingresos_linea_usd"] += datos_categoria["ingresos"]
        orden = sorted(categorias_por_compra[clave])
        for categoria in orden:
            por_categoria[categoria] += 1
        for a, b in combinations(orden, 2):
            pares[(a, b)] += 1

    return {
        "flujo": flujo,
        "distintos": distintos,
        "tiempos": tiempos,
        "categorias": categorias,
        "sesiones": sesiones,
        "con_compra": con_compra,
        "por_categoria": por_categoria,
        "pares": pares,
        "compras": len(categorias_por_compra),
    }


def publicar(db, version: str, datos: dict) -> dict:
    """Escribe los agregados y devuelve el conteo de documentos por colección."""
    for coleccion in (FLUJO, TIEMPOS, AFINIDAD, CATEGORIAS):
        db[coleccion].delete_many({"version": version})

    db[FLUJO].insert_many(
        [
            {
                "version": version,
                "fecha": fecha,
                "canal": canal,
                "dispositivo": dispositivo,
                "pais": pais,
                "compro": compro,
                "sesiones": sesiones,
                "dispositivo_distinto": datos["distintos"].get(
                    (fecha, canal, dispositivo, pais, compro), 0
                ),
            }
            for (fecha, canal, dispositivo, pais, compro), sesiones in datos["flujo"].items()
        ]
        or [{"version": version, "vacio": True}]
    )
    db[TIEMPOS].insert_many(
        [
            {
                "version": version,
                "fecha": fecha,
                "canal": canal,
                "dispositivo": dispositivo,
                "pais": pais,
                "transicion": clave,
                "tramo": tramo,
                "sesiones": sesiones,
            }
            for (fecha, canal, dispositivo, pais, clave, tramo), sesiones in datos["tiempos"].items()
        ]
        or [{"version": version, "vacio": True}]
    )
    db[AFINIDAD].insert_many(
        [
            {
                "version": version,
                "a": a,
                "b": b,
                "compras_conjuntas": compras,
                "compras_a": datos["por_categoria"].get(a, 0),
                "compras_b": datos["por_categoria"].get(b, 0),
            }
            for (a, b), compras in datos["pares"].items()
        ]
        or [{"version": version, "vacio": True}]
    )
    db[CATEGORIAS].insert_many(
        [
            {
                "version": version,
                "fecha": fecha,
                "canal": canal,
                "dispositivo": dispositivo,
                "pais": pais,
                "categoria": categoria,
                **metricas,
                "ingresos_linea_usd": round(metricas["ingresos_linea_usd"], 2),
            }
            for (fecha, canal, dispositivo, pais, categoria), metricas in datos[
                "categorias"
            ].items()
        ]
        or [{"version": version, "vacio": True}]
    )
    return {
        FLUJO: db[FLUJO].count_documents({"version": version}),
        TIEMPOS: db[TIEMPOS].count_documents({"version": version}),
        AFINIDAD: db[AFINIDAD].count_documents({"version": version}),
        CATEGORIAS: db[CATEGORIAS].count_documents({"version": version}),
    }


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Compacta la versión activa de marketing")
    argumentos.add_argument(
        "--carpeta", default="data/marketing/muestra5", help="Copia original fuera de Atlas."
    )
    argumentos.add_argument("--aplicar", action="store_true", help="Escribe y elimina el detalle.")
    opciones = argumentos.parse_args()

    carpeta = Path(opciones.carpeta)
    datos = construir(carpeta)
    print(f"Fuera de Atlas: {datos['sesiones']} sesiones · {datos['con_compra']} con compra · "
          f"{datos['compras']} compras con categorías · {len(datos['pares'])} pares · "
          f"{len(datos['categorias'])} filas de categoría")

    ajustes = get_settings()
    cliente = MongoClient(ajustes.mongodb_uri, serverSelectionTimeoutMS=10_000, compressors="zlib")
    db = cliente[ajustes.mongodb_db]
    version = (db["marketing_estado"].find_one({"_id": "activo"}) or {}).get("version")
    if not version:
        print("No hay versión activa: no se compacta nada.", flush=True)
        cliente.close()
        return 1
    print("versión activa:", version)

    # Conciliación previa contra lo publicado.
    publicadas = 0
    if "marketing_sesiones" in db.list_collection_names():
        publicadas = db["marketing_sesiones"].count_documents({"version": version})
    if publicadas:
        publicadas = db["marketing_sesiones"].count_documents({"version": version})
        con_compra_bd = db["marketing_sesiones"].count_documents({"version": version, "compro": 1})
        print(f"En Atlas antes: {publicadas} sesiones · {con_compra_bd} con compra")
        if publicadas != datos["sesiones"] or con_compra_bd != datos["con_compra"]:
            print("Las cifras no coinciden: no se elimina el detalle.", flush=True)
            cliente.close()
            return 1

    if not opciones.aplicar:
        print("Modo simulación: usa --aplicar para escribir y eliminar el detalle.")
        cliente.close()
        return 0

    conteos = publicar(db, version, datos)
    print("agregados escritos:", conteos, flush=True)

    eliminados = {}
    for coleccion in DETALLE:
        if coleccion in db.list_collection_names():
            eliminados[coleccion] = db[coleccion].count_documents({"version": version})
            db.drop_collection(coleccion)
    print("detalle eliminado:", eliminados, flush=True)

    # Índices justificados del modelo compacto. Categorías pasa de 10 598 filas a
    # unas 500: el índice por categoría costaba más que la colección completa.
    db[FLUJO].create_index([("version", 1), ("fecha", 1), ("canal", 1)], name="flujo_version_fecha_canal")
    db[TIEMPOS].create_index([("version", 1), ("fecha", 1), ("transicion", 1)], name="tiempos_version_fecha_transicion")
    db[AFINIDAD].create_index([("version", 1), ("a", 1)], name="afinidad_version_a")
    indices_categorias = {i["name"] for i in db[CATEGORIAS].list_indexes()}
    if "marketing_categorias_version_categoria" in indices_categorias:
        db[CATEGORIAS].drop_index("marketing_categorias_version_categoria")
        print("índice retirado: marketing_categorias_version_categoria")
    # Usuarios: bastan los índices declarados en ``app/db/indexes.py``. Con el
    # detalle fuera, los índices por dispositivo, país y compra costaban más
    # espacio del que ahorraban sobre una colección de ~15 000 documentos.
    existentes = {i["name"] for i in db["marketing_usuarios"].list_indexes()}
    for nombre in (
        "marketing_usuarios_version_dispositivo_usuario",
        "marketing_usuarios_version_pais_usuario",
        "marketing_usuarios_version_usuario_compro",
    ):
        if nombre in existentes:
            db["marketing_usuarios"].drop_index(nombre)
            print("índice retirado:", nombre)

    st = db.command("dbStats", scale=1)
    print(f"base tras compactar: {st.get('objects')} docs · datos {st['dataSize']/1048576:.1f} MiB · "
          f"índices {st['indexSize']/1048576:.2f} MiB")
    cliente.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
