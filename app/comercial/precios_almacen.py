"""Lectura de precios semanales compatible con los dos esquemas de almacenamiento.

La base local guarda los precios como **un documento por ``store_id × wm_yr_wk``** con dos listas
paralelas (``items`` y ``precios`` en ``float``). El paquete de publicación usa el mismo grano pero
en formato compacto: un catálogo por tienda con el orden estable de artículos y, por semana, un
arreglo binario de **centavos enteros** (``int32``) con la misma longitud que el catálogo, donde
``0`` significa «sin precio en esa semana».

Por qué es seguro:

* los precios del archivo tienen dos decimales, así que ``round(precio × 100)`` es exacto y no se
  pierde precisión al compararlos con la fuente;
* el precio de venta es siempre positivo (la importación lo valida), de modo que ``0`` no puede
  confundirse con un precio real: la ausencia se sigue representando como ausencia, nunca como cero;
* el catálogo por tienda evita repetir ~2 500 identificadores en cada una de las 282 semanas.

Este módulo es la única puerta de lectura: ``precios_analisis`` y ``exploracion`` piden los precios
aquí, y así la API funciona igual contra la base local y contra la base de publicación.
"""

from __future__ import annotations

from typing import Any

from pymongo.database import Database

from app.db.mongo import M5_PRECIOS, M5_PRECIOS_CATALOGO


def _a_precios(documento: dict[str, Any], catalogo: list[str] | None) -> dict[str, float]:
    """Traduce un documento de semana a ``{item_id: precio}`` con los dos esquemas."""
    centavos = documento.get("centavos")
    if centavos is not None:
        if not catalogo:
            return {}
        # ``centavos`` es un arreglo binario int32 alineado al catálogo de la tienda.
        valores = memoryview(centavos).cast("i")
        return {
            item: valor / 100.0
            for item, valor in zip(catalogo, valores, strict=False)
            if valor > 0
        }
    articulos = documento.get("items") or []
    precios = documento.get("precios") or []
    return {item: float(precio) for item, precio in zip(articulos, precios, strict=False)}


def catalogo_tienda(db: Database, version_precios: str, tienda: str) -> list[str] | None:
    """Orden estable de artículos de la tienda en el esquema compacto (``None`` si no existe)."""
    documento = db[M5_PRECIOS_CATALOGO].find_one(
        {"version_precios": version_precios, "store_id": tienda}, {"items": 1, "_id": 0}
    )
    if not documento:
        return None
    return list(documento.get("items") or [])


def semanas_tienda(db: Database, version_precios: str, tienda: str) -> dict[int, dict[str, float]]:
    """``wm_yr_wk → {item_id: precio}`` de una tienda, en cualquiera de los dos esquemas.

    Un artículo ausente de una semana no aparece en ese diccionario: la ausencia de precio no se
    representa con cero.
    """
    catalogo = catalogo_tienda(db, version_precios, tienda)
    proyeccion = {"wm_yr_wk": 1, "centavos": 1, "items": 1, "precios": 1, "version_ventas": 1, "_id": 0}
    semanas: dict[int, dict[str, float]] = {}
    for documento in db[M5_PRECIOS].find(
        {"version_precios": version_precios, "store_id": tienda}, proyeccion
    ):
        semanas[int(documento["wm_yr_wk"])] = _a_precios(documento, catalogo)
    return semanas


def precios_por_semana(
    db: Database,
    version_precios: str,
    tienda: str,
    semanas: list[int],
) -> dict[int, dict[str, float]]:
    """Precios de un conjunto de semanas de una tienda, en cualquiera de los dos esquemas."""
    if not semanas:
        return {}
    catalogo = catalogo_tienda(db, version_precios, tienda)
    proyeccion = {"wm_yr_wk": 1, "centavos": 1, "items": 1, "precios": 1, "_id": 0}
    salida: dict[int, dict[str, float]] = {}
    for documento in db[M5_PRECIOS].find(
        {
            "version_precios": version_precios,
            "store_id": tienda,
            "wm_yr_wk": {"$in": sorted(int(valor) for valor in semanas)},
        },
        proyeccion,
    ):
        salida[int(documento["wm_yr_wk"])] = _a_precios(documento, catalogo)
    return salida
