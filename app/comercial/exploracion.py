"""Análisis descriptivos de exploración del Atlas comercial.

Fuente analítica de referencia: el EDA interactivo de M5
(``back-to-predict-the-future-interactive-m5-eda.Rmd``), que organiza el análisis en
territorio y jerarquía de producto, estacionalidad global, calendario (eventos y SNAP),
precios y comportamiento de las series individuales.

Este módulo **traduce** esos análisis al backend del proyecto, recalculándolos sobre la
versión publicada en MongoDB local. No reproduce el notebook: aquí no hay R, ni loess, ni
muestras aleatorias, ni el conjunto de desastres externos.

Qué calcula
-----------
- Combinaciones reales del catálogo (tienda × categoría y tienda × departamento) y los
  conteos que no se pueden derivar sumando: artículos únicos y series artículo × tienda.
- Comparación entre dos periodos por tienda × categoría, con días observados y métrica
  «unidades totales» o «promedio diario».
- Estacionalidad semanal (lunes a domingo) y mensual (enero a diciembre entre años).
- Matriz temporal tienda × día para un año del calendario.
- Detalle de artículo × tienda con ventas diarias, precio semanal y capas de calendario.
- Resumen de intermitencia (ceros observados) por serie artículo × tienda.

Reglas que se aplican aquí
--------------------------
- Un día **sin observación** devuelve ``None``; nunca se convierte en cero. El cero es una
  venta observada de cero unidades.
- Un **precio ausente** es ausencia, no cero, y no se imputa.
- Los denominadores son explícitos: el promedio diario divide entre días observados, no
  entre días del calendario.
- Las combinaciones inexistentes no se fabrican con productos cartesianos: se verifican
  contra las series realmente importadas.
- Los conteos distintos no se suman entre tiendas para obtener un total nacional.
- Nada de esto es causal ni se usa como variable de entrenamiento: el pipeline predictivo
  calcula sus propias variables con corte explícito en el origen.
"""

from __future__ import annotations

import calendar
import time
from datetime import date, timedelta

import numpy as np
from pymongo.database import Database

from app.comercial import consultas, precios_almacen
from app.comercial.consultas import FiltroInvalido
from app.comercial.precios import manifiesto_vigente
from app.db.mongo import M5_AGREGADOS, M5_BLOQUES, M5_CALENDARIO, M5_CATALOGO, M5_PRECIOS

UNIDAD = "unidades vendidas"

DIAS_SEMANA = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
"""Etiqueta de presentación del día de la semana; el índice 0 es lunes."""

MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

DIMENSIONES = ("estado", "tienda", "categoria")
"""Cortes admitidos para la estacionalidad; no se cruzan entre sí."""

METRICAS_AB = ("total", "promedio_diario")

TOLERANCIA_PRECIO = 0.01
"""Diferencia absoluta (USD por unidad) para considerar que un precio cambió."""

MAX_SERIES_TABLA = 200
"""Tope de series por página en la tabla de intermitencia."""

DIMENSION_DERIVADO = "departamento_tienda"
"""Dimensión del derivado tienda × departamento ya publicado."""


# --------------------------------------------------------------------------- #
# Lectura de vectores
# --------------------------------------------------------------------------- #


def _alinear(documento: dict, dias: list[int]) -> list[int | None]:
    """Vector de un agregado alineado a ``dias``; fuera de rango es ``None``."""
    inicio = int(documento.get("d_inicio") or 1)
    unidades = documento.get("unidades") or []
    salida: list[int | None] = []
    for dia in dias:
        posicion = dia - inicio
        salida.append(int(unidades[posicion]) if 0 <= posicion < len(unidades) else None)
    return salida


def _vectores_agregados(
    db: Database, version: str, dimension: str, claves: list[str], dias: list[int]
) -> dict[str, list[int | None]]:
    """Vector diario de cada clave de una dimensión de ``m5_agregados``."""
    if not claves:
        return {}
    documentos = {
        documento["clave"]: documento
        for documento in db[M5_AGREGADOS].find(
            {"version": version, "dimension": dimension, "clave": {"$in": list(claves)}},
            {"clave": 1, "d_inicio": 1, "unidades": 1},
        )
    }
    return {clave: _alinear(documentos[clave], dias) if clave in documentos else [] for clave in claves}


def _sumar(vectores: list[list[int | None]], dias: list[int]) -> list[int | None]:
    """Suma vectores día a día; un día solo es válido si **todas** las partes lo están."""
    if not vectores:
        return [None] * len(dias)
    salida: list[int | None] = []
    for indice in range(len(dias)):
        total = 0
        completo = True
        for vector in vectores:
            if indice >= len(vector) or vector[indice] is None:
                completo = False
                break
            total += int(vector[indice] or 0)
        salida.append(total if completo else None)
    return salida


def _serie_bloques(
    db: Database, version: str, *, tienda: str, item: str, dias: list[int]
) -> list[int | None]:
    """Serie diaria de un artículo en una tienda, leída de los bloques locales."""
    documentos = list(
        db[M5_BLOQUES].find(
            {"version": version, "store_id": tienda, "item_id": item},
            {"d_inicio": 1, "unidades": 1},
        )
    )
    if not documentos:
        return [None] * len(dias)
    valores: dict[int, int] = {}
    for documento in documentos:
        inicio = int(documento["d_inicio"])
        for posicion, valor in enumerate(documento["unidades"]):
            valores[inicio + posicion] = int(valor)
    return [valores.get(dia) for dia in dias]


# --------------------------------------------------------------------------- #
# Catálogo, existencias y conteos
# --------------------------------------------------------------------------- #


def _catalogo_productos(db: Database, version: str) -> dict[str, dict]:
    """``item_id`` → ``{cat_id, dept_id}`` de la versión publicada."""
    return {
        documento["item_id"]: {
            "cat_id": documento.get("cat_id"),
            "dept_id": documento.get("dept_id"),
        }
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "producto"}, {"item_id": 1, "cat_id": 1, "dept_id": 1}
        )
    }


def existencias(db: Database, version: str) -> dict:
    """Series artículo × tienda realmente importadas, por tienda.

    Se leen de los bloques (una serie por par) y no del catálogo: así una combinación
    tienda × categoría solo se ofrece si existe al menos un artículo de esa categoría con
    ventas en esa tienda. No se fabrican combinaciones por producto cartesiano.
    """
    productos = _catalogo_productos(db, version)
    por_tienda: dict[str, list[str]] = {}
    categorias: dict[str, set[str]] = {}
    departamentos: dict[str, set[str]] = {}
    for tienda in sorted({d["store_id"] for d in db[M5_CATALOGO].find({"version": version, "tipo": "tienda"}, {"store_id": 1})}):
        items = sorted(
            db[M5_BLOQUES].distinct("item_id", {"version": version, "store_id": tienda})
        )
        por_tienda[tienda] = items
        categorias[tienda] = {productos[i]["cat_id"] for i in items if i in productos}
        departamentos[tienda] = {productos[i]["dept_id"] for i in items if i in productos}
    return {
        "items_por_tienda": por_tienda,
        "categorias_en_tienda": {tienda: sorted(valor) for tienda, valor in categorias.items()},
        "departamentos_en_tienda": {
            tienda: sorted(valor) for tienda, valor in departamentos.items()
        },
        "articulos": len(productos),
        "series": sum(len(items) for items in por_tienda.values()),
        "productos": productos,
    }


