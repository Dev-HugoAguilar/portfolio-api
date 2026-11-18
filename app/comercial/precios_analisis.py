"""Análisis de precios de venta de M5 (una tienda a la vez).

Qué calcula
-----------
A partir de ``m5_precios`` (un documento por ``store_id × wm_yr_wk`` con las listas
paralelas ``items`` y ``precios``) y de ``m5_calendario``, este módulo responde
cuatro preguntas, siempre en el grano ``store_id × item_id × wm_yr_wk``:

- **distribución** del precio medio por artículo en el periodo (cuartiles, media,
  cajas por categoría y el detalle artículo a artículo);
- **cambios** de precio entre semanas consecutivas *con precio disponible*
  (aumentos, disminuciones y semanas sin cambio, comparando en centavos enteros);
- **evolución** semanal del precio de un artículo concreto;
- **resumen** de cabecera para la pestaña de precios.

Qué NO hace
-----------
- **No calcula ingresos**: nunca multiplica precio por unidades vendidas. Este
  módulo solo mira precios.
- **No habla de promociones**: M5 no trae campañas ni descuentos declarados. Aquí
  solo existen «aumento», «disminución» y «sin cambio» de precio; una disminución
  de precio no es una promoción.
- **No imputa precios**: si un artículo no aparece en la lista de una semana, no
  tiene precio esa semana (no vale cero). La ausencia se conserva como ausencia y
  se publica como ``None``.

Reglas de cálculo
-----------------
- **Ponderación por días cubiertos**: el precio medio de un artículo es la media de
  sus precios semanales ponderada por los días del periodo que caen en cada semana
  (según ``m5_calendario``), **nunca** por unidades vendidas. Una semana sin precio
  aporta 0 al numerador y 0 al denominador, así que no arrastra el promedio.
- **Transiciones**: se ordenan las semanas cronológicamente y se comparan las
  semanas **consecutivas que tienen precio**. Un hueco sin precio no es una
  transición: la siguiente semana con precio se compara contra la última que tuvo
  precio.
- **Cambios**: se comparan en **centavos enteros** (el origen trae dos decimales), así
  que un cambio de un centavo cuenta como aumento o disminución. Un umbral comercial
  opcional (``UMBRAL_COMERCIAL_USD``, 0 por omisión) se aplica sobre esa diferencia y se
  declara en la respuesta; nunca se usa para tapar el error de representación.
- **Duplicados**: antes de calcular nada se verifica que no haya dos precios para el
  mismo ``store_id × item_id × wm_yr_wk``; si los hay se aborta con ``ErrorPrecios``.

Rendimiento
-----------
Una sola lectura por tienda (282 documentos) con proyección de las claves
necesarias; el resto del trabajo es vectorizado con numpy sobre una matriz
``semanas × artículos`` con ``NaN`` donde falta el precio.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
from pymongo.database import Database

from app.comercial import precios_almacen
from app.comercial.precios import ErrorPrecios
from app.db.mongo import M5_CALENDARIO, M5_CATALOGO, M5_PRECIOS

UNIDAD = "USD por unidad"
"""Unidad de todos los precios que devuelve este módulo (dólares por unidad)."""

REGLA_PONDERACION = (
    "media del precio semanal ponderada por los días del periodo cubiertos por cada "
    "semana; no se pondera por unidades vendidas"
)
"""Texto que se publica junto a la distribución para explicar la ponderación."""

UMBRAL_COMERCIAL_USD = 0.0
"""Umbral comercial **opcional** (USD) para considerar relevante un cambio de precio.

