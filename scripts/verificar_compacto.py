"""Verifica el modelo compacto de marketing contra la copia original.

Tres comprobaciones, en orden:

1. **Agregados vs. original**: recalcula desde los CSV.gz del extracto los
   agregados que guarda Atlas y compara cifra por cifra (todas las filas y un
   rango de fechas concreto).
2. **Atlas vs. original**: suma los documentos publicados y comprueba que las
   métricas aditivas coinciden con el original dentro del mismo rango.
3. **API vs. Atlas**: pide a la API los mismos cortes y compara con la verdad
   calculada (sesiones del flujo, tramos del embudo, compras por categoría).

Uso::

    python -m scripts.verificar_compacto                # todos los cortes
    python -m scripts.verificar_compacto --desde 2020-12-01 --hasta 2021-01-31
    python -m scripts.verificar_compacto --sin-api      # solo Atlas

No escribe nada en Atlas: solo lee.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from itertools import combinations
from pathlib import Path

from pymongo import MongoClient

from app.config import get_settings
from app.marketing.ingesta import _canal

FLUJO = "marketing_flujo"
TIEMPOS = "marketing_tiempos"
AFINIDAD = "marketing_afinidad"
EMBUDO = "marketing_embudo"
RESUMEN = "marketing_resumen"
CATEGORIAS = "marketing_categorias"

TRANSICIONES = {1: "vista_carrito", 2: "carrito_pago", 3: "pago_compra"}
TRAMOS = (0, 0.25, 0.5, 1, 2, 4, 8, 24)
API = "http://127.0.0.1:8010/api/v1/marketing"


def _leer(carpeta: Path, nombre: str):
    with gzip.open(carpeta / f"{nombre}.csv.gz", "rt", encoding="utf-8", newline="") as archivo:
        for fila in csv.DictReader(archivo):
            yield fila


def _tramo(horas: float) -> float:
    anterior = TRAMOS[0]
    for limite in TRAMOS[1:]:
        if horas < limite:
            return float(anterior)
        anterior = limite
    return float(TRAMOS[-1])


def verdad(carpeta: Path, desde: str | None, hasta: str | None) -> dict:
    """Agregados de referencia calculados desde los CSV del extracto."""
    flujo: Counter = Counter()
    distintos: Counter = Counter()
    tiempos: Counter = Counter()
    sesiones = 0
    con_compra = 0
    for fila in _leer(carpeta, "sesiones"):
        fecha = fila.get("fecha")
        if desde and fecha < desde:
            continue
        if hasta and fecha > hasta:
            continue
        sesiones += 1
        canal = _canal(fila.get("fuente"), fila.get("medio"))
        dispositivo = fila.get("dispositivo") or "Sin información"
        pais = fila.get("pais") or "Sin información"
        compro = 1 if fila.get("t4") else 0
        con_compra += compro
        clave = (fecha, canal, dispositivo, pais, compro)
        flujo[clave] += 1
        if (fila.get("dispositivo_sesion") or dispositivo) != dispositivo:
            distintos[clave] += 1
        for paso, nombre in TRANSICIONES.items():
            inicio, fin = fila.get(f"t{paso}"), fila.get(f"t{paso + 1}")
            if not inicio or not fin:
                continue
            try:
                horas = (int(fin) - int(inicio)) / 3_600_000_000
            except (TypeError, ValueError):
                continue
            if horas >= 0:
                tiempos[(fecha, canal, dispositivo, pais, nombre, _tramo(horas))] += 1

    # Categorías y pares: una compra se atribuye a las dimensiones de su primera
    # línea, igual que en la compactación, para que sumar filas no duplique compras.
    lineas: dict[str, list[dict]] = {}
    for fila in _leer(carpeta, "articulos"):
        clave_compra = fila.get("clave_compra")
        if clave_compra:
            lineas.setdefault(clave_compra, []).append(fila)
    categorias: Counter = Counter()
    por_categoria: Counter = Counter()
    pares: Counter = Counter()
    for clave_compra in sorted(lineas):
        filas = sorted(
            lineas[clave_compra], key=lambda fila: (fila.get("fecha") or "", fila.get("session_id") or "")
        )
        primera = filas[0]
        dimensiones = (
            primera.get("fecha"),
            _canal(primera.get("fuente"), primera.get("medio")),
            primera.get("dispositivo") or "Sin información",
            primera.get("pais") or "Sin información",
        )
        fecha = dimensiones[0]
        if desde and fecha < desde:
            continue
        if hasta and fecha > hasta:
            continue
        distintas = {
            (fila.get("categoria") or "").strip() or "Sin información" for fila in filas
        }
        for categoria in distintas:
            categorias[(*dimensiones, categoria)] += 1
            por_categoria[categoria] += 1
        for a, b in combinations(sorted(distintas), 2):
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
    }


def _rango(desde: str | None, hasta: str | None) -> dict:
    rango = {}
    if desde:
        rango["$gte"] = desde
    if hasta:
        rango["$lte"] = hasta
    return {"fecha": rango} if rango else {}


def leer_atlas(db, version: str, desde: str | None, hasta: str | None) -> dict:
    consulta = {"version": version, **_rango(desde, hasta)}
    flujo: Counter = Counter()
    distintos: Counter = Counter()
    for fila in db[FLUJO].find(consulta, {"_id": 0}):
        clave = (
            fila.get("fecha"),
            fila.get("canal"),
            fila.get("dispositivo"),
            fila.get("pais"),
            int(fila.get("compro") or 0),
        )
        flujo[clave] += int(fila.get("sesiones") or 0)
        distintos_doc = int(fila.get("dispositivo_distinto") or 0)
        if distintos_doc:
            distintos[clave] += distintos_doc
    tiempos: Counter = Counter()
    for fila in db[TIEMPOS].find(consulta, {"_id": 0}):
        tiempos[
            (
                fila.get("fecha"),
                fila.get("canal"),
                fila.get("dispositivo"),
                fila.get("pais"),
                fila.get("transicion"),
                float(fila.get("tramo") or 0),
            )
        ] += int(fila.get("sesiones") or 0)
    return {"flujo": flujo, "distintos": distintos, "tiempos": tiempos}


def comparar(nombre: str, esperado: dict, obtenido: dict, tolerancia: int = 0) -> bool:
    """Compara dos diccionarios de conteos valor a valor."""
    faltan = {k: v for k, v in esperado.items() if obtenido.get(k, 0) != v}
    sobran = {k: v for k, v in obtenido.items() if k not in esperado}
    total_esperado = sum(esperado.values())
    total_obtenido = sum(obtenido.values())
    ok = not faltan and not sobran and abs(total_esperado - total_obtenido) <= tolerancia
    marca = "OK  " if ok else "FALLA"
    print(
        f"[{marca}] {nombre}: {total_obtenido} (esperado {total_esperado}) · "
        f"{len(obtenido)} claves (esperado {len(esperado)})"
    )
    if faltan:
        ejemplo = list(faltan.items())[:3]
        print(f"        diferencias: {ejemplo}")
    if sobran:
        ejemplo = list(sobran.items())[:3]
        print(f"        claves de más: {ejemplo}")
    return ok


def api(ruta: str) -> dict:
    with urllib.request.urlopen(f"{API}{ruta}", timeout=180) as respuesta:
        return json.load(respuesta)


def _consulta(desde: str | None, hasta: str | None, **dimensiones: str | None) -> str:
    """Cadena de consulta de la API (nombres singulares: canal, dispositivo, pais)."""
    alias = {"canales": "canal", "dispositivos": "dispositivo", "paises": "pais"}
    partes = []
    if desde:
        partes.append(f"desde={desde}")
    if hasta:
        partes.append(f"hasta={hasta}")
    for clave, valor in dimensiones.items():
        if valor:
            partes.append(f"{alias.get(clave, clave)}={urllib.parse.quote(valor)}")
    return "?" + "&".join(partes) if partes else ""


def verdad_dimensiones(
    carpeta: Path,
    desde: str | None,
    hasta: str | None,
    canales: list[str] | None = None,
    dispositivos: list[str] | None = None,
    paises: list[str] | None = None,
) -> dict:
    """Sesiones y usuarios distintos del extracto bajo los mismos filtros."""

    def coincide(fila: dict) -> bool:
        fecha = fila.get("fecha")
        if desde and fecha < desde:
            return False
        if hasta and fecha > hasta:
            return False
        if canales and _canal(fila.get("fuente"), fila.get("medio")) not in canales:
            return False
        if dispositivos and (fila.get("dispositivo") or "Sin información") not in dispositivos:
            return False
        if paises and (fila.get("pais") or "Sin información") not in paises:
            return False
        return True

    sesiones = sum(1 for fila in _leer(carpeta, "sesiones") if coincide(fila))
    usuarios: set[str] = set()
    compradores: set[str] = set()
    for fila in _leer(carpeta, "usuarios"):
        if not coincide(fila):
            continue
        usuario = fila.get("user_pseudo_id")
        if usuario:
            usuarios.add(usuario)
            if int(fila.get("compro") or 0):
                compradores.add(usuario)
    return {"sesiones": sesiones, "usuarios": len(usuarios), "compradores": len(compradores)}


def _filtrar(conteos: dict, canales, dispositivos, paises, campos: tuple[int, int, int]) -> dict:
    """Restringe un agregado (canal, dispositivo, país) a los valores elegidos."""
    i_canal, i_disp, i_pais = campos
    salida = {}
    for clave, valor in conteos.items():
        if canales and clave[i_canal] not in canales:
            continue
        if dispositivos and clave[i_disp] not in dispositivos:
            continue
        if paises and clave[i_pais] not in paises:
            continue
        salida[clave] = valor
    return salida


def verificar_api(
    carpeta: Path,
    desde: str | None,
    hasta: str | None,
    canales: list[str] | None = None,
    dispositivos: list[str] | None = None,
    paises: list[str] | None = None,
) -> bool:
    """Contrasta la API contra la verdad del extracto, con y sin dimensiones."""
    sufijo = _consulta(
        desde,
        hasta,
        paises=(paises or [None])[0],
        dispositivos=(dispositivos or [None])[0],
        canales=(canales or [None])[0],
    )
    etiqueta = sufijo or " (sin filtros)"
    ok = True

    valor = verdad(carpeta, desde, hasta)
    flujo = _filtrar(valor["flujo"], canales, dispositivos, paises, (1, 2, 3))
    tiempos = _filtrar(valor["tiempos"], canales, dispositivos, paises, (1, 2, 3))
    distintos = _filtrar(valor["distintos"], canales, dispositivos, paises, (1, 2, 3))

    # 1. Tramos de tiempo entre etapas: la base de cada paso sale del agregado.
    conversion = api(f"/conversion{sufijo}")
    esperado = {paso: 0 for paso in (1, 2, 3)}
    for clave, sesiones in tiempos.items():
        nombre = clave[4]
        if nombre in TRANSICIONES.values():
            esperado[list(TRANSICIONES.values()).index(nombre) + 1] += sesiones
    bases = {fila["paso"]: fila["base"] for fila in conversion.get("transiciones") or []}
    iguales = all(bases.get(paso, 0) == esperado[paso] for paso in esperado)
    print(f"[{'OK  ' if iguales else 'FALLA'}] API tramos{etiqueta}: {bases} · archivo {esperado}")
    ok = ok and iguales

    tramos = [
        tramo
        for fila in conversion.get("transiciones") or []
        for tramo in fila.get("tramos") or []
    ]
    sin_etiqueta = [t for t in tramos if t.get("horas") is None]
    print(
        f"[{'OK  ' if not sin_etiqueta else 'FALLA'}] API etiquetas de tramo{etiqueta}: "
        f"{sorted({(t['horas'], t['tramo']) for t in tramos if t.get('horas') is not None})}"
    )
    ok = ok and not sin_etiqueta

    # 2. Flujo de sesiones (sankey del resumen): total, con compra y dispositivo.
    resumen = api(f"/resumen{sufijo}")
    sankey = resumen.get("sankey") or {}
    total_flujo = sum(flujo.values())
    con_compra = sum(s for clave, s in flujo.items() if clave[4])
    cuadra = (
        int(sankey.get("total_sesiones") or 0) == total_flujo
        and int(sankey.get("sesiones_con_compra") or 0) == con_compra
    )
    print(
        f"[{'OK  ' if cuadra else 'FALLA'}] API flujo{etiqueta}: "
        f"{sankey.get('total_sesiones')} sesiones / {sankey.get('sesiones_con_compra')} con compra · "
        f"archivo {total_flujo} / {con_compra}"
    )
    ok = ok and cuadra

    otros = sum(distintos.values())
    print(
        f"[{'OK  ' if int(sankey.get('sesiones_dispositivo_distinto') or 0) == otros else 'FALLA'}] "
        f"API sesiones con otro dispositivo{etiqueta}: "
        f"{sankey.get('sesiones_dispositivo_distinto')} · archivo {otros}"
    )
    ok = ok and int(sankey.get("sesiones_dispositivo_distinto") or 0) == otros

    # 3. Métricas del rango: usuarios distintos y sesiones (no aditivas incluidas).
    indicadores = resumen.get("indicadores") or {}
    esperado_dim = verdad_dimensiones(carpeta, desde, hasta, canales, dispositivos, paises)
    for clave, esperado_valor in esperado_dim.items():
        obtenido = int((indicadores.get(clave) or {}).get("valor") or 0)
        igual = obtenido == esperado_valor
        print(
            f"[{'OK  ' if igual else 'FALLA'}] API {clave}{etiqueta}: {obtenido} · archivo {esperado_valor}"
        )
        ok = ok and igual

    # 4. Categorías: compras distintas que incluyen la categoría (se atribuyen a la
    # fecha y dimensiones de la primera línea de la compra).
    categorias_filtradas = _filtrar(valor["categorias"], canales, dispositivos, paises, (1, 2, 3))
    esperado_categorias: Counter = Counter()
    for clave, compras in categorias_filtradas.items():
        esperado_categorias[clave[4]] += compras
    productos = api(f"/productos{sufijo}{'&' if sufijo else '?'}limite=10")
    categorias_api = {
        fila["categoria"]: int(fila.get("compras") or 0)
        for fila in (productos.get("categorias") or [])
    }
    mismos = all(
        esperado_categorias.get(categoria) == compras
        for categoria, compras in categorias_api.items()
    ) and categorias_api.keys() <= set(esperado_categorias)
    print(
        f"[{'OK  ' if mismos else 'FALLA'}] API categorías{etiqueta}: {categorias_api} · "
        f"archivo {dict(esperado_categorias.most_common(5))}"
    )
    ok = ok and mismos
    return ok


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Verifica el modelo compacto de marketing")
    argumentos.add_argument("--carpeta", default="data/marketing/muestra5")
    argumentos.add_argument("--desde", default="2020-12-01")
    argumentos.add_argument("--hasta", default="2021-01-31")
    argumentos.add_argument("--sin-api", action="store_true")
    opciones = argumentos.parse_args()

    carpeta = Path(opciones.carpeta)
    ajustes = get_settings()
    cliente = MongoClient(ajustes.mongodb_uri, serverSelectionTimeoutMS=15_000, compressors="zlib")
    db = cliente[ajustes.mongodb_db]
    version = (db["marketing_estado"].find_one({"_id": "activo"}) or {}).get("version")
    if not version:
        print("No hay versión activa en Atlas.")
        cliente.close()
        return 1
    print(f"versión activa: {version} · copia original: {carpeta}")

    ok = True
    for desde, hasta, etiqueta in ((None, None, "todo el periodo"), (opciones.desde, opciones.hasta, "rango filtrado")):
        print(f"\n== {etiqueta} ==")
        esperado = verdad(carpeta, desde, hasta)
        obtenido = leer_atlas(db, version, desde, hasta)
        ok &= comparar("flujo (canal × dispositivo × país × compra)", esperado["flujo"], obtenido["flujo"])
        ok &= comparar(
            "sesiones con dispositivo distinto", esperado["distintos"], obtenido["distintos"]
        )
        ok &= comparar("tiempos entre etapas", esperado["tiempos"], obtenido["tiempos"])

        # Categorías por compra, tal como quedan publicadas en Atlas.
        categorias_bd = Counter()
        for fila in db[CATEGORIAS].find(
            {"version": version, **_rango(desde, hasta)}, {"_id": 0, "compras": 1, "fecha": 1,
             "canal": 1, "dispositivo": 1, "pais": 1, "categoria": 1}
        ):
            clave = (
                fila.get("fecha"),
                fila.get("canal"),
                fila.get("dispositivo"),
                fila.get("pais"),
                fila.get("categoria"),
            )
            categorias_bd[clave] += int(fila.get("compras") or 0)
        ok &= comparar("categorías por compra", esperado["categorias"], categorias_bd)

        # Métricas derivadas que el tablero declara.
        sesiones_bd = sum(int(f.get("sesiones") or 0) for f in db[RESUMEN].find(
            {"version": version, **_rango(desde, hasta)}, {"sesiones": 1}
        ))
        compra_bd = sum(
            sesiones
            for (_f, _c, _d, _p, compro), sesiones in obtenido["flujo"].items()
            if compro
        )
        print(
            f"[{'OK  ' if sesiones_bd == esperado['sesiones'] else 'FALLA'}] resumen.sesiones: "
            f"{sesiones_bd} · archivo {esperado['sesiones']}"
        )
        ok &= sesiones_bd == esperado["sesiones"]
        print(
            f"[{'OK  ' if compra_bd == esperado['con_compra'] else 'FALLA'}] flujo con compra: "
            f"{compra_bd} · archivo {esperado['con_compra']}"
        )
        ok &= compra_bd == esperado["con_compra"]

        if not opciones.sin_api:
            ok &= verificar_api(carpeta, desde, hasta)
            # Mismo corte con dimensiones: comprueba que los agregados responden a
            # los filtros del tablero y que el conteo de usuarios distintos sigue
            # siendo exacto (no es aditivo: no se pueden sumar los días).
            ok &= verificar_api(
                carpeta, desde, hasta, dispositivos=["desktop"], paises=["United States"]
            )

    cliente.close()
    print("\nRESULTADO:", "todo conciliado" if ok else "hay diferencias (ver arriba)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