def combinaciones(db: Database, version: str, *, tiendas: list[str], categorias: list[str]) -> list[dict]:
    """Combinaciones tienda × categoría que existen en las series importadas."""
    datos = existencias(db, version)
    productos = datos["productos"]
    etiquetas = consultas.CATEGORIAS_NOMBRE
    salida: list[dict] = []
    for tienda in tiendas:
        presentes = set(datos["categorias_en_tienda"].get(tienda, []))
        for categoria in categorias:
            if categoria not in presentes:
                continue
            items = [i for i in datos["items_por_tienda"].get(tienda, []) if productos.get(i, {}).get("cat_id") == categoria]
            salida.append(
                {
                    "clave": f"{tienda}|{categoria}",
                    "store_id": tienda,
                    "cat_id": categoria,
                    "etiqueta": f"{etiquetas.get(categoria, categoria)} · {categoria}",
                    "series": len(items),
                }
            )
    return salida


def conteos_contexto(
    db: Database, version: str, *, filtros: dict, fechas: dict[int, date], dias: list[int]
) -> dict:
    """Conteos distintos del contexto filtrado, sin sumar marginales.

    Distingue de forma explícita: artículos únicos (``item_id`` distinto), series
    artículo × tienda (par distinto), categorías únicas y combinaciones reales.
    """
    datos = existencias(db, version)
    productos = datos["productos"]
    tiendas = [t for t in filtros["tiendas"] if t in datos["items_por_tienda"]]
    categorias = list(filtros["categorias"])
    departamentos = list(filtros["departamentos"])
    seleccionados = {
        item
        for tienda in tiendas
        for item in datos["items_por_tienda"].get(tienda, [])
        if productos.get(item, {}).get("cat_id") in categorias
        and (not departamentos or productos.get(item, {}).get("dept_id") in departamentos)
    }
    series = sum(
        1
        for tienda in tiendas
        for item in datos["items_por_tienda"].get(tienda, [])
        if item in seleccionados
    )
    departamentos_catalogo = sorted(
        {
            documento["dept_id"]
            for documento in db[M5_CATALOGO].find(
                {"version": version, "tipo": "departamento"}, {"dept_id": 1, "cat_id": 1}
            )
            if documento.get("cat_id") in categorias
        }
    )
    combos_departamento = sum(
        1
        for tienda in tiendas
        for departamento in datos["departamentos_en_tienda"].get(tienda, [])
        if departamento in departamentos_catalogo
    )
    return {
        "tiendas": len(tiendas),
        "estados": len({t.split("_")[0] for t in tiendas}),
        "categorias_unicas": len(categorias),
        "combinaciones_tienda_categoria": sum(
            1
            for tienda in tiendas
            for categoria in categorias
            if categoria in datos["categorias_en_tienda"].get(tienda, [])
        ),
        "departamentos_unicos": len(departamentos_catalogo),
        "combinaciones_tienda_departamento": combos_departamento,
        "articulos_unicos": len(seleccionados),
        "series_articulo_tienda": series,
        "regla": (
            "Artículos únicos = item_id distintos del contexto; series = pares "
            "artículo × tienda distintos. No se suman artículos únicos de varias tiendas "
            "para obtener un total nacional: un artículo puede estar en varias tiendas."
        ),
    }


# --------------------------------------------------------------------------- #
# Comparación entre periodos por tienda × categoría
# --------------------------------------------------------------------------- #


def _vector_combo(
    db: Database,
    version: str,
    *,
    filtros: dict,
    tienda: str,
    categoria: str,
    dias: list[int],
) -> list[int | None]:
    """Unidades diarias de una tienda y una categoría, con los filtros comerciales.

    El agregado ``categoria_tienda`` solo es válido sin filtros de departamento ni de
    producto. Con ellos se baja al nivel que sí los soporta: ``departamento_tienda`` o los
    bloques de los artículos seleccionados. Nunca se combinan marginales.
    """
    departamentos = list(filtros["departamentos"])
    productos = list(filtros["productos"])
    if productos:
        catalogo = _catalogo_productos(db, version)
        elegidos = [
            item
            for item in productos
            if catalogo.get(item, {}).get("cat_id") == categoria
            and (not departamentos or catalogo.get(item, {}).get("dept_id") in departamentos)
        ]
        if not elegidos:
            return [None] * len(dias)
        return _sumar(
            [_serie_bloques(db, version, tienda=tienda, item=item, dias=dias) for item in elegidos],
            dias,
        )
    if departamentos:
        catalogo = _catalogo_productos(db, version)
        deps = sorted(
            {
                departamento
                for departamento in departamentos
                if any(
                    datos["dept_id"] == departamento and datos["cat_id"] == categoria
                    for datos in catalogo.values()
                )
            }
        )
        if not deps:
            return [None] * len(dias)
        vectores = _vectores_agregados(
            db,
            version,
            DIMENSION_DERIVADO,
            [f"{tienda}|{departamento}" for departamento in deps],
            dias,
        )
        return _sumar([vector for vector in vectores.values() if vector], dias)
    vectores = _vectores_agregados(db, version, "categoria_tienda", [f"{categoria}|{tienda}"], dias)
    return vectores.get(f"{categoria}|{tienda}", [])


def _metricas_intervalo(
    vector: list[int | None], dias: list[int], *, desde: date, hasta: date, fechas: dict[int, date]
) -> dict:
    """Unidades observadas y días válidos de un intervalo inclusivo."""
    unidades = 0
    validos = 0
    esperados = 0
    ceros = 0
    if not vector:
        return {"unidades": 0, "dias_validos": 0, "dias_esperados": 0, "dias_cero": 0, "hay_datos": False}
    for indice, dia in enumerate(dias):
        fecha = fechas.get(dia)
        if fecha is None or fecha < desde or fecha > hasta:
            continue
        esperados += 1
        valor = vector[indice] if indice < len(vector) else None
        if valor is None:
            continue
        validos += 1
        unidades += int(valor)
        if int(valor) == 0:
            ceros += 1
    return {
        "unidades": unidades,
        "dias_validos": validos,
        "dias_esperados": esperados,
        "dias_cero": ceros,
        "hay_datos": validos > 0,
    }


def _valor_metrica(intervalo: dict, metrica: str) -> float | None:
    """Valor de la métrica pedida para un intervalo, o ``None`` si no es calculable."""
    if not intervalo["hay_datos"]:
        return None
    if metrica == "total":
        return float(intervalo["unidades"])
    return intervalo["unidades"] / intervalo["dias_validos"]


def _diferencia(valor_b: float | None, valor_a: float | None, metrica: str) -> tuple[float | None, float | None, str | None]:
    """Diferencia (B − A) y variación porcentual, con el motivo cuando no se puede."""
    if valor_a is None or valor_b is None:
        return None, None, "sin días observados en uno de los dos periodos"
    diferencia = valor_b - valor_a
    if valor_a == 0:
        if valor_b == 0:
            return diferencia, None, "cero en ambos periodos"
        return diferencia, None, "sin base en A"
    return diferencia, 100 * diferencia / valor_a, None