Por omisión es ``0``: cualquier diferencia de un centavo cuenta como cambio real. No es una
tolerancia contra el error de representación; ese ruido se elimina comparando en **centavos
enteros** (``CENTAVOS_POR_USD``), porque el origen trae dos decimales.
"""

CENTAVOS_POR_USD = 100
"""Se compara en centavos enteros: ``round(precio × 100)``."""

PRECISION_ORIGEN = "dos decimales en sell_prices.csv; las diferencias se calculan en centavos enteros"
"""Precisión validada del origen y método de comparación."""

TIPO_UMBRAL = "umbral comercial opcional en USD sobre la diferencia en centavos"
"""Cómo se interpreta ``UMBRAL_COMERCIAL_USD``."""

ETIQUETAS_CATEGORIA = {"FOODS": "Alimentos", "HOBBIES": "Aficiones", "HOUSEHOLD": "Hogar"}
"""Nombre legible de cada ``cat_id`` del catálogo M5."""

SIN_CATEGORIA = "DESCONOCIDA"
"""Clave con la que se agrupan los artículos sin categoría en el catálogo."""

ETIQUETA_SIN_CATEGORIA = "Sin categoría en el catálogo"
"""Etiqueta legible de ``SIN_CATEGORIA``."""

MAXIMO_SERIES = 40
"""Cuántas series (item × tienda) se publican con su detalle en ``cambios``."""

NOTA_SIN_PROMOCIONES = "una disminución de precio no es una promoción: M5 no trae campañas"
"""Aclaración que acompaña a los conteos de cambios."""

DECIMALES = 4
"""Decimales con los que se publican los precios calculados."""


# --------------------------------------------------------------------------- #
# Lectura de datos
# --------------------------------------------------------------------------- #


def _leer_tienda_detalle(db: Database, version_precios: str, tienda: str) -> dict:
    """Lee en una sola consulta los precios de una tienda, semana a semana.

    Devuelve ``{"semanas": {wm_yr_wk: {item_id: precio}}, "version_ventas": str|None,
    "documentos": n}``. Detecta y reporta claves repetidas: dos documentos para la
    misma semana o dos precios para el mismo artículo dentro de una semana.
    """
    proyeccion = {"wm_yr_wk": 1, "version_ventas": 1, "_id": 0}
    semanas: dict[int, dict[str, float]] = {}
    versiones: set[str] = set()
    documentos = 0

    # La lectura delega en ``precios_almacen``: entiende el esquema local (listas paralelas en
    # float) y el compacto del paquete de publicación (catálogo + centavos binarios).
    for documento in db[M5_PRECIOS].find(
        {"version_precios": version_precios, "store_id": tienda}, proyeccion
    ):
        documentos += 1
        semana = int(documento["wm_yr_wk"])
        if semana in semanas:
            raise ErrorPrecios(
                f"Hay más de un documento de precios para la tienda {tienda} en la semana {semana}"
            )
        version = documento.get("version_ventas")
        if version:
            versiones.add(str(version))

    semanas = precios_almacen.semanas_tienda(db, version_precios, tienda)

    if not semanas:
        raise ErrorPrecios(
            f"No hay precios importados para la tienda {tienda} en la versión {version_precios}"
        )
    if len(versiones) > 1:
        raise ErrorPrecios(
            f"Los documentos de precios de la tienda {tienda} declaran más de una versión de "
            f"ventas: {sorted(versiones)}"
        )
    return {
        "semanas": semanas,
        "version_ventas": next(iter(versiones), None),
        "documentos": documentos,
    }


def _leer_tienda(db: Database, version_precios: str, tienda: str) -> dict[int, dict[str, float]]:
    """``wm_yr_wk → {item_id: precio}`` de una tienda, con validación de duplicados.

    Un artículo ausente de una semana simplemente no aparece en ese diccionario: la
    ausencia de precio no se representa con cero.
    """
    return _leer_tienda_detalle(db, version_precios, tienda)["semanas"]


def _matriz_tienda(
    lectura: dict[int, dict[str, float]]
) -> tuple[list[str], dict[str, int], np.ndarray, np.ndarray]:
    """Convierte las lecturas de la tienda en una matriz ``semanas × artículos``.

    Devuelve los nombres de artículo, su posición, las semanas ordenadas y la matriz
    en ``float64`` con ``NaN`` donde el artículo no tiene precio esa semana. La
    matriz se indexa como ``matriz[posición_semana, posición_artículo]``.
    """
    semanas = np.array(sorted(lectura), dtype=np.int64)
    articulos: dict[str, int] = {}
    for precios in lectura.values():
        for item in precios:
            articulos.setdefault(item, len(articulos))
    nombres = list(articulos)
    posicion_de = articulos.__getitem__
    matriz = np.full((semanas.size, len(nombres)), np.nan, dtype=np.float64)
    for fila, semana in enumerate(semanas.tolist()):
        precios = lectura[int(semana)]
        indices = np.fromiter(map(posicion_de, precios), dtype=np.int64, count=len(precios))
        valores = np.fromiter(precios.values(), dtype=np.float64, count=len(precios))
        matriz[fila, indices] = valores
    return nombres, articulos, semanas, matriz


def _calendario_periodo(db: Database, desde: date, hasta: date) -> dict[int, dict]:
    """Días del periodo que cubre cada ``wm_yr_wk``, con su primera y última fecha.

    Devuelve ``{wm_yr_wk: {"dias": n, "inicio": date, "fin": date}}`` ordenado por
    semana. Los pesos de la ponderación salen de ``dias``; ``inicio`` y ``fin`` se
    usan para publicar los extremos (posiblemente parciales) de cada semana.
    """
    inicio = datetime(desde.year, desde.month, desde.day)
    fin = datetime(hasta.year, hasta.month, hasta.day)
    calendario: dict[int, dict] = {}
    consulta = {"fecha": {"$gte": inicio, "$lte": fin}}
    for documento in db[M5_CALENDARIO].find(consulta, {"wm_yr_wk": 1, "fecha": 1, "_id": 0}):
        semana = documento.get("wm_yr_wk")
        fecha = documento.get("fecha")
        if semana is None or fecha is None:
            continue
        dia = fecha.date() if isinstance(fecha, datetime) else fecha
        datos = calendario.get(int(semana))
        if datos is None:
            calendario[int(semana)] = {"dias": 1, "inicio": dia, "fin": dia}
            continue
        datos["dias"] += 1
        if dia < datos["inicio"]:
            datos["inicio"] = dia
        if dia > datos["fin"]:
            datos["fin"] = dia
    return {semana: calendario[semana] for semana in sorted(calendario)}


def _dias_por_semana(db: Database, desde: date, hasta: date) -> dict[int, int]:
    """``wm_yr_wk → días del periodo que caen en esa semana`` (pesos de la media)."""
    calendario = _calendario_periodo(db, desde, hasta)
    return {semana: datos["dias"] for semana, datos in calendario.items()}


def _catalogo(db: Database, version_ventas: str | None) -> dict | None:
    """Catálogo de la versión de ventas.

    Devuelve ``{"productos": {item_id: (cat_id, dept_id)}, "etiquetas": {cat_id: texto}}``
    o ``None`` si no hay versión de ventas con la que consultarlo.
    """
    if not version_ventas:
        return None
    productos: dict[str, tuple[str, str]] = {}
    for documento in db[M5_CATALOGO].find(
        {"version": version_ventas, "tipo": "producto"},
        {"item_id": 1, "cat_id": 1, "dept_id": 1, "_id": 0},
    ):
        productos[documento["item_id"]] = (documento.get("cat_id"), documento.get("dept_id"))
    if not productos:
        return None
    etiquetas = dict(ETIQUETAS_CATEGORIA)
    for documento in db[M5_CATALOGO].find(
        {"version": version_ventas, "tipo": "categoria"}, {"cat_id": 1, "nombre": 1, "_id": 0}
    ):
        cat_id = documento.get("cat_id")
        if cat_id:
            etiquetas[cat_id] = ETIQUETAS_CATEGORIA.get(cat_id, documento.get("nombre") or cat_id)
    return {"productos": productos, "etiquetas": etiquetas}


def _etiqueta(etiquetas: dict[str, str], cat_id: str) -> str:
    """Nombre legible de una categoría (o su ``cat_id`` si no está catalogada)."""
    if cat_id == SIN_CATEGORIA:
        return ETIQUETA_SIN_CATEGORIA
    return etiquetas.get(cat_id, cat_id)


# --------------------------------------------------------------------------- #
# Base común: una lectura y una matriz por tienda y periodo
# --------------------------------------------------------------------------- #


def _base(
    db: Database,
    version_precios: str,
    *,
    tienda: str,
    desde: date,
    hasta: date,
    version_ventas: str | None = None,
) -> dict:
    """Lectura única por tienda con el periodo ya resuelto en arreglos.

    Junta lo que necesitan todas las funciones públicas: la matriz de precios de la
    tienda, las semanas del periodo (con sus pesos en días), el calendario y el
    catálogo. Así ninguna función lee dos veces los mismos documentos.
    """
    if desde > hasta:
        raise ValueError("El periodo es inválido: 'desde' no puede ser posterior a 'hasta'")

    detalle = _leer_tienda_detalle(db, version_precios, tienda)
    calendario = _calendario_periodo(db, desde, hasta)
    nombres, _, semanas_tienda, matriz = _matriz_tienda(detalle["semanas"])
    posicion_semana = {int(semana): fila for fila, semana in enumerate(semanas_tienda.tolist())}

    semanas_periodo = np.array(
        [semana for semana in calendario if semana in posicion_semana], dtype=np.int64
    )
    filas_periodo = np.array(
        [posicion_semana[int(semana)] for semana in semanas_periodo.tolist()], dtype=np.int64
    )
    dias = np.array(
        [calendario[int(semana)]["dias"] for semana in semanas_periodo.tolist()], dtype=np.float64
    )
    catalogo = _catalogo(db, version_ventas or detalle["version_ventas"])

    return {
        "tienda": tienda,
        "desde": desde,
        "hasta": hasta,
        "nombres": nombres,
        "matriz": matriz,
        "semanas_tienda": semanas_tienda.tolist(),
        "calendario": calendario,
        "semanas_periodo": semanas_periodo,
        "filas_periodo": filas_periodo,
        "dias": dias,
        "dias_totales": int(dias.sum()),
        "catalogo": catalogo,
        "etiquetas": (catalogo or {}).get("etiquetas", dict(ETIQUETAS_CATEGORIA)),
    }


def _periodo(base: dict) -> dict:
    """Bloque ``periodo`` que comparten todas las respuestas."""
    return {
        "desde": base["desde"].isoformat(),
        "hasta": base["hasta"].isoformat(),
        "dias": base["dias_totales"],
    }


def _alcance(
    base: dict,
    *,
    categorias: list[str] | None = None,
    departamentos: list[str] | None = None,
    items_permitidos: list[str] | None = None,
) -> np.ndarray:
    """Máscara booleana de los artículos que entran al análisis.

    ``items_permitidos`` restringe a una lista explícita; ``categorias`` y
    ``departamentos`` se resuelven con el catálogo. Si el catálogo no está
    disponible, esos dos filtros no se aplican (no hay con qué resolverlos).
    """
    total = len(base["nombres"])
    alcance = np.ones(total, dtype=bool)
    productos = (base["catalogo"] or {}).get("productos")
    if productos:
        if categorias:
            permitidas = set(categorias)
            alcance &= np.fromiter(
                (
                    (productos.get(item, (None, None))[0] in permitidas)
                    for item in base["nombres"]
                ),
                dtype=bool,
                count=total,
            )
        if departamentos:
            permitidos = set(departamentos)
            alcance &= np.fromiter(
                (
                    (productos.get(item, (None, None))[1] in permitidos)
                    for item in base["nombres"]
                ),
                dtype=bool,
                count=total,
            )
    if items_permitidos is not None:
        permitidos_items = set(items_permitidos)
        alcance &= np.fromiter(
            (item in permitidos_items for item in base["nombres"]), dtype=bool, count=total
        )
    return alcance


def _medios(base: dict, alcance: np.ndarray) -> dict:
    """Precio medio ponderado por días cubiertos para cada artículo del alcance.

    El numerador suma ``precio_semana × días_de_esa_semana_en_el_periodo`` y el
    denominador suma esos mismos días. Un artículo sin precio en una semana aporta 0
    a los dos, así que su media no se falsea.
    """
    columnas = np.flatnonzero(alcance)
    if columnas.size == 0 or base["filas_periodo"].size == 0:
        return {
            "columnas": columnas,
            "items": [],
            "medios": np.zeros(0, dtype=np.float64),
            "dias_cubiertos": np.zeros(0, dtype=np.int64),
            "semanas_con_precio": np.zeros(0, dtype=np.int64),
        }

    submatriz = base["matriz"][np.ix_(base["filas_periodo"], columnas)]
    pesos = base["dias"]
    presente = ~np.isnan(submatriz)
    dias_cubiertos = presente.T @ pesos
    numerador = np.nansum(submatriz * pesos[:, None], axis=0)
    medios = np.full(columnas.size, np.nan, dtype=np.float64)
    np.divide(numerador, dias_cubiertos, out=medios, where=dias_cubiertos > 0)

    nombres = base["nombres"]
    nombres_alcance = [nombres[int(columna)] for columna in columnas.tolist()]
    orden = np.array(sorted(range(len(nombres_alcance)), key=nombres_alcance.__getitem__))
    return {
        "columnas": columnas[orden],
        "items": [nombres_alcance[int(posicion)] for posicion in orden.tolist()],
        "medios": medios[orden],
        "dias_cubiertos": dias_cubiertos.astype(np.int64)[orden],
        "semanas_con_precio": presente.sum(axis=0).astype(np.int64)[orden],
    }


def _catalogo_item(base: dict, item: str) -> tuple[str, str | None]:
    """``(cat_id, dept_id)`` de un artículo; ``DESCONOCIDA``/``None`` sin catálogo."""
    productos = (base["catalogo"] or {}).get("productos")
    if not productos:
        return SIN_CATEGORIA, None
    return productos.get(item, (SIN_CATEGORIA, None))


def _estadisticas(valores: np.ndarray) -> dict:
    """Mínimo, cuartiles (interpolación lineal), mediana y máximo de un arreglo."""
    if valores.size == 0:
        return {"min": None, "q1": None, "mediana": None, "q3": None, "max": None}
    q1, mediana, q3 = np.percentile(valores, [25, 50, 75])
    return {
        "min": round(float(valores.min()), DECIMALES),
        "q1": round(float(q1), DECIMALES),
        "mediana": round(float(mediana), DECIMALES),
        "q3": round(float(q3), DECIMALES),
        "max": round(float(valores.max()), DECIMALES),
    }


# --------------------------------------------------------------------------- #
# Transiciones de precio
# --------------------------------------------------------------------------- #


def _a_centavos(valores: np.ndarray) -> np.ndarray:
    """Convierte USD a centavos enteros (``NaN`` se conserva).

    El origen publica dos decimales, así que multiplicar por 100 y redondear elimina el
    ruido de representación binaria (2,68 → 267,99999… → 268) sin tocar diferencias reales
    de un centavo.
    """
    return np.rint(np.asarray(valores, dtype=np.float64) * CENTAVOS_POR_USD)


def _transiciones(submatriz: np.ndarray, umbral_usd: float = UMBRAL_COMERCIAL_USD) -> dict:
    """Cuenta aumentos, disminuciones y «sin cambio» por artículo, en centavos enteros.

    Compara cada semana con precio contra la **última anterior con precio**: un hueco no
    genera transición. La diferencia se calcula en centavos, de modo que un cambio de
    ``+0,01`` o ``−0,01`` cuenta como aumento o disminución. ``umbral_usd`` es un umbral
    **comercial** opcional (por omisión 0) aplicado sobre esa diferencia; nunca se usa para
    tapar el error de representación.
    """
    filas, columnas = submatriz.shape
    umbral_centavos = int(round(float(umbral_usd) * CENTAVOS_POR_USD))
    validos = ~np.isnan(submatriz)
    semanas_con_precio = validos.sum(axis=0)
    if filas < 2:
        ceros = np.zeros(columnas, dtype=np.int64)
        return {
            "aumentos": ceros,
            "disminuciones": ceros.copy(),
            "sin_cambio": ceros.copy(),
            "transiciones": ceros.copy(),
            "semanas_con_precio": semanas_con_precio,
            "primera_fila": np.full(columnas, -1, dtype=np.int64),
            "ultima_fila": np.full(columnas, -1, dtype=np.int64),
        }

    centavos = _a_centavos(submatriz)
    indices = np.where(validos, np.arange(filas, dtype=np.int64)[:, None], -1)
    ultimo = np.maximum.accumulate(indices, axis=0)
    previo = np.vstack([np.full((1, columnas), -1, dtype=np.int64), ultimo[:-1, :]])
    columnas_idx = np.arange(columnas, dtype=np.int64)
    anterior = np.where(previo >= 0, centavos[np.clip(previo, 0, None), columnas_idx], np.nan)
    comparable = validos & (previo >= 0)
    diferencias = np.where(comparable, centavos - anterior, np.nan)

    aumentos = (diferencias > umbral_centavos).sum(axis=0)
    disminuciones = (diferencias < -umbral_centavos).sum(axis=0)
    sin_cambio = (np.abs(diferencias) <= umbral_centavos).sum(axis=0)
    return {
        "aumentos": aumentos,
        "disminuciones": disminuciones,
        "sin_cambio": sin_cambio,
        "transiciones": aumentos + disminuciones + sin_cambio,
        "semanas_con_precio": semanas_con_precio,
        "primera_fila": np.where(semanas_con_precio > 0, validos.argmax(axis=0), -1),
        "ultima_fila": np.where(
            semanas_con_precio > 0, filas - 1 - validos[::-1, :].argmax(axis=0), -1
        ),
    }


def _cobertura_combinaciones(
    con_precio: np.ndarray, dias_cubiertos: np.ndarray, dias_intervalo: int
) -> dict:
    """Desglose mutuamente excluyente de la cobertura de precio por combinación.

    Los tres estados suman el universo: cobertura completa (todos los días del intervalo
    con precio), cobertura parcial (algún día con precio) y sin precio en el intervalo.
    ``pct_con_algun_precio`` mide cuántas combinaciones tienen **al menos** un precio y no
    debe leerse como cobertura de días.
    """
    completa = con_precio & (dias_cubiertos >= dias_intervalo)
    parcial = con_precio & (dias_cubiertos < dias_intervalo)
    sin_precio = ~con_precio
    total = int(con_precio.size)
    dias_posibles = total * dias_intervalo
    dias_con_precio = int(dias_cubiertos[con_precio].sum()) if total else 0
    mediana = int(np.median(dias_cubiertos[con_precio])) if con_precio.any() else 0
    return {
        "completa": int(completa.sum()),
        "parcial": int(parcial.sum()),
        "sin_precio": int(sin_precio.sum()),
        "pct_completa": round(100 * int(completa.sum()) / total, 2) if total else None,
        "pct_con_algun_precio": round(100 * int(con_precio.sum()) / total, 2) if total else None,
        "dias_del_intervalo": int(dias_intervalo),
        "dias_cubiertos_total": dias_con_precio,
        "dias_posibles_total": dias_posibles,
        "pct_dias_cubiertos": (
            round(100 * dias_con_precio / dias_posibles, 2) if dias_posibles else None
        ),
        "dias_cubiertos_mediana": mediana,
        "nota": (
            "«Con algún precio» no es cobertura completa: completa + parcial + sin precio "
            "suma el universo de combinaciones, y el porcentaje de días usa días con precio ÷ "
            "(combinaciones × días del intervalo)."
        ),
    }


def _sin_precio_detalle(base: dict, medios: dict) -> dict:
    """Explica las combinaciones sin precio dentro del periodo.

    No se asume falta de inventario: se cuenta cuántas tienen precio en **otras** semanas
    de la tienda (antes o después del intervalo), cuántas no tienen ningún precio en toda
    la historia importada y cuál es la última semana con precio de las que quedan fuera.

    Ojo con el orden: ``_medios`` devuelve ``items``, ``medios`` y ``columnas`` ordenados
    por ``item_id``, así que la columna de la matriz es ``medios["columnas"][posición]``.
    """
    sin_precio = np.isnan(medios["medios"])
    total = int(sin_precio.sum())
    if total == 0:
        return {
            "combinaciones": 0,
            "con_precio_fuera_del_intervalo": 0,
            "sin_precio_en_toda_la_tienda": 0,
            "semanas_totales_tienda": len(base.get("semanas_tienda") or []),
            "ultima_semana_con_precio_fuera": None,
            "nota": "Todas las combinaciones del alcance tienen precio en el intervalo.",
        }
    columnas = np.asarray(medios["columnas"], dtype=np.int64)[sin_precio]
    matriz = base["matriz"]
    fuera = ~np.isnan(matriz)
    fuera[base["filas_periodo"], :] = False
    con_fuera = fuera[:, columnas].any(axis=0)
    semanas_tienda = np.asarray(base.get("semanas_tienda") or [], dtype=np.int64)
    ultima = None
    if semanas_tienda.size and con_fuera.any():
        posiciones = np.where(fuera[:, columnas], np.arange(fuera.shape[0])[:, None], -1)
        ultima_fila = posiciones.max(axis=0)
        # El máximo se toma por artículo (no global): la semana más tardía de las que
        # tienen precio fuera del intervalo, acotada al propio artículo.
        maximos = ultima_fila[con_fuera]
        if maximos.size:
            ultima = int(semanas_tienda[int(maximos.max())])
    return {
        "combinaciones": total,
        "con_precio_fuera_del_intervalo": int(con_fuera.sum()),
        "sin_precio_en_toda_la_tienda": int(total - int(con_fuera.sum())),
        "semanas_totales_tienda": len(semanas_tienda),
        "ultima_semana_con_precio_fuera": ultima,
        "nota": (
            "Una combinación sin precio en el intervalo puede tener precio en otras semanas "
            "del archivo (habitualmente antes del periodo): no implica falta de inventario ni "
            "ventas cero, y no se imputa ningún valor."
        ),
    }


def _pct_aumentos(aumentos: int, disminuciones: int) -> tuple[float | None, str | None]:
    """Porcentaje de aumentos entre los cambios, o ``None`` con el motivo."""
    cambios = aumentos + disminuciones
    if cambios == 0:
        return None, "no hubo aumentos ni disminuciones de precio en el periodo"
    return round(100 * aumentos / cambios, 2), None


# --------------------------------------------------------------------------- #
# A) Distribución
# --------------------------------------------------------------------------- #


def distribucion(
    db: Database,
    version_precios: str,
    *,
    tienda: str,
    desde: date,
    hasta: date,
    categorias: list[str] | None = None,
    departamentos: list[str] | None = None,
    items_permitidos: list[str] | None = None,
    version_ventas: str | None = None,
) -> dict:
    """Distribución del precio medio por artículo en una tienda y un periodo.

    Una observación por **artículo × tienda**: su precio medio ponderado por los días
    del periodo cubiertos por cada semana (nunca por unidades). Los cuartiles se
    calculan con interpolación lineal (``numpy.percentile`` por omisión) sobre los
    artículos con al menos un día cubierto.

    ``version_ventas`` decide con qué catálogo se resuelven ``categorias`` y
    ``departamentos``; si se omite se usa la versión que declaran los documentos de
    precios de la tienda. Sin catálogo disponible, esos dos filtros no se aplican
    (``items_permitidos`` sí, porque no necesita catálogo).

    Un artículo sin precio en el periodo se publica con ``precio_medio`` ``None``:
    ausencia de precio, no cero.
    """
    base = _base(
        db,
        version_precios,
        tienda=tienda,
        desde=desde,
        hasta=hasta,
        version_ventas=version_ventas,
    )
    alcance = _alcance(
        base,
        categorias=categorias,
        departamentos=departamentos,
        items_permitidos=items_permitidos,
    )
    medios = _medios(base, alcance)

    items = medios["items"]
    precios = medios["medios"]
    dias_cubiertos = medios["dias_cubiertos"]
    semanas_precio = medios["semanas_con_precio"]
    con_precio = ~np.isnan(precios)
    total = len(items)
    n_con_precio = int(con_precio.sum())
    etiquetas = base["etiquetas"]

    categorias_de = [_catalogo_item(base, item)[0] for item in items]
    departamentos_de = [_catalogo_item(base, item)[1] for item in items]
    valores = [
        {
            "item_id": item,
            "cat_id": categorias_de[posicion],
            "dept_id": departamentos_de[posicion],
            "precio_medio": (
                round(float(precios[posicion]), DECIMALES) if con_precio[posicion] else None
            ),
            "semanas_con_precio": int(semanas_precio[posicion]),
            "dias_cubiertos": int(dias_cubiertos[posicion]),
        }
        for posicion, item in enumerate(items)
    ]

    cajas: list[dict] = []
    if n_con_precio:
        categorias_array = np.array(categorias_de, dtype=object)
        for cat_id in sorted({str(cat) for cat in categorias_array[con_precio].tolist()}):
            seleccion = con_precio & (categorias_array == cat_id)
            cajas.append(
                {
                    "categoria": cat_id,
                    "etiqueta": _etiqueta(etiquetas, cat_id),
                    "n": int(seleccion.sum()),
                    **_estadisticas(precios[seleccion]),
                }
            )

    salida = {
        "unidad": UNIDAD,
        "tienda": base["tienda"],
        "periodo": _periodo(base),
        "regla_ponderacion": REGLA_PONDERACION,
        "combinaciones_incluidas": total,
        "articulos": n_con_precio,
        # Desglose mutuamente excluyente: completa + parcial + sin precio = universo.
        "cobertura": _cobertura_combinaciones(con_precio, dias_cubiertos, base["dias_totales"]),
        "sin_precio": _sin_precio_detalle(base, medios),
        "cuartiles": _estadisticas(precios[con_precio]),
        "media": round(float(precios[con_precio].mean()), DECIMALES) if n_con_precio else None,
        "cajas": cajas,
        "valores": valores,
    }
    if not n_con_precio:
        salida["cuartiles"] = None
        salida["motivo"] = "sin precios en el periodo"
    return salida


# --------------------------------------------------------------------------- #
# B) Cambios
# --------------------------------------------------------------------------- #


def cambios(
    db: Database,
    version_precios: str,
    *,
    tienda: str,
    desde: date,
    hasta: date,
    categorias: list[str] | None = None,
    departamentos: list[str] | None = None,
    umbral_usd: float = UMBRAL_COMERCIAL_USD,
) -> dict:
    """Cambios de precio entre semanas consecutivas **con precio disponible**.

    Para cada serie (item × tienda) se ordenan las semanas del periodo y se compara
    cada semana con precio contra la última anterior que también tuvo precio: así un
    hueco sin precio no inventa una transición.

    Las diferencias se calculan en **centavos enteros**: un cambio de un centavo cuenta.
    ``umbral_usd`` es un umbral **comercial** opcional (0 por omisión) que se declara en la
    respuesta; no es una tolerancia técnica. Ni el catálogo ni estas cuentas hablan de
    promociones: solo de aumentos, disminuciones y semanas sin cambio.

    ``semanas`` cuenta las semanas del periodo con al menos un precio publicado en la
    tienda. ``series`` publica el detalle de las ``MAXIMO_SERIES`` series con más
    transiciones (solo las que tienen algún precio en el periodo); el resto solo suma
    en los agregados.
    """
    if umbral_usd < 0:
        raise ValueError("El umbral comercial es un importe en USD: no puede ser negativo")

    base = _base(db, version_precios, tienda=tienda, desde=desde, hasta=hasta)
    alcance = _alcance(base, categorias=categorias, departamentos=departamentos)
    columnas = np.flatnonzero(alcance)
    semanas_periodo = base["semanas_periodo"]

    if columnas.size == 0 or semanas_periodo.size == 0:
        vacio = np.zeros(columnas.size, dtype=np.int64)
        conteos = {
            "aumentos": vacio,
            "disminuciones": vacio.copy(),
            "sin_cambio": vacio.copy(),
            "transiciones": vacio.copy(),
            "semanas_con_precio": vacio.copy(),
            "primera_fila": np.full(columnas.size, -1, dtype=np.int64),
            "ultima_fila": np.full(columnas.size, -1, dtype=np.int64),
        }
        submatriz = np.empty((semanas_periodo.size, columnas.size), dtype=np.float64)
    else:
        submatriz = base["matriz"][np.ix_(base["filas_periodo"], columnas)]
        conteos = _transiciones(submatriz, umbral_usd)

    nombres = base["nombres"]
    items = [nombres[int(columna)] for columna in columnas.tolist()]
    categorias_de = [_catalogo_item(base, item)[0] for item in items]
    departamentos_de = [_catalogo_item(base, item)[1] for item in items]
    etiquetas = base["etiquetas"]

    totales = {
        "transiciones": int(conteos["transiciones"].sum()),
        "aumentos": int(conteos["aumentos"].sum()),
        "disminuciones": int(conteos["disminuciones"].sum()),
        "sin_cambio": int(conteos["sin_cambio"].sum()),
    }
    pct_aumentos, motivo_pct = _pct_aumentos(totales["aumentos"], totales["disminuciones"])
    totales["pct_aumentos_entre_cambios"] = pct_aumentos
    totales["motivo_pct"] = motivo_pct
    totales["pct_sin_cambio"] = (
        round(100 * totales["sin_cambio"] / totales["transiciones"], 2)
        if totales["transiciones"]
        else None
    )

    orden_categorias = sorted({str(cat) for cat in categorias_de})
    por_categoria: list[dict] = []
    categorias_array = np.array(categorias_de, dtype=object)
    for cat_id in orden_categorias:
        seleccion = categorias_array == cat_id
        aumentos = int(conteos["aumentos"][seleccion].sum())
        disminuciones = int(conteos["disminuciones"][seleccion].sum())
        pct_categoria, _ = _pct_aumentos(aumentos, disminuciones)
        por_categoria.append(
            {
                "cat_id": cat_id,
                "etiqueta": _etiqueta(etiquetas, cat_id),
                "transiciones": int(conteos["transiciones"][seleccion].sum()),
                "aumentos": aumentos,
                "disminuciones": disminuciones,
                "sin_cambio": int(conteos["sin_cambio"][seleccion].sum()),
                "pct_aumentos_entre_cambios": pct_categoria,
            }
        )

    semanas_lista = [int(semana) for semana in semanas_periodo.tolist()]
    con_precio = conteos["semanas_con_precio"] > 0
    # Solo se publica el detalle de series con algún precio: sin precio no hay serie.
    orden_series = sorted(
        np.flatnonzero(con_precio).tolist(),
        key=lambda posicion: (-int(conteos["transiciones"][posicion]), items[posicion]),
    )
    series: list[dict] = []
    for posicion in orden_series[:MAXIMO_SERIES]:
        primera = int(conteos["primera_fila"][posicion])
        ultima = int(conteos["ultima_fila"][posicion])
        series.append(
            {
                "item_id": items[posicion],
                "cat_id": categorias_de[posicion],
                "dept_id": departamentos_de[posicion],
                "aumentos": int(conteos["aumentos"][posicion]),
                "disminuciones": int(conteos["disminuciones"][posicion]),
                "sin_cambio": int(conteos["sin_cambio"][posicion]),
                "transiciones": int(conteos["transiciones"][posicion]),
                "semanas_con_precio": int(conteos["semanas_con_precio"][posicion]),
                "primera_semana": semanas_lista[primera],
                "ultima_semana": semanas_lista[ultima],
            }
        )

    return {
        "unidad": UNIDAD,
        "tienda": base["tienda"],
        "periodo": _periodo(base),
        "umbral": {
            "valor_usd": float(umbral_usd),
            "tipo": TIPO_UMBRAL,
            "precision": PRECISION_ORIGEN,
            "nota": (
                "Por omisión 0: cualquier cambio de un centavo cuenta como aumento o "
                "disminución. No se usa ninguna tolerancia para tapar el error de "
                "representación."
            ),
        },
        "semanas_del_intervalo": int(semanas_periodo.size),
        "precision_comparacion": "centavos",
        "muestra_precios_enteros": True,
        "semanas": int((~np.isnan(submatriz)).any(axis=1).sum()),
        "series_con_precio": int(con_precio.sum()),
        "totales": totales,
        "por_categoria": por_categoria,
        "series": series,
        "nota": NOTA_SIN_PROMOCIONES,
    }


# --------------------------------------------------------------------------- #
# C) Evolución de un artículo
# --------------------------------------------------------------------------- #


def evolucion(
    db: Database,
    version_precios: str,
    *,
    tienda: str,
    item_id: str,
    desde: date,
    hasta: date,
) -> dict:
    """Serie semanal del precio de un artículo en una tienda.

    Solo publica las semanas del periodo con precio. ``inicio`` y ``fin`` son las
    fechas del calendario que caen dentro del periodo: la primera y la última semana
    pueden quedar parciales. Los conteos de cambios comparan semanas consecutivas con
    precio en **centavos enteros** (los huecos no cuentan).

    Si el artículo no tiene precio en ninguna semana del periodo, ``puntos`` va vacío y
    ``motivo`` explica por qué: no se devuelven ceros.
    """
    if desde > hasta:
        raise ValueError("El periodo es inválido: 'desde' no puede ser posterior a 'hasta'")

    detalle = _leer_tienda_detalle(db, version_precios, tienda)
    calendario = _calendario_periodo(db, desde, hasta)
    series = detalle["semanas"]

    puntos: list[dict] = []
    precios: list[float] = []
    for semana in sorted(calendario):
        precio = series.get(semana, {}).get(item_id)
        if precio is None:
            continue
        datos = calendario[semana]
        precios.append(precio)
        puntos.append(
            {
                "wm_yr_wk": semana,
                "inicio": datos["inicio"].isoformat(),
                "fin": datos["fin"].isoformat(),
                "precio": round(float(precio), DECIMALES),
            }
        )

    # Una sola columna: los cambios se cuentan igual que en `cambios`, sin huecos.
    conteos = _transiciones(np.array(precios, dtype=np.float64).reshape(-1, 1))
    salida = {
        "unidad": UNIDAD,
        "tienda": tienda,
        "item_id": item_id,
        "periodo": {
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "dias": sum(datos["dias"] for datos in calendario.values()),
        },
        "puntos": puntos,
        "semanas_con_precio": len(puntos),
        "semanas_sin_precio": len(calendario) - len(puntos),
        "cambios": {
            "aumentos": int(conteos["aumentos"][0]),
            "disminuciones": int(conteos["disminuciones"][0]),
            "sin_cambio": int(conteos["sin_cambio"][0]),
            "transiciones": int(conteos["transiciones"][0]),
        },
        "umbral": {
            "valor_usd": UMBRAL_COMERCIAL_USD,
            "tipo": TIPO_UMBRAL,
            "precision": PRECISION_ORIGEN,
            "nota": "Por omisión no hay umbral: un cambio de un centavo cuenta.",
        },
        "motivo": None,
    }
    if not puntos:
        salida["motivo"] = "sin precios publicados para este artículo en el periodo"
    return salida


# --------------------------------------------------------------------------- #
# D) Resumen de cabecera
# --------------------------------------------------------------------------- #


def resumen_precios(
    db: Database, version_precios: str, *, tienda: str, desde: date, hasta: date
) -> dict:
    """Cifras de cabecera de la pestaña de precios.

    Reutiliza el mismo lector y el mismo cálculo de precios medios ponderados que
    :func:`distribucion` (una sola lectura por tienda), pero devuelve solo los
    escalares: semanas con precio, series con precio, artículos con precio y la
    media, mediana, mínimo y máximo de los precios medios por artículo.
    """
    base = _base(db, version_precios, tienda=tienda, desde=desde, hasta=hasta)
    medios = _medios(base, np.ones(len(base["nombres"]), dtype=bool))
    precios_validos = medios["medios"][~np.isnan(medios["medios"])]
    filas = base["filas_periodo"]
    semanas = int((~np.isnan(base["matriz"][filas])).any(axis=1).sum()) if filas.size else 0
    extremos = _estadisticas(precios_validos)
    con_precio = precios_validos.size
    return {
        "semanas": semanas,
        "series_con_precio": int(con_precio),
        "items": len(medios["items"]),
        "precio_medio": (
            round(float(precios_validos.mean()), DECIMALES) if con_precio else None
        ),
        "mediana": extremos["mediana"],
        "min": extremos["min"],
        "max": extremos["max"],
        "unidad": UNIDAD,
    }