def comparacion_tienda_categoria(
    db: Database, version: str, *, filtros: dict, fechas: dict[int, date]
) -> dict:
    """Comparación entre los periodos A y B por tienda × categoría.

    Cada fila es una combinación **real** del catálogo con las series importadas. El
    promedio diario divide entre días observados (un día con cero ventas cuenta; un día
    sin observación no). La cobertura de cada fila se declara y una suma parcial nunca se
    presenta como total completo.
    """
    inicio = time.perf_counter()
    ventana = filtros["ventana"]
    a, b = filtros["a"], filtros["b"]
    dias = consultas._dias_de(ventana, fechas)
    combos = combinaciones(
        db, version, tiendas=list(filtros["tiendas"]), categorias=list(filtros["categorias"])
    )
    catalogo_tiendas = {
        documento["store_id"]: documento.get("state_id")
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "tienda"}, {"store_id": 1, "state_id": 1}
        )
    }
    filas = []
    for combo in combos:
        vector = _vector_combo(
            db,
            version,
            filtros=filtros,
            tienda=combo["store_id"],
            categoria=combo["cat_id"],
            dias=dias,
        )
        intervalo_a = _metricas_intervalo(vector, dias, desde=a.desde, hasta=a.hasta, fechas=fechas)
        intervalo_b = _metricas_intervalo(vector, dias, desde=b.desde, hasta=b.hasta, fechas=fechas)
        intervalo_total = _metricas_intervalo(
            vector, dias, desde=ventana.desde, hasta=ventana.hasta, fechas=fechas
        )
        metricas: dict[str, dict] = {}
        for metrica in METRICAS_AB:
            valor_a = _valor_metrica(intervalo_a, metrica)
            valor_b = _valor_metrica(intervalo_b, metrica)
            diferencia, variacion, motivo = _diferencia(valor_b, valor_a, metrica)
            metricas[metrica] = {
                "a": valor_a,
                "b": valor_b,
                "diferencia": diferencia,
                "variacion_pct": variacion,
                "motivo": motivo,
            }
        completa = (
            intervalo_a["dias_validos"] == intervalo_a["dias_esperados"]
            and intervalo_b["dias_validos"] == intervalo_b["dias_esperados"]
            and intervalo_a["dias_esperados"] > 0
            and intervalo_b["dias_esperados"] > 0
        )
        cobertura = "sin_datos" if not (intervalo_a["hay_datos"] or intervalo_b["hay_datos"]) else (
            "completa" if completa else "parcial"
        )
        filas.append(
            {
                "clave": combo["clave"],
                "store_id": combo["store_id"],
                "state_id": catalogo_tiendas.get(combo["store_id"]),
                "cat_id": combo["cat_id"],
                "etiqueta": combo["etiqueta"],
                "series": combo["series"],
                "unidades_a": intervalo_a["unidades"],
                "unidades_b": intervalo_b["unidades"],
                "dias_validos_a": intervalo_a["dias_validos"],
                "dias_esperados_a": intervalo_a["dias_esperados"],
                "dias_validos_b": intervalo_b["dias_validos"],
                "dias_esperados_b": intervalo_b["dias_esperados"],
                "dias_cero_a": intervalo_a["dias_cero"],
                "dias_cero_b": intervalo_b["dias_cero"],
                # Periodo principal (el de la exploración y la composición): sirve para
                # la matriz «por tienda» sin mezclar ámbitos temporales con A/B.
                "unidades_periodo": intervalo_total["unidades"],
                "dias_validos_periodo": intervalo_total["dias_validos"],
                "dias_esperados_periodo": intervalo_total["dias_esperados"],
                "cobertura": cobertura,
                "metricas": metricas,
                "motivo": metricas["total"]["motivo"],
            }
        )

    def _suma(indice: str, campo: str) -> int:
        return int(sum(fila[campo] for fila in filas if fila["cobertura"] != "sin_datos"))

    # Reconciliación: la suma de las filas se contrasta con el total del contexto.
    contexto_a = _metricas_intervalo(
        consultas._vector_contexto(
            db,
            version,
            tiendas=list(filtros["tiendas"]),
            categorias=list(filtros["categorias"]),
            departamentos=list(filtros["departamentos"]),
            dias=dias,
        ),
        dias,
        desde=a.desde,
        hasta=a.hasta,
        fechas=fechas,
    )
    contexto_b = _metricas_intervalo(
        consultas._vector_contexto(
            db,
            version,
            tiendas=list(filtros["tiendas"]),
            categorias=list(filtros["categorias"]),
            departamentos=list(filtros["departamentos"]),
            dias=dias,
        ),
        dias,
        desde=b.desde,
        hasta=b.hasta,
        fechas=fechas,
    )
    suma_a = _suma("a", "unidades_a")
    suma_b = _suma("b", "unidades_b")
    filas_por_tienda: dict[str, list[dict]] = {}
    for fila in filas:
        filas_por_tienda.setdefault(fila["store_id"], []).append(fila)
    return {
        "unidad": UNIDAD,
        "metrica_inicial": "total",
        "metricas": list(METRICAS_AB),
        "regla_promedio": (
            "Promedio diario = unidades observadas ÷ días con observación del periodo. "
            "Un día con cero ventas observado cuenta en el denominador; un día sin dato no "
            "cuenta. Normalizar por días no elimina las diferencias de estacionalidad."
        ),
        "periodo": {
            "desde": ventana.desde.isoformat(),
            "hasta": ventana.hasta.isoformat(),
            "dias": ventana.dias,
        },
        "a": {"desde": a.desde.isoformat(), "hasta": a.hasta.isoformat(), "dias": a.dias},
        "b": {"desde": b.desde.isoformat(), "hasta": b.hasta.isoformat(), "dias": b.dias},
        "por_omision": bool(filtros.get("ab_por_omision")),
        "filas": filas,
        "tiendas": [
            {
                "store_id": tienda,
                "state_id": catalogo_tiendas.get(tienda),
                "filas": [fila["clave"] for fila in grupo],
            }
            for tienda, grupo in sorted(filas_por_tienda.items())
        ],
        "conteos": {
            "tiendas": len(filas_por_tienda),
            "categorias": len({fila["cat_id"] for fila in filas}),
            "combinaciones": len(filas),
            "filas_con_cobertura_completa": sum(1 for fila in filas if fila["cobertura"] == "completa"),
            "filas_con_cobertura_parcial": sum(1 for fila in filas if fila["cobertura"] == "parcial"),
            "filas_sin_datos": sum(1 for fila in filas if fila["cobertura"] == "sin_datos"),
        },
        "reconciliacion": {
            "filas_a": suma_a,
            "contexto_a": contexto_a["unidades"],
            "cuadra_a": suma_a == contexto_a["unidades"],
            "filas_b": suma_b,
            "contexto_b": contexto_b["unidades"],
            "cuadra_b": suma_b == contexto_b["unidades"],
            "nota": (
                "La suma de las filas se contrasta con el total del contexto calculado sobre "
                "las mismas tiendas, categorías y filtros. Si no cuadra, la diferencia se "
                "declara en lugar de ajustar una cifra."
            ),
        },
        "calculo_ms": round((time.perf_counter() - inicio) * 1000, 1),
    }


# --------------------------------------------------------------------------- #
# Estacionalidad
# --------------------------------------------------------------------------- #


def _vector_tienda(
    db: Database, version: str, *, filtros: dict, tienda: str, dias: list[int]
) -> list[int | None]:
    """Unidades diarias de una tienda con **todos** los filtros comerciales aplicados.

    Se suman las categorías permitidas de esa tienda (cada una resuelta por
    :func:`_vector_combo`). Nunca se lee el agregado de tienda completo si hay un filtro
    de categoría, departamento o producto: eso devolvería un total que no corresponde al
    contexto pedido.
    """
    vectores = [
        _vector_combo(db, version, filtros=filtros, tienda=tienda, categoria=categoria, dias=dias)
        for categoria in filtros["categorias"]
    ]
    return _sumar([vector for vector in vectores if vector], dias)


def _series_por_dimension(
    db: Database, version: str, *, filtros: dict, dias: list[int], dimension: str
) -> dict[str, dict]:
    """Serie diaria por grupo de la dimensión pedida (estado, tienda o categoría)."""
    tiendas = list(filtros["tiendas"])
    cal = consultas.catalogo(db, version)
    estado_de = {tienda["store_id"]: tienda["state_id"] for tienda in cal["tiendas"]}
    if dimension == "tienda":
        claves = sorted(tiendas)
        return {
            clave: {
                "etiqueta": clave,
                "estado": estado_de.get(clave),
                "vector": _vector_tienda(db, version, filtros=filtros, tienda=clave, dias=dias),
            }
            for clave in claves
        }
    if dimension == "estado":
        grupos: dict[str, list[str]] = {}
        for tienda in tiendas:
            grupos.setdefault(estado_de.get(tienda, "?"), []).append(tienda)
        salida: dict[str, dict] = {}
        for estado, lista in sorted(grupos.items()):
            vectores = [
                _vector_tienda(db, version, filtros=filtros, tienda=tienda, dias=dias)
                for tienda in sorted(lista)
            ]
            salida[estado] = {
                "etiqueta": consultas.ESTADOS_NOMBRE.get(estado, estado),
                "tiendas": sorted(lista),
                "vector": _sumar([vector for vector in vectores if vector], dias),
            }
        return salida
    claves = list(filtros["categorias"])
    salida = {}
    for categoria in claves:
        vectores = [
            _vector_combo(db, version, filtros=filtros, tienda=tienda, categoria=categoria, dias=dias)
            for tienda in sorted(tiendas)
        ]
        salida[categoria] = {
            "etiqueta": f"{consultas.CATEGORIAS_NOMBRE.get(categoria, categoria)} · {categoria}",
            "tiendas": sorted(tiendas),
            "vector": _sumar([vector for vector in vectores if vector], dias),
        }
    return salida


def _semanal(series: dict[str, dict], dias: list[int], fechas: dict[int, date]) -> dict:
    """Resumen lunes a domingo por grupo, con promedio diario y días observados."""
    grupos = []
    for clave, grupo in series.items():
        unidades = [0] * 7
        validos = [0] * 7
        ceros = [0] * 7
        for indice, dia in enumerate(dias):
            valor = grupo["vector"][indice] if indice < len(grupo["vector"]) else None
            if valor is None:
                continue
            fecha = fechas.get(dia)
            if fecha is None:
                continue
            posicion = fecha.weekday()
            unidades[posicion] += int(valor)
            validos[posicion] += 1
            if int(valor) == 0:
                ceros[posicion] += 1
        puntos = [
            {
                "dia_semana": posicion + 1,
                "etiqueta": DIAS_SEMANA[posicion],
                "unidades": unidades[posicion],
                "fechas_validas": validos[posicion],
                "promedio": (
                    round(unidades[posicion] / validos[posicion], 4) if validos[posicion] else None
                ),
                "dias_cero": ceros[posicion],
            }
            for posicion in range(7)
        ]
        grupos.append(
            {
                "clave": clave,
                "etiqueta": grupo.get("etiqueta", clave),
                "tiendas": grupo.get("tiendas"),
                "puntos": puntos,
                "total": sum(unidades),
                "fechas_validas": sum(validos),
            }
        )
    promedios = [
        punto["promedio"]
        for grupo in grupos
        for punto in grupo["puntos"]
        if punto["promedio"] is not None
    ]
    return {
        "grupos": grupos,
        "escala_comun_max": round(max(promedios), 4) if promedios else None,
    }


def _rango_de_fechas(desde: date, hasta: date) -> list[date]:
    """Fechas inclusivas entre dos días (vacío si el orden está invertido)."""
    if hasta < desde:
        return []
    return [desde + timedelta(days=paso) for paso in range((hasta - desde).days + 1)]


def _mensual(
    vector: list[int | None],
    dias: list[int],
    fechas: dict[int, date],
    *,
    ventana,
    cobertura: dict,
) -> dict:
    """Año × mes con **cuatro** conteos distintos y sin confundirlos.

    - ``dias_del_mes``: días que tiene el mes en el calendario (31 en enero).
    - ``dias_en_intervalo``: días del mes que caen dentro del periodo **solicitado**.
    - ``dias_con_cobertura``: días del mes dentro del periodo que además tienen cobertura
      de ventas publicada.
    - ``dias_observados``: días de esos con un valor observado (nunca se imputa un cero).

    El promedio diario usa ``dias_observados`` como denominador y las unidades de esos
    mismos días como numerador.
    """
    valores_por_fecha: dict[date, int | None] = {}
    for indice, dia in enumerate(dias):
        fecha = fechas.get(dia)
        if fecha is None:
            continue
        valores_por_fecha[fecha] = vector[indice] if indice < len(vector) else None

    solicitadas = _rango_de_fechas(ventana.desde, ventana.hasta)
    meses: dict[tuple[int, int], dict] = {}
    for fecha in solicitadas:
        registro = meses.setdefault(
            (fecha.year, fecha.month),
            {
                "unidades": 0,
                "dias_en_intervalo": 0,
                "dias_con_cobertura": 0,
                "dias_observados": 0,
                "dias_cero": 0,
                "primera_cobertura": None,
                "ultima_cobertura": None,
            },
        )
        registro["dias_en_intervalo"] += 1
        if not (cobertura["desde"] <= fecha <= cobertura["hasta"]):
            continue
        registro["dias_con_cobertura"] += 1
        if registro["primera_cobertura"] is None:
            registro["primera_cobertura"] = fecha
        registro["ultima_cobertura"] = fecha
        valor = valores_por_fecha.get(fecha)
        if valor is None:
            continue
        registro["dias_observados"] += 1
        registro["unidades"] += int(valor)
        if int(valor) == 0:
            registro["dias_cero"] += 1

    def _motivo(anio: int, mes: int, dias_del_mes: int, registro: dict) -> str | None:
        """Explica qué límite deja el mes incompleto, sin usar el mismo campo dos veces.

        Empieza con «N días observados de M» y añade el límite real: el inicio o el fin de
        los datos de ventas cuando el periodo los alcanza, o el periodo solicitado cuando es
        más estrecho, o días con cobertura sin observación.
        """
        if registro["dias_observados"] == dias_del_mes:
            return None
        if registro["dias_en_intervalo"] == 0:
            return "el periodo solicitado no incluye este mes"
        inicio_mes = date(anio, mes, 1)
        fin_mes = date(anio, mes, dias_del_mes)
        partes = [f"{registro['dias_observados']} días observados de {dias_del_mes}"]

        # Inicio: si el periodo empieza en el mismo día que la cobertura (o antes), el
        # límite lo pone el inicio de los datos; si empieza después, lo pone el periodo.
        if cobertura["desde"] > inicio_mes and ventana.desde <= cobertura["desde"]:
            partes.append(
                f"datos disponibles desde el {cobertura['desde'].day} de "
                f"{MESES[cobertura['desde'].month - 1]} de {cobertura['desde'].year}"
            )
        elif ventana.desde > inicio_mes:
            partes.append(
                f"el periodo solicitado cubre {registro['dias_en_intervalo']} de {dias_del_mes} días"
            )

        # Fin: mismo criterio al otro extremo.
        if cobertura["hasta"] < fin_mes and ventana.hasta >= cobertura["hasta"]:
            partes.append(
                f"datos disponibles hasta el {cobertura['hasta'].day} de "
                f"{MESES[cobertura['hasta'].month - 1]} de {cobertura['hasta'].year}"
            )
        elif ventana.hasta < fin_mes:
            partes.append(
                f"el periodo solicitado termina el {ventana.hasta.day} de "
                f"{MESES[ventana.hasta.month - 1]}"
            )

        if registro["dias_observados"] < registro["dias_con_cobertura"]:
            partes.append(
                f"{registro['dias_con_cobertura'] - registro['dias_observados']} días con cobertura "
                "sin observación"
            )
        return " · ".join(partes)

    anios = []
    for (anio, _) in sorted(meses):
        if anio not in [fila["anio"] for fila in anios]:
            anios.append({"anio": anio, "meses": []})
    indices = {fila["anio"]: fila for fila in anios}
    for anio, fila in indices.items():
        for mes in range(1, 13):
            dias_del_mes = calendar.monthrange(anio, mes)[1]
            registro = meses.get((anio, mes))
            if registro is None:
                fila["meses"].append(
                    {
                        "mes": mes,
                        "etiqueta": MESES[mes - 1],
                        "dias_del_mes": dias_del_mes,
                        "dias_en_intervalo": 0,
                        "dias_con_cobertura": 0,
                        "dias_observados": 0,
                        "dias_sin_dato": 0,
                        "dias_fuera_intervalo": dias_del_mes,
                        "unidades": None,
                        "promedio_diario": None,
                        "dias_cero": 0,
                        "completo": False,
                        "parcial": True,
                        "motivo": "el periodo solicitado no incluye este mes",
                    }
                )
                continue
            completo = registro["dias_observados"] == dias_del_mes
            fila["meses"].append(
                {
                    "mes": mes,
                    "etiqueta": MESES[mes - 1],
                    "dias_del_mes": dias_del_mes,
                    "dias_en_intervalo": registro["dias_en_intervalo"],
                    "dias_con_cobertura": registro["dias_con_cobertura"],
                    "dias_observados": registro["dias_observados"],
                    "dias_sin_dato": registro["dias_con_cobertura"] - registro["dias_observados"],
                    "dias_fuera_intervalo": dias_del_mes - registro["dias_en_intervalo"],
                    "unidades": registro["unidades"],
                    "promedio_diario": (
                        round(registro["unidades"] / registro["dias_observados"], 4)
                        if registro["dias_observados"]
                        else None
                    ),
                    "dias_cero": registro["dias_cero"],
                    "completo": completo,
                    "parcial": not completo,
                    "motivo": _motivo(anio, mes, dias_del_mes, registro),
                }
            )
    return {
        "anios": anios,
        "nota": (
            "Cada mes se compara con días del mes, días del intervalo solicitado, días con "
            "cobertura y días observados, que son cosas distintas. Un mes incompleto se marca "
            "y declara su límite (inicio de los datos, fin de los datos o periodo). El promedio "
            "divide entre días observados; nunca se rellenan días faltantes con cero."
        ),
        "cobertura": {
            "desde": cobertura["desde"].isoformat(),
            "hasta": cobertura["hasta"].isoformat(),
            "periodo_desde": ventana.desde.isoformat(),
            "periodo_hasta": ventana.hasta.isoformat(),
        },
    }


def estacionalidad(
    db: Database, version: str, *, filtros: dict, fechas: dict[int, date], dimension: str = "estado"
) -> dict:
    """Estacionalidad semanal y mensual del contexto seleccionado."""
    if dimension not in DIMENSIONES:
        raise FiltroInvalido(
            f"Dimensión desconocida: «{dimension}». Valores válidos: {', '.join(DIMENSIONES)}"
        )
    inicio = time.perf_counter()
    ventana = filtros["ventana"]
    dias = consultas._dias_de(ventana, fechas)
    cobertura = consultas.cobertura_publicada(db, version)
    series = _series_por_dimension(db, version, filtros=filtros, dias=dias, dimension=dimension)
    semanal = _semanal(series, dias, fechas)
    contexto = consultas._vector_contexto(
        db,
        version,
        tiendas=list(filtros["tiendas"]),
        categorias=list(filtros["categorias"]),
        departamentos=list(filtros["departamentos"]),
        dias=dias,
    )
    mensual = _mensual(contexto, dias, fechas, ventana=ventana, cobertura=cobertura)
    dias_solicitados = [fecha for fecha in _rango_de_fechas(ventana.desde, ventana.hasta)]
    dias_fuera = [
        fecha
        for fecha in dias_solicitados
        if not (cobertura["desde"] <= fecha <= cobertura["hasta"])
    ]
    return {
        "unidad": UNIDAD,
        "dimension": dimension,
        "dimensiones": list(DIMENSIONES),
        "periodo": {
            "desde": ventana.desde.isoformat(),
            "hasta": ventana.hasta.isoformat(),
            "dias": ventana.dias,
        },
        "dominio": {
            "desde": date(cobertura["desde"].year, 1, 1).isoformat(),
            "hasta": date(max(fechas.values()).year, 12, 31).isoformat(),
            "nota": (
                "El periodo se puede pedir desde el 1 de enero del primer año con datos hasta "
                "el 31 de diciembre del último: las fechas sin cobertura se marcan como tales "
                "y no se rellenan con cero."
            ),
        },
        "cobertura_ventas": {
            "desde": cobertura["desde"].isoformat(),
            "hasta": cobertura["hasta"].isoformat(),
            "dias_solicitados": len(dias_solicitados),
            "dias_fuera_de_cobertura": len(dias_fuera),
        },
        "semanal": {
            "grupos": semanal["grupos"],
            "escala_comun_max": semanal["escala_comun_max"],
            "metrica_inicial": "promedio",
            "metricas": ["promedio", "total"],
            "regla": (
                "Por día de la semana se suman las unidades y se cuentan las fechas con "
                "observación; el promedio divide entre esas fechas, no entre semanas del "
                "calendario. Un día con cero ventas cuenta como observado."
            ),
            "advertencia": (
                "Patrón observado en el periodo, no un efecto causal del día de la semana."
            ),
        },
        "mensual": {
            **mensual,
            "metrica_inicial": "promedio_diario",
            "metricas": ["total", "promedio_diario"],
        },
        "calculo_ms": round((time.perf_counter() - inicio) * 1000, 1),
    }


# --------------------------------------------------------------------------- #
# Matriz temporal
# --------------------------------------------------------------------------- #


def matriz_temporal(
    db: Database, version: str, *, filtros: dict, fechas: dict[int, date], anio: int
) -> dict:
    """Matriz tienda × día de un año **completo**, con el estado de cada fecha.

    El eje es el año del calendario: 365 posiciones (366 en bisiesto), no solo los días con
    ventas. Cada celda declara su estado:

    - ``observado``: fecha dentro del periodo y de la cobertura, con valor del agregado.
    - ``sin_dato``: fecha sin cobertura de ventas (antes del inicio o después del último día
      publicado). No es un cero.
    - ``fuera_del_filtro``: fecha con cobertura pero fuera del periodo solicitado.

    La escala de color es común a las tiendas visibles y su máximo se declara; no se recorta
    ningún extremo. Los totales observados no cambian: solo se explicita qué días no tienen
    dato en lugar de omitirlos.
    """
    inicio_ms = time.perf_counter()
    cobertura = consultas.cobertura_publicada(db, version)
    ventana = filtros["ventana"]
    primero = date(anio, 1, 1)
    ultimo = date(anio, 12, 31)
    eje = _rango_de_fechas(primero, ultimo)
    dia_de_fecha = {fecha: dia for dia, fecha in fechas.items() if fecha.year == anio}
    dias_ventana = [
        dia for fecha, dia in sorted(dia_de_fecha.items()) if ventana.desde <= fecha <= ventana.hasta
    ]
    if not dias_ventana:
        raise FiltroInvalido(
            f"El año {anio} no tiene días dentro del periodo solicitado "
            f"({ventana.desde.isoformat()}–{ventana.hasta.isoformat()})."
        )
    tiendas = list(filtros["tiendas"])
    cal = consultas.catalogo(db, version)
    estado_de = {tienda["store_id"]: tienda["state_id"] for tienda in cal["tiendas"]}
    vectores: dict[str, list[int | None]] = {}
    for tienda in sorted(tiendas):
        valores = _vector_tienda(db, version, filtros=filtros, tienda=tienda, dias=dias_ventana)
        vectores[tienda] = valores
    indice_de_dia = {dia: posicion for posicion, dia in enumerate(dias_ventana)}

    filas = []
    maximo = 0
    conteo_estados = {"observado": 0, "sin_dato": 0, "fuera_del_filtro": 0}
    for tienda in sorted(tiendas):
        valores: list[int | None] = []
        total = 0
        observados = 0
        for fecha in eje:
            dentro_cobertura = cobertura["desde"] <= fecha <= cobertura["hasta"]
            dentro_ventana = ventana.desde <= fecha <= ventana.hasta
            dia = dia_de_fecha.get(fecha)
            if not dentro_cobertura:
                valores.append(None)
                conteo_estados["sin_dato"] += 1
                continue
            if not dentro_ventana:
                valores.append(None)
                conteo_estados["fuera_del_filtro"] += 1
                continue
            posicion = indice_de_dia.get(dia) if dia is not None else None
            valor = vectores[tienda][posicion] if posicion is not None and posicion < len(vectores[tienda]) else None
            valores.append(valor)
            if valor is None:
                conteo_estados["sin_dato"] += 1
                continue
            conteo_estados["observado"] += 1
            observados += 1
            total += int(valor)
            if int(valor) > maximo:
                maximo = int(valor)
        filas.append(
            {
                "store_id": tienda,
                "state_id": estado_de.get(tienda),
                "valores": valores,
                "total": total,
                "dias_observados": observados,
            }
        )

    meses = []
    for mes in range(1, 13):
        indices = [indice for indice, fecha in enumerate(eje) if fecha.month == mes]
        if indices:
            meses.append(
                {
                    "mes": mes,
                    "etiqueta": MESES[mes - 1],
                    "indice": indices[0],
                    "dias": len(indices),
                }
            )
    estados = [
        {
            "fecha": fecha.isoformat(),
            "d": dia_de_fecha.get(fecha),
            "mes": fecha.month,
            "estado": (
                "fuera_del_filtro"
                if cobertura["desde"] <= fecha <= cobertura["hasta"]
                and not (ventana.desde <= fecha <= ventana.hasta)
                else (
                    "sin_dato"
                    if not (cobertura["desde"] <= fecha <= cobertura["hasta"])
                    else "observado"
                )
            ),
        }
        for fecha in eje
    ]
    return {
        "anio": anio,
        "unidad": UNIDAD,
        "dias": estados,
        "meses": meses,
        "tiendas": filas,
        "escala": {
            "dominio": [0, maximo],
            "tipo": "lineal desde cero",
            "nota": (
                "Escala común a las tiendas visibles. El máximo del dominio es el mayor "
                "valor observado del año en este contexto: no se recorta ningún extremo."
            ),
        },
        "cobertura": {
            "desde": cobertura["desde"].isoformat(),
            "hasta": cobertura["hasta"].isoformat(),
            "periodo_desde": ventana.desde.isoformat(),
            "periodo_hasta": ventana.hasta.isoformat(),
            "dias_del_anio": len(eje),
            "celdas": len(eje) * len(filas),
            "celdas_observadas": conteo_estados["observado"],
            "celdas_sin_dato": conteo_estados["sin_dato"],
            "celdas_fuera_filtro": conteo_estados["fuera_del_filtro"],
            "nota": (
                "«Sin dato» es una fecha fuera de la cobertura de ventas publicada y «fuera "
                "del filtro» una fecha con cobertura que el periodo solicitado excluye: "
                "ninguna de las dos es una venta de cero. Un cero observado se dibuja como cero."
            ),
        },
        "calculo_ms": round((time.perf_counter() - inicio_ms) * 1000, 1),
    }


def _semanas_del_periodo(
    db: Database, *, desde: date, hasta: date, fechas: dict[int, date]
) -> dict[int, list[date]]:
    """Semanas ``wm_yr_wk`` del periodo con sus fechas reales (una sola lectura)."""
    dias = [dia for dia, fecha in fechas.items() if desde <= fecha <= hasta]
    if not dias:
        return {}
    semanas: dict[int, list[date]] = {}
    for documento in db[M5_CALENDARIO].find({"d": {"$in": dias}}, {"d": 1, "wm_yr_wk": 1}):
        semana = documento.get("wm_yr_wk")
        if not semana:
            continue
        semanas.setdefault(int(semana), []).append(fechas[int(documento["d"])])
    return {semana: sorted(valor) for semana, valor in semanas.items()}


def _precios_semanales(
    db: Database, *, tienda: str, item: str, desde: date, hasta: date, fechas: dict[int, date]
) -> dict:
    """Precio semanal del artículo en la tienda, con los límites reales de cada semana."""
    manifiesto = manifiesto_vigente(db)
    if not manifiesto:
        return {"version_precios": None, "puntos": [], "motivo": "no hay precios importados"}
    semanas = _semanas_del_periodo(db, desde=desde, hasta=hasta, fechas=fechas)
    puntos = []
    # Lectura común a los dos esquemas de precios (local y paquete de publicación).
    precios = precios_almacen.precios_por_semana(
        db, manifiesto["version_precios"], tienda, list(semanas)
    )
    for wm_yr_wk, lectura in precios.items():
        if item not in lectura:
            continue
        fechas_semana = semanas.get(int(wm_yr_wk), [])
        puntos.append(
            {
                "wm_yr_wk": int(wm_yr_wk),
                "inicio": fechas_semana[0].isoformat() if fechas_semana else None,
                "fin": fechas_semana[-1].isoformat() if fechas_semana else None,
                "precio": round(float(lectura[item]), 2),
            }
        )
    puntos.sort(key=lambda punto: punto["wm_yr_wk"])
    return {
        "version_precios": manifiesto["version_precios"],
        "puntos": puntos,
        "motivo": None if puntos else "sin precio publicado para este artículo en el periodo",
    }


def _capas_calendario(
    db: Database, *, estado: str | None, desde: date, hasta: date, fechas: dict[int, date]
) -> dict:
    """Días SNAP del estado de la tienda y eventos del calendario en el periodo."""
    if not estado:
        return {"snap": [], "eventos": [], "motivo": "la tienda no declara estado"}
    campo = f"snap_{estado.lower()}"
    dias = [dia for dia, fecha in fechas.items() if desde <= fecha <= hasta]
    if not dias:
        return {"snap": [], "eventos": []}
    snap = []
    eventos = []
    for documento in db[M5_CALENDARIO].find(
        {"d": {"$in": dias}},
        {"d": 1, "fecha": 1, campo: 1, "evento_1": 1, "evento_2": 1, "tipo_1": 1, "tipo_2": 1},
    ).sort("d", 1):
        fecha = fechas[int(documento["d"])]
        if int(documento.get(campo) or 0) == 1:
            snap.append({"fecha": fecha.isoformat(), "d": int(documento["d"])})
        for sufijo in ("1", "2"):
            nombre = documento.get(f"evento_{sufijo}")
            if nombre:
                eventos.append(
                    {
                        "fecha": fecha.isoformat(),
                        "d": int(documento["d"]),
                        "nombre": nombre,
                        "tipo": documento.get(f"tipo_{sufijo}"),
                    }
                )
    return {
        "snap": snap,
        "eventos": eventos,
        "estado": estado,
        "nota": (
            "Las marcas muestran coincidencias temporales; no identifican efectos causales. "
            "SNAP corresponde a los flags reales del estado de la tienda."
        ),
    }


def intermitencia_serie(vector: list[int | None], dias: list[int], fechas: dict[int, date]) -> dict:
    """Resumen de ceros y actividad de una serie, sin atravesar huecos.

    Una racha de ceros se corta cuando aparece un día **sin observación**: un dato ausente
    no es un cero. Las fechas se etiquetan como «primera/última venta positiva observada»,
    nunca como apertura, cierre, inventario ni rotura de stock.
    """
    validos = 0
    positivos = 0
    ceros = 0
    racha = 0
    racha_maxima = 0
    primera: date | None = None
    ultima: date | None = None
    for indice, dia in enumerate(dias):
        valor = vector[indice] if indice < len(vector) else None
        if valor is None:
            racha = 0
            continue
        validos += 1
        if int(valor) > 0:
            positivos += 1
            racha = 0
            fecha = fechas.get(dia)
            if fecha is not None:
                if primera is None:
                    primera = fecha
                ultima = fecha
        else:
            ceros += 1
            racha += 1
            if racha > racha_maxima:
                racha_maxima = racha
    return {
        "dias_validos": validos,
        "dias_positivos": positivos,
        "dias_cero": ceros,
        "pct_cero": round(100 * ceros / validos, 2) if validos else None,
        "racha_maxima_ceros": racha_maxima,
        "primera_venta_positiva": primera.isoformat() if primera else None,
        "ultima_venta_positiva": ultima.isoformat() if ultima else None,
        "unidades": int(sum(int(valor) for valor in vector if valor is not None)),
    }


def detalle_articulo(
    db: Database,
    version: str,
    *,
    filtros: dict,
    fechas: dict[int, date],
    item_id: str,
    tienda: str,
) -> dict:
    """Detalle de un artículo × tienda: ventas diarias, precio semanal y capas de calendario."""
    catalogo = _catalogo_productos(db, version)
    if item_id not in catalogo:
        raise FiltroInvalido(f"Artículo inexistente en la versión publicada: {item_id}")
    if tienda not in {t["store_id"] for t in consultas.catalogo(db, version)["tiendas"]}:
        raise FiltroInvalido(f"Tienda inexistente: {tienda}")
    ventana = filtros["ventana"]
    dias = consultas._dias_de(ventana, fechas)
    vector = _serie_bloques(db, version, tienda=tienda, item=item_id, dias=dias)
    if all(valor is None for valor in vector):
        raise FiltroInvalido(
            f"El artículo {item_id} no tiene series importadas en la tienda {tienda}."
        )
    estado = next(
        (
            documento["state_id"]
            for documento in db[M5_CATALOGO].find(
                {"version": version, "tipo": "tienda", "store_id": tienda}, {"state_id": 1}
            )
        ),
        None,
    )
    datos_item = catalogo[item_id]
    precios = _precios_semanales(
        db, tienda=tienda, item=item_id, desde=ventana.desde, hasta=ventana.hasta, fechas=fechas
    )
    capas = _capas_calendario(
        db, estado=estado, desde=ventana.desde, hasta=ventana.hasta, fechas=fechas
    )
    resumen = intermitencia_serie(vector, dias, fechas)
    return {
        "articulo": {
            "item_id": item_id,
            "tienda": tienda,
            "estado": estado,
            "cat_id": datos_item["cat_id"],
            "dept_id": datos_item["dept_id"],
            "categoria": consultas.CATEGORIAS_NOMBRE.get(datos_item["cat_id"], datos_item["cat_id"]),
        },
        "periodo": {
            "desde": ventana.desde.isoformat(),
            "hasta": ventana.hasta.isoformat(),
            "dias": ventana.dias,
        },
        "unidad": UNIDAD,
        "ventas": [
            {"d": dia, "fecha": fechas[dia].isoformat(), "unidades": vector[indice]}
            for indice, dia in enumerate(dias)
            if dia in fechas
        ],
        "precios": precios,
        "capas": capas,
        "intermitencia": resumen,
        "nota": (
            "Ventas observadas y precio semanal son dos métricas distintas: no se multiplican "
            "ni se convierten en ingresos. Los cambios de precio no son promociones."
        ),
    }


# --------------------------------------------------------------------------- #
# Intermitencia por series de una tienda
# --------------------------------------------------------------------------- #


def _estadisticas_matriz(
    matriz: np.ndarray, inicio: int, fin: int
) -> dict[str, np.ndarray]:
    """Estadísticas por serie sobre la ventana ``[inicio, fin)`` de la matriz.

    La matriz usa ``-1`` para los días **sin dato**: un ausente corta la racha de ceros y
    no entra en los denominadores.
    """
    recorte = matriz[:, inicio:fin]
    observado = recorte >= 0
    positivos = observado & (recorte > 0)
    ceros = observado & (recorte == 0)
    validos = observado.sum(axis=1)
    # Racha máxima de ceros sin atravesar un día sin dato.
    rachas = np.zeros(recorte.shape[0], dtype=np.int32)
    maxima = np.zeros(recorte.shape[0], dtype=np.int32)
    for columna in range(recorte.shape[1]):
        sin_dato = ~observado[:, columna]
        es_cero = ceros[:, columna]
        rachas = np.where(sin_dato, 0, np.where(es_cero, rachas + 1, 0))
        maxima = np.maximum(maxima, rachas)
    return {
        "validos": validos,
        "positivos": positivos.sum(axis=1),
        "ceros": ceros.sum(axis=1),
        "racha": maxima,
        "unidades": np.where(observado, recorte, 0).sum(axis=1),
        "primera": np.where(positivos.any(axis=1), positivos.argmax(axis=1), -1),
        "ultima": np.where(
            positivos.any(axis=1),
            positivos.shape[1] - 1 - positivos[:, ::-1].argmax(axis=1),
            -1,
        ),
    }


def matriz_tienda(
    db: Database,
    version: str,
    *,
    tienda: str,
    desde_d: int,
    hasta_d: int,
) -> tuple[list[str], np.ndarray]:
    """Matriz ``(series, días)`` de una tienda, con ``-1`` en los días sin dato.

    Solo se leen y decodifican los bloques que **solapan** el intervalo pedido: un
    bloque guarda 196 días contiguos, así que una ventana de un año toca dos bloques por
    serie en lugar de los diez del histórico completo.
    """
    if hasta_d < desde_d:
        return [], np.zeros((0, 0), dtype=np.int32)
    documentos = db[M5_BLOQUES].find(
        {
            "version": version,
            "store_id": tienda,
            "d_inicio": {"$lte": hasta_d},
            "d_fin": {"$gte": desde_d},
        },
        {"item_id": 1, "d_inicio": 1, "unidades": 1},
    )
    items: list[str] = []
    posiciones: dict[str, int] = {}
    bloques: list[tuple[int, int, list[int]]] = []
    for documento in documentos:
        item = documento["item_id"]
        if item not in posiciones:
            posiciones[item] = len(items)
            items.append(item)
        bloques.append((posiciones[item], int(documento["d_inicio"]), documento["unidades"]))
    matriz = np.full((len(items), hasta_d - desde_d + 1), -1, dtype=np.int32)
    for fila, inicio, unidades in bloques:
        # El bloque cubre [inicio, inicio + len). Se recorta a la ventana pedida.
        primero = max(inicio, desde_d)
        ultimo = min(inicio + len(unidades) - 1, hasta_d)
        if ultimo < primero:
            continue
        matriz[fila, primero - desde_d : ultimo - desde_d + 1] = unidades[
            primero - inicio : ultimo - inicio + 1
        ]
    return items, matriz



def intermitencia(
    db: Database,
    version: str,
    *,
    tienda: str,
    desde: date,
    hasta: date,
    fechas: dict[int, date],
    categorias: list[str] | None = None,
    departamentos: list[str] | None = None,
    pagina: int = 1,
    tamano: int = 25,
    orden: str = "ceros",
) -> dict:
    """Resumen de intermitencia de las series artículo × tienda de una tienda.

    Incluye **todas** las series de la tienda (también las de ventas siempre cero): contar
    solo las positivas sesgaría el porcentaje de ceros.
    """
    inicio_ms = time.perf_counter()
    if tienda not in {t["store_id"] for t in consultas.catalogo(db, version)["tiendas"]}:
        raise FiltroInvalido(f"Tienda inexistente: {tienda}")
    ordenes = ("ceros", "unidades", "racha")
    if orden not in ordenes:
        raise FiltroInvalido(f"Orden desconocido: «{orden}». Valores válidos: {', '.join(ordenes)}")
    tamano = max(1, min(int(tamano), MAX_SERIES_TABLA))
    pagina = max(1, int(pagina))
    dias = [dia for dia in sorted(fechas) if desde <= fechas[dia] <= hasta]
    if not dias:
        raise FiltroInvalido("El periodo no tiene días de calendario.")
    items, matriz = matriz_tienda(
        db, version, tienda=tienda, desde_d=dias[0], hasta_d=dias[-1]
    )
    estadisticas = _estadisticas_matriz(matriz, 0, matriz.shape[1])
    catalogo = _catalogo_productos(db, version)
    categorias = list(categorias or [])
    departamentos = list(departamentos or [])
    seleccion = np.ones(len(items), dtype=bool)
    if categorias or departamentos:
        for posicion, item in enumerate(items):
            datos = catalogo.get(item, {})
            if categorias and datos.get("cat_id") not in categorias:
                seleccion[posicion] = False
            if departamentos and datos.get("dept_id") not in departamentos:
                seleccion[posicion] = False
    indices = np.flatnonzero(seleccion)
    validos = estadisticas["validos"][indices]
    ceros = estadisticas["ceros"][indices]
    proporcion = np.where(validos > 0, ceros / np.maximum(validos, 1), np.nan)
    orden_indices = {
        "ceros": np.argsort(-np.nan_to_num(proporcion, nan=-1), kind="stable"),
        "unidades": np.argsort(-estadisticas["unidades"][indices], kind="stable"),
        "racha": np.argsort(-estadisticas["racha"][indices], kind="stable"),
    }[orden]
    total = int(indices.size)
    desde_indice = (pagina - 1) * tamano
    pagina_indices = orden_indices[desde_indice : desde_indice + tamano]
    filas = []
    for posicion in pagina_indices:
        indice = int(indices[int(posicion)])
        item = items[indice]
        datos = catalogo.get(item, {})
        primera = int(estadisticas["primera"][indice])
        ultima = int(estadisticas["ultima"][indice])
        filas.append(
            {
                "item_id": item,
                "cat_id": datos.get("cat_id"),
                "dept_id": datos.get("dept_id"),
                "store_id": tienda,
                "dias_validos": int(estadisticas["validos"][indice]),
                "dias_positivos": int(estadisticas["positivos"][indice]),
                "dias_cero": int(estadisticas["ceros"][indice]),
                "pct_cero": (
                    round(100 * float(estadisticas["ceros"][indice]) / float(estadisticas["validos"][indice]), 2)
                    if estadisticas["validos"][indice]
                    else None
                ),
                "racha_maxima_ceros": int(estadisticas["racha"][indice]),
                "unidades": int(estadisticas["unidades"][indice]),
                "primera_venta_positiva": fechas[dias[primera]].isoformat() if primera >= 0 else None,
                "ultima_venta_positiva": fechas[dias[ultima]].isoformat() if ultima >= 0 else None,
            }
        )
    con_datos = validos > 0
    valores = proporcion[con_datos & ~np.isnan(proporcion)]
    tramos = [
        {"etiqueta": "0–25 %", "desde": 0.0, "hasta": 0.25},
        {"etiqueta": "25–50 %", "desde": 0.25, "hasta": 0.5},
        {"etiqueta": "50–75 %", "desde": 0.5, "hasta": 0.75},
        {"etiqueta": "75–100 %", "desde": 0.75, "hasta": 1.0},
    ]
    distribucion = [
        {
            "etiqueta": tramo["etiqueta"],
            "series": int(((valores >= tramo["desde"]) & (valores < tramo["hasta"])).sum())
            if tramo["hasta"] < 1.0
            else int(((valores >= tramo["desde"]) & (valores <= tramo["hasta"])).sum()),
        }
        for tramo in tramos
    ]
    todos_cero = int(((validos > 0) & (ceros == validos)).sum())
    return {
        "tienda": tienda,
        "unidad": UNIDAD,
        "periodo": {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "dias": len(dias)},
        "resumen": {
            "series": total,
            "series_con_datos": int(con_datos.sum()),
            "series_sin_datos": int((~con_datos).sum()),
            "series_todas_cero": todos_cero,
            "pct_cero_mediana": round(100 * float(np.median(valores)), 2) if valores.size else None,
            "pct_cero_media": round(100 * float(valores.mean()), 2) if valores.size else None,
            "distribucion": distribucion,
        },
        "filas": filas,
        "paginacion": {
            "pagina": pagina,
            "tamano": tamano,
            "total": total,
            "paginas": (total + tamano - 1) // tamano if tamano else 0,
            "orden": orden,
        },
        "reglas": (
            "Un día sin dato no cuenta como cero e interrumpe la racha. Las fechas son "
            "«primera/última venta positiva observada», no apertura, cierre, inventario ni "
            "rotura de stock. Se incluyen las series con ventas siempre cero."
        ),
        "calculo_ms": round((time.perf_counter() - inicio_ms) * 1000, 1),
    }
