"""Lectura de agregados para las vistas públicas del Atlas comercial.

Todo se resuelve sobre lo que la importación dejó calculado:

- ``m5_agregados`` (vector diario por dimensión) para las series por tienda, por
  departamento (dimensión derivada ``departamento_tienda``) y la comparación A/B.
- ``m5_bloques`` (producto × tienda en bloques de días) para las series de producto,
  la concentración y la búsqueda de productos por volumen.
- ``m5_calendario`` para traducir ``d_N`` a fecha y para las semanas de lunes a
  domingo.

Jerarquía de exploración (dos ramas que no se mezclan):

- **Territorio**: estado → tienda.
- **Productos**: categoría → departamento → producto.

Flujo servido aquí: tiendas → departamentos de una tienda → productos de un
departamento de esa tienda. Los filtros globales (categorías, departamentos,
periodo, A/B) se respetan en los tres niveles y nunca se rellenan paneles con
categorías excluidas.

Reglas que se aplican aquí:

- Los periodos son intervalos **inclusivos** de días comerciales.
- Un día sin observación se devuelve como ``None``; nunca se convierte en cero.
- Las series comparten el mismo dominio temporal.
- La concentración es acumulada descendente sobre productos con ventas positivas
  en el intervalo; su total reconcilia con el resumen y con las series.
- Una selección inválida es un error, nunca «todos».
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from pymongo.database import Database

from app.comercial import derivados, fuentes
from app.comercial.esquema import ErrorEsquema, Ventana
from app.db.mongo import M5_AGREGADOS, M5_BLOQUES, M5_CALENDARIO, M5_CATALOGO, M5_IMPORTACIONES

AGREGACIONES = ("dia", "semana", "mes")

NIVELES = ("tienda", "departamento", "producto")
"""Niveles de exploración de «Pulso de ventas»."""

NIVEL_POR_OMISION = "tienda"

MAX_PRODUCTOS = 5
"""Tope de series de producto en una misma respuesta (no se dibujan 3 049 paneles)."""

ESTADOS_NOMBRE = {"CA": "California", "TX": "Texas", "WI": "Wisconsin"}
"""Nombre de presentación del estado. El código original se conserva en los datos."""

CATEGORIAS_NOMBRE = {"FOODS": "Alimentos", "HOBBIES": "Aficiones", "HOUSEHOLD": "Hogar"}
"""Etiqueta de presentación de la categoría; el código original se conserva."""


class FiltroInvalido(ErrorEsquema):
    """Un filtro pedido no existe en la versión publicada."""


# --------------------------------------------------------------------------- #
# Catálogo y validación
# --------------------------------------------------------------------------- #


def catalogo(db: Database, version: str) -> dict:
    """Catálogo de la versión: tiendas, estados, categorías, departamentos."""
    documentos = list(
        db[M5_CATALOGO].find({"version": version}, {"tipo": 1, "store_id": 1, "state_id": 1,
                                                   "cat_id": 1, "dept_id": 1, "item_id": 1})
    )
    tiendas = sorted(
        ({"store_id": d["store_id"], "state_id": d["state_id"]} for d in documentos if d["tipo"] == "tienda"),
        key=lambda x: x["store_id"],
    )
    estados = sorted({t["state_id"] for t in tiendas})
    categorias = sorted({d["cat_id"] for d in documentos if d["tipo"] == "categoria"})
    departamentos = sorted(
        ({"cat_id": d["cat_id"], "dept_id": d["dept_id"]} for d in documentos if d["tipo"] == "departamento"),
        key=lambda x: (x["cat_id"], x["dept_id"]),
    )
    productos = sum(1 for d in documentos if d["tipo"] == "producto")
    return {
        "tiendas": tiendas,
        "estados": estados,
        "categorias": categorias,
        "departamentos": departamentos,
        "productos": productos,
    }


def buscar_productos(
    db: Database, version: str, *, texto: str | None, limite: int, desplazamiento: int
) -> tuple[list[dict], int]:
    """Búsqueda paginada de productos por identificador, categoría o departamento."""
    consulta: dict = {"version": version, "tipo": "producto"}
    if texto:
        patron = {"$regex": texto.strip(), "$options": "i"}
        consulta["$or"] = [{"item_id": patron}, {"cat_id": patron}, {"dept_id": patron}]
    total = db[M5_CATALOGO].count_documents(consulta)
    documentos = (
        db[M5_CATALOGO]
        .find(consulta, {"item_id": 1, "cat_id": 1, "dept_id": 1, "_id": 0})
        .sort("item_id", 1)
        .skip(max(0, desplazamiento))
        .limit(max(1, limite))
    )
    return list(documentos), total


def resolver_filtros(db: Database, version: str, filtros: dict) -> dict:
    """Valida códigos y fechas contra la cobertura real publicada.

    Una selección inválida **no** significa «todos»: se informa del error.
    """
    cal = catalogo(db, version)
    tiendas = filtros.get("tiendas") or [t["store_id"] for t in cal["tiendas"]]
    desconocidas = [t for t in tiendas if t not in {v["store_id"] for v in cal["tiendas"]}]
    if desconocidas:
        raise FiltroInvalido(f"Tiendas inexistentes: {', '.join(sorted(desconocidas))}")

    categorias = filtros.get("categorias") or list(cal["categorias"])
    malas = [c for c in categorias if c not in set(cal["categorias"])]
    if malas:
        raise FiltroInvalido(f"Categorías inexistentes: {', '.join(sorted(malas))}")

    departamentos = filtros.get("departamentos") or []
    validos = {d["dept_id"] for d in cal["departamentos"]}
    malos = [d for d in departamentos if d not in validos]
    if malos:
        raise FiltroInvalido(f"Departamentos inexistentes: {', '.join(sorted(malos))}")

    productos = filtros.get("productos") or []
    if productos:
        existentes = {
            d["item_id"]
            for d in db[M5_CATALOGO].find(
                {"version": version, "tipo": "producto", "item_id": {"$in": productos}}, {"item_id": 1}
            )
        }
        faltan = [p for p in productos if p not in existentes]
        if faltan:
            raise FiltroInvalido(f"Productos inexistentes: {', '.join(sorted(faltan)[:5])}")

    cobertura = cobertura_publicada(db, version)
    desde = filtros.get("desde") or cobertura["desde"]
    hasta = filtros.get("hasta") or cobertura["hasta"]
    if isinstance(desde, str):
        desde = date.fromisoformat(desde)
    if isinstance(hasta, str):
        hasta = date.fromisoformat(hasta)
    if desde < cobertura["desde"] or hasta > cobertura["hasta"]:
        raise FiltroInvalido(
            f"El intervalo {desde}–{hasta} sale de la cobertura {cobertura['desde']}–{cobertura['hasta']}"
        )
    ventana = Ventana(desde, hasta)

    def _ventana(desde_valor, hasta_valor, nombre: str) -> Ventana | None:
        if desde_valor is None and hasta_valor is None:
            return None
        if desde_valor is None or hasta_valor is None:
            raise FiltroInvalido(f"El intervalo {nombre} necesita inicio y fin")
        if isinstance(desde_valor, str):
            desde_valor = date.fromisoformat(desde_valor)
        if isinstance(hasta_valor, str):
            hasta_valor = date.fromisoformat(hasta_valor)
        if desde_valor < cobertura["desde"] or hasta_valor > cobertura["hasta"]:
            raise FiltroInvalido(f"El intervalo {nombre} sale de la cobertura publicada")
        if not ventana.contiene(Ventana(desde_valor, hasta_valor)):
            raise FiltroInvalido(f"El intervalo {nombre} debe estar dentro del intervalo principal")
        return Ventana(desde_valor, hasta_valor)

    a = _ventana(filtros.get("a_desde"), filtros.get("a_hasta"), "A")
    b = _ventana(filtros.get("b_desde"), filtros.get("b_hasta"), "B")
    ab_por_omision = False
    if a is None and b is None:
        # Sin intervalos explícitos se comparan las dos mitades consecutivas del
        # intervalo principal. Se informa en la respuesta: no es una elección oculta.
        total_dias = ventana.dias
        if total_dias >= 2:
            mitad = total_dias // 2
            a = Ventana(ventana.desde, ventana.desde + timedelta(days=mitad - 1))
            b = Ventana(ventana.desde + timedelta(days=mitad), ventana.hasta)
            ab_por_omision = True

    # --- nivel de exploración ------------------------------------------------
    contexto = _resolver_nivel(
        db,
        version,
        cal=cal,
        nivel=filtros.get("nivel"),
        tienda=filtros.get("tienda"),
        estado=filtros.get("estado"),
        departamento=filtros.get("departamento"),
        tiendas=sorted(tiendas),
        categorias=sorted(categorias),
        departamentos=sorted(departamentos),
        productos=sorted(productos),
        ventana=ventana,
    )

    return {
        "tiendas": contexto["tiendas_efectivas"],
        "tiendas_solicitadas": sorted(tiendas),
        "categorias": sorted(categorias),
        "departamentos": sorted(departamentos),
        "productos": sorted(productos),
        "ventana": ventana,
        "a": a,
        "b": b,
        "ab_por_omision": ab_por_omision,
        **contexto,
    }


def _resolver_nivel(
    db: Database,
    version: str,
    *,
    cal: dict,
    nivel: str | None,
    tienda: str | None,
    estado: str | None,
    departamento: str | None,
    tiendas: list[str],
    categorias: list[str],
    departamentos: list[str],
    productos: list[str],
    ventana: Ventana,
) -> dict:
    """Valida el nivel y el contexto territorial/de catálogo de la petición.

    El territorio es una jerarquía: Estados Unidos → estado → tienda. El nivel de
    tiendas conserva el contrato anterior (una serie por tienda, con `estado` como
    filtro opcional). Los niveles de departamento y producto exigen **contexto
    territorial explícito** —una tienda (`tienda`) o un estado (`estado`)— porque
    agregan varias tiendas en una sola serie y nunca debe confundirse con el total
    de un solo punto de venta.
    """
    elegido = str(nivel or NIVEL_POR_OMISION).strip().lower()
    if elegido not in NIVELES:
        raise FiltroInvalido(
            f"Nivel de exploración desconocido: «{elegido}». Valores válidos: {', '.join(NIVELES)}"
        )
    tienda_limpia = str(tienda).strip() if tienda not in (None, "") else None
    estado_limpio = str(estado).strip() if estado not in (None, "") else None
    departamento_limpio = str(departamento).strip() if departamento not in (None, "") else None

    if estado_limpio and estado_limpio not in set(cal["estados"]):
        raise FiltroInvalido(f"Estado inexistente: {estado_limpio}")

    tiendas_del_estado = [
        t["store_id"] for t in cal["tiendas"] if not estado_limpio or t["state_id"] == estado_limpio
    ]
    if elegido == "tienda" and (tienda_limpia or departamento_limpio):
        raise FiltroInvalido(
            "«tienda» y «departamento» solo aplican al explorar departamentos o productos; "
            "en el nivel de tiendas el filtro territorial es «estado» o «tiendas»."
        )
    if elegido == "departamento" and departamento_limpio:
        raise FiltroInvalido(
            "En el nivel de departamentos el filtro es «departamentos» (lista), no «departamento»."
        )
    if elegido == "departamento" and productos:
        raise FiltroInvalido(
            "Para explorar productos envía nivel=producto con «departamento» y «productos»."
        )

    tiendas_contexto = [t for t in tiendas if t in set(tiendas_del_estado)]
    if elegido in ("departamento", "producto"):
        if not tienda_limpia and not estado_limpio:
            raise FiltroInvalido(
                f"El nivel «{elegido}» necesita contexto territorial explícito: "
                "una tienda («tienda») o un estado («estado»)."
            )
        if tienda_limpia:
            if tienda_limpia not in tiendas:
                raise FiltroInvalido(
                    f"La tienda {tienda_limpia} no está entre las tiendas seleccionadas: "
                    f"{', '.join(tiendas)}"
                )
            if tienda_limpia not in set(tiendas_del_estado):
                raise FiltroInvalido(
                    f"La tienda {tienda_limpia} no pertenece al estado {estado_limpio}."
                )
            tiendas_contexto = [tienda_limpia]
        if not tiendas_contexto:
            raise FiltroInvalido(
                "El contexto territorial no deja ninguna tienda que representar con los filtros actuales."
            )

    departamentos_panel: list[str] = []
    if elegido == "departamento":
        permitidos = _departamentos_del_contexto(db, version, categorias=categorias)
        if departamentos:
            fuera = [d for d in departamentos if d not in permitidos]
            if fuera:
                raise FiltroInvalido(
                    f"El departamento {fuera[0]} no pertenece a las categorías seleccionadas "
                    f"({', '.join(categorias)})"
                )
            departamentos_panel = sorted(set(departamentos))
        else:
            departamentos_panel = permitidos
        if not departamentos_panel:
            raise FiltroInvalido("Los filtros de categoría no dejan ningún departamento que mostrar")

    productos_contexto: list[str] = []
    seleccion_automatica = False
    if elegido == "producto":
        if not departamento_limpio:
            raise FiltroInvalido(
                "El nivel producto necesita un departamento explícito (parámetro «departamento»)."
            )
        categoria_de = {d["dept_id"]: d["cat_id"] for d in cal["departamentos"]}
        if departamento_limpio not in categoria_de:
            raise FiltroInvalido(f"Departamento inexistente: {departamento_limpio}")
        if categoria_de[departamento_limpio] not in categorias:
            raise FiltroInvalido(
                f"El departamento {departamento_limpio} pertenece a "
                f"{categoria_de[departamento_limpio]}, que está excluida por el filtro de categorías."
            )
        if departamentos and departamento_limpio not in departamentos:
            raise FiltroInvalido(
                f"El departamento {departamento_limpio} está excluido por el filtro de departamentos."
            )
        productos_contexto = _productos_de_departamento(db, version, departamento_limpio)
        if productos:
            if len(productos) > MAX_PRODUCTOS:
                raise FiltroInvalido(
                    f"Como máximo se representan {MAX_PRODUCTOS} productos; llegaron {len(productos)}."
                )
            disponibles = set(productos_contexto)
            ajenos = [producto for producto in productos if producto not in disponibles]
            if ajenos:
                raise FiltroInvalido(
                    f"El producto {ajenos[0]} no pertenece al departamento {departamento_limpio}."
                )
        else:
            productos = [
                seleccion["item_id"]
                for seleccion in seleccion_por_volumen(
                    db,
                    version,
                    tiendas=tiendas_contexto,
                    departamento=departamento_limpio,
                    ventana=ventana,
                )
            ]
            seleccion_automatica = True

    return {
        "nivel": elegido,
        "tienda": tienda_limpia,
        "estado": estado_limpio,
        "departamento": departamento_limpio,
        "departamentos_panel": departamentos_panel,
        "productos_contexto": productos_contexto,
        "productos": sorted(productos) if elegido == "producto" else productos,
        "seleccion_automatica": seleccion_automatica,
        # Al descender, el alcance territorial efectivo es el contexto (una tienda o las
        # tiendas de un estado): la concentración, el A/B y los totales se calculan sobre
        # ese contexto, no sobre todas las tiendas que estuvieran seleccionadas.
        "tiendas_efectivas": sorted(tiendas_contexto),
        "tiendas_contexto": sorted(tiendas_contexto),
    }


def _departamentos_del_contexto(db: Database, version: str, *, categorias: list[str]) -> list[str]:
    """Departamentos del catálogo permitidos por el filtro de categorías."""
    consulta = {"version": version, "tipo": "departamento"}
    if categorias:
        consulta["cat_id"] = {"$in": list(categorias)}
    return sorted(
        documento["dept_id"]
        for documento in db[M5_CATALOGO].find(consulta, {"dept_id": 1, "_id": 0})
    )


def _productos_de_departamento(db: Database, version: str, departamento: str) -> list[str]:
    """Identificadores reales de M5 del departamento, en orden estable."""
    return sorted(
        documento["item_id"]
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "producto", "dept_id": departamento},
            {"item_id": 1, "_id": 0},
        )
    )


def seleccion_por_volumen(
    db: Database,
    version: str,
    *,
    tiendas: list[str],
    departamento: str,
    ventana: Ventana,
    limite: int = MAX_PRODUCTOS,
) -> list[dict]:
    """Productos del departamento con más unidades en el periodo (desempate estable).

    Solo productos con ventas positivas: el orden por unidades sobre ceros no
    significaría nada. El desempate es por ``item_id`` ascendente. Las unidades se
    suman sobre el contexto territorial (una tienda o las tiendas de un estado).
    """
    productos = _productos_de_departamento(db, version, departamento)
    if not productos:
        return []
    totales = _totales_por_producto(
        db,
        version,
        productos=productos,
        tiendas=list(tiendas),
        desde=ventana.desde,
        hasta=ventana.hasta,
    )
    ordenado = sorted(totales.items(), key=lambda par: (-par[1], par[0]))[: max(1, limite)]
    return [{"item_id": item_id, "unidades": unidades} for item_id, unidades in ordenado]


def productos_del_contexto(
    db: Database, version: str, *, departamento: str | None, categorias: list[str]
) -> list[dict]:
    """Productos del contexto de catálogo, en orden estable por identificador."""
    consulta: dict = {"version": version, "tipo": "producto"}
    if departamento:
        consulta["dept_id"] = departamento
    if categorias:
        consulta["cat_id"] = {"$in": list(categorias)}
    return sorted(
        (
            {"item_id": documento["item_id"], "dept_id": documento["dept_id"], "cat_id": documento["cat_id"]}
            for documento in db[M5_CATALOGO].find(
                consulta, {"item_id": 1, "dept_id": 1, "cat_id": 1, "_id": 0}
            )
        ),
        key=lambda producto: producto["item_id"],
    )


def buscar_productos_contexto(
    db: Database,
    version: str,
    *,
    tiendas: list[str],
    departamento: str | None,
    categorias: list[str],
    desde: date,
    hasta: date,
    texto: str | None,
    orden: str,
    limite: int,
    desplazamiento: int,
) -> dict:
    """Búsqueda paginada de productos del contexto, con sus unidades reales.

    - Identificadores reales de M5 (``item_id``); no se inventan nombres.
    - Orden estable: ``unidades`` (descendente, desempate por identificador) o
      ``item_id`` (ascendente).
    - Las unidades son las del intervalo y el contexto territorial (una tienda o las
      tiendas de un estado). Un producto sin ventas positivas aparece con 0, no
      desaparece: la búsqueda es de catálogo.
    - ``universo`` es el total del departamento en ese mismo contexto y periodo: es el
      denominador de ``participacion_pct`` y de la participación conjunta de la
      selección. La suma de los cinco productos **no** es el total del departamento.
    """
    if orden not in ("unidades", "item_id"):
        raise FiltroInvalido(f"Orden no soportado: «{orden}». Usa «unidades» o «item_id».")
    catalogo = productos_del_contexto(
        db, version, departamento=departamento, categorias=categorias
    )
    productos_catalogo = len(catalogo)
    unidades = _totales_por_producto(
        db,
        version,
        productos=[producto["item_id"] for producto in catalogo],
        tiendas=list(tiendas),
        desde=desde,
        hasta=hasta,
    ) if catalogo else {}
    universo_unidades = sum(unidades.values())
    universo_productos = len(unidades)

    for producto in catalogo:
        producto["unidades"] = int(unidades.get(producto["item_id"], 0))
        producto["participacion_pct"] = (
            round(100 * producto["unidades"] / universo_unidades, 4) if universo_unidades > 0 else None
        )

    if texto:
        buscado = texto.strip().lower()
        catalogo = [producto for producto in catalogo if buscado in producto["item_id"].lower()]

    if orden == "unidades":
        catalogo.sort(key=lambda producto: (-producto["unidades"], producto["item_id"]))
    total = len(catalogo)
    seleccion_inicial = sorted(
        (producto for producto in catalogo if producto["unidades"] > 0),
        key=lambda producto: (-producto["unidades"], producto["item_id"]),
    )[:MAX_PRODUCTOS]
    pagina = catalogo[max(0, desplazamiento) : max(0, desplazamiento) + max(1, limite)]
    seleccion_unidades = sum(producto["unidades"] for producto in seleccion_inicial)
    return {
        "productos": pagina,
        "total": total,
        "universo": {
            "unidades": universo_unidades,
            "productos": universo_productos,
            "productos_catalogo": productos_catalogo,
        },
        "seleccion_inicial": [
            {
                "item_id": producto["item_id"],
                "unidades": producto["unidades"],
                "participacion_pct": producto["participacion_pct"],
            }
            for producto in seleccion_inicial
        ],
        "seleccion_inicial_participacion_pct": (
            round(100 * seleccion_unidades / universo_unidades, 4) if universo_unidades > 0 else None
        ),
        "orden": orden,
        "contexto": {
            "tiendas": sorted(tiendas),
            "departamento": departamento,
            "categorias": sorted(categorias),
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
        },
    }


def cobertura_publicada(db: Database, version: str) -> dict:
    """Días con **ventas** de la versión publicada (intervalo inclusivo).

    No se usa el rango del calendario: M5 publica 1969 días de calendario y 1941 con
    ventas. Tomar el calendario como cobertura haría que el tablero ofreciera fechas
    sin observación (los 28 días finales de ``calendar.csv``).
    """
    manifiesto = db[M5_IMPORTACIONES].find_one({"_id": version}) or {}
    cobertura = manifiesto.get("cobertura") or {}
    if cobertura.get("desde") and cobertura.get("hasta"):
        return {
            "desde": date.fromisoformat(str(cobertura["desde"])),
            "hasta": date.fromisoformat(str(cobertura["hasta"])),
            "dias": int(cobertura.get("dias") or 0),
            "dias_de_calendario": (manifiesto.get("calendario") or {}).get("dias_de_calendario"),
            "dias_calendario_sin_ventas": (manifiesto.get("calendario") or {}).get(
                "dias_calendario_sin_ventas"
            ),
            "version": version,
            "fuente": "manifiesto de importación",
        }

    # Respaldo: días realmente presentes en los agregados (no el calendario completo).
    documento = db[M5_AGREGADOS].find_one({"version": version, "dimension": "total"}, {"unidades": 1, "d_inicio": 1})
    if not documento:
        raise FiltroInvalido("No hay agregados importados para esta versión")
    primero = db[M5_CALENDARIO].find_one({"d": documento["d_inicio"]})
    ultimo = db[M5_CALENDARIO].find_one({"d": documento["d_inicio"] + len(documento["unidades"]) - 1})
    if not primero or not ultimo:
        raise FiltroInvalido("El calendario no cubre los días de ventas")
    return {
        "desde": _fecha(primero["fecha"]),
        "hasta": _fecha(ultimo["fecha"]),
        "dias": len(documento["unidades"]),
        "version": version,
        "fuente": "agregados",
    }


def _fecha(valor) -> date:
    return valor.date() if hasattr(valor, "date") else valor


def mapa_fechas(db: Database) -> dict[int, date]:
    """``d_N`` → fecha, para todo el calendario importado."""
    salida: dict[int, date] = {}
    for documento in db[M5_CALENDARIO].find({}, {"d": 1, "fecha": 1}):
        salida[int(documento["d"])] = _fecha(documento["fecha"])
    return salida


# --------------------------------------------------------------------------- #
# Series
# --------------------------------------------------------------------------- #


def _agrupar(fechas: list[date], valores: list[int | None], agregacion: str) -> dict:
    """Agrupa días en semana (lunes-domingo) o mes; los huecos no se imputan."""
    cubos: dict[str, dict] = {}
    for fecha, valor in zip(fechas, valores, strict=False):
        if agregacion == "dia":
            clave = fecha.isoformat()
            etiqueta = fecha.isoformat()
        elif agregacion == "semana":
            inicio = fuentes.inicio_semana(fecha)
            clave = inicio.isoformat()
            etiqueta = f"{inicio.isoformat()} – {fuentes.fin_semana(fecha).isoformat()}"
        else:
            clave = f"{fecha.year:04d}-{fecha.month:02d}"
            etiqueta = clave
        cubo = cubos.setdefault(clave, {"clave": clave, "etiqueta": etiqueta, "total": 0,
                                         "dias": 0, "dias_sin_observacion": 0, "parcial": False})
        if valor is None:
            cubo["dias_sin_observacion"] += 1
            continue
        cubo["total"] += valor
        cubo["dias"] += 1
    primero = min(fechas) if fechas else None
    ultimo = max(fechas) if fechas else None
    for cubo in cubos.values():
        inicio = date.fromisoformat(cubo["clave"]) if agregacion != "mes" else None
        if agregacion == "dia":
            cubo["parcial"] = False
        elif agregacion == "semana":
            cubo["parcial"] = bool(primero and ultimo and (inicio < primero or fuentes.fin_semana(inicio) > ultimo))
        else:
            cubo["parcial"] = False
    return {"cubos": list(cubos.values()), "agregacion": agregacion}


def series_por_nivel(
    db: Database,
    version: str,
    *,
    filtros: dict,
    agregacion: str,
    fechas: dict[int, date],
) -> dict:
    """Series del nivel activo, con el mismo dominio temporal en todas.

    Tienda: una serie por tienda seleccionada.
    Departamento: una serie por departamento permitido de la tienda en contexto.
    Producto: una serie por producto seleccionado (máximo ``MAX_PRODUCTOS``).
    """
    ventana: Ventana = filtros["ventana"]
    dias = _dias_de(ventana, fechas)
    fechas_ordenadas = [fechas[d] for d in dias]
    nivel = filtros.get("nivel", NIVEL_POR_OMISION)
    vectores = _vectores(db, version, filtros, dias)

    series = []
    for clave in _claves_del_nivel(filtros):
        valores = vectores.get(clave) or [None] * len(dias)
        agrupada = _agrupar(fechas_ordenadas, valores, agregacion)
        series.append(
            {
                "clave": clave,
                "nombre": clave,
                "nivel": nivel,
                "store_id": filtros.get("tienda") or clave,
                "total": sum(valor for valor in valores if valor is not None),
                "dias_con_dato": sum(1 for valor in valores if valor is not None),
                "valores": agrupada["cubos"],
            }
        )
    return {
        "fechas": [fecha.isoformat() for fecha in fechas_ordenadas],
        "series": series,
        "dominio": {
            "desde": ventana.desde.isoformat(),
            "hasta": ventana.hasta.isoformat(),
            "dias": len(dias),
        },
        "agregacion": agregacion,
    }


def series_por_tienda(
    db: Database,
    version: str,
    *,
    filtros: dict,
    agregacion: str,
    fechas: dict[int, date],
) -> dict:
    """Compatibilidad: series del nivel de tiendas (el nivel por omisión)."""
    return series_por_nivel(db, version, filtros=filtros, agregacion=agregacion, fechas=fechas)


def _claves_del_nivel(filtros: dict) -> list[str]:
    """Claves de las series que se representan en el nivel activo, en orden."""
    nivel = filtros.get("nivel", NIVEL_POR_OMISION)
    if nivel == "departamento":
        return list(filtros.get("departamentos_panel") or [])
    if nivel == "producto":
        return list(filtros.get("productos") or [])
    return list(filtros["tiendas"])


def _vectores(
    db: Database, version: str, filtros: dict, dias: list[int]
) -> dict[str, list[int | None]]:
    """Vector diario por clave del nivel activo, alineado a ``dias``.

    Departamento y producto leen de la fuente que les corresponde: el derivado
    ``departamento_tienda`` (publicado) y los bloques producto × tienda. Nunca se
    combinan marginales para inventar la intersección.
    """
    nivel = filtros.get("nivel", NIVEL_POR_OMISION)
    if nivel == "departamento":
        tiendas = _tiendas_contexto(filtros)
        departamentos = list(filtros.get("departamentos_panel") or [])
        if not tiendas or not departamentos:
            return {}
        # La intersección tienda × departamento es el derivado; para un contexto de
        # varias tiendas se suma su vector día a día (nunca se combinan marginales).
        sumado: dict[str, list[int | None]] = {d: [0] * len(dias) for d in departamentos}
        for tienda in tiendas:
            diarios = derivados.series(db, version, tienda=tienda, departamentos=departamentos)
            for departamento, valores in diarios.items():
                vector = _alinear(valores, dias, 1)
                destino = sumado[departamento]
                for indice, valor in enumerate(vector):
                    if valor is None:
                        continue
                    destino[indice] = (destino[indice] or 0) + valor
        return sumado
    if nivel == "producto":
        tiendas = _tiendas_contexto(filtros)
        if not tiendas:
            return {}
        return {
            producto: _vector_bloques_contexto(
                db, version, tiendas=tiendas, item=producto, dias=dias
            )
            for producto in (filtros.get("productos") or [])
        }
    return _vectores_tiendas(db, version, filtros, dias)


def _tiendas_contexto(filtros: dict) -> list[str]:
    """Tiendas del contexto territorial efectivo (una tienda o las de un estado)."""
    return list(filtros.get("tiendas_contexto") or filtros.get("tiendas") or [])


def _alinear(valores: list[int | None], dias: list[int], d_inicio: int) -> list[int | None]:
    """Recorta/rellena un vector con ``d_inicio`` a la lista de días pedida."""
    return [
        valores[dia - d_inicio] if 0 <= dia - d_inicio < len(valores) else None for dia in dias
    ]


def _vector_bloques(
    db: Database, version: str, *, tienda: str, item: str, dias: list[int]
) -> list[int | None]:
    """Serie diaria de un producto en una tienda, desde los bloques locales.

    Un bloque guarda 196 días contiguos; los días sin bloque quedan en ``None``.
    """
    return _vector_bloques_contexto(db, version, tiendas=[tienda], item=item, dias=dias)


def _vector_bloques_contexto(
    db: Database, version: str, *, tiendas: list[str], item: str, dias: list[int]
) -> list[int | None]:
    """Serie diaria de un producto sumada sobre el contexto territorial."""
    rango: dict[int, int | None] = {dia: None for dia in dias}
    for documento in db[M5_BLOQUES].find(
        {"version": version, "store_id": {"$in": list(tiendas)}, "item_id": item},
        {"d_inicio": 1, "unidades": 1},
    ):
        inicio = int(documento["d_inicio"])
        for desplazamiento, valor in enumerate(documento["unidades"]):
            dia = inicio + desplazamiento
            if dia in rango:
                rango[dia] = (rango[dia] or 0) + valor
    return [rango[dia] for dia in dias]


def _vectores_tiendas(
    db: Database, version: str, filtros: dict, dias: list[int]
) -> dict[str, list[int | None]]:
    """Vector diario por tienda (agregados, o bloques si hay filtro de producto)."""
    tiendas = filtros["tiendas"]
    acumulado: dict[str, list[int | None]] = {}
    if filtros["productos"] or filtros["departamentos"]:
        acumulado = _series_desde_bloques(db, version, filtros, dias)
    else:
        documentos = {
            documento["clave"]: documento
            for documento in db[M5_AGREGADOS].find(
                {"version": version, "dimension": "tienda", "clave": {"$in": tiendas}},
                {"clave": 1, "d_inicio": 1, "unidades": 1},
            )
        }
        for tienda in tiendas:
            documento = documentos.get(tienda)
            if not documento:
                acumulado[tienda] = [None] * len(dias)
                continue
            acumulado[tienda] = _alinear(documento["unidades"], dias, int(documento["d_inicio"]))

    # Filtro por categoría sobre los agregados categoría × tienda.
    if filtros["categorias"] and set(filtros["categorias"]) != set(_todas_las_categorias(db, version)):
        por_categoria = _series_categoria_tienda(db, version, filtros, dias)
        base = {tienda: [0] * len(dias) for tienda in tiendas}
        for tienda, valores in por_categoria.items():
            if tienda not in base:
                continue
            for indice, valor in enumerate(valores):
                if valor is None:
                    continue
                base[tienda][indice] = (base[tienda][indice] or 0) + valor
        acumulado = base
    return acumulado


def _series_categoria_tienda(db: Database, version: str, filtros: dict, dias: list[int]) -> dict:
    """Suma por tienda de las categorías seleccionadas."""
    claves = [f"{cat}|{store}" for cat in filtros["categorias"] for store in filtros["tiendas"]]
    por_tienda: dict[str, list[int | None]] = defaultdict(lambda: [0] * len(dias))
    for documento in db[M5_AGREGADOS].find(
        {"version": version, "dimension": "categoria_tienda", "clave": {"$in": claves}},
        {"clave": 1, "d_inicio": 1, "unidades": 1},
    ):
        cat, tienda = documento["clave"].split("|", 1)
        del cat
        destino = por_tienda[tienda]
        unidades = documento["unidades"]
        inicio = documento["d_inicio"]
        for indice, dia in enumerate(dias):
            posicion = dia - inicio
            if 0 <= posicion < len(unidades):
                destino[indice] = (destino[indice] or 0) + unidades[posicion]
    return dict(por_tienda)


def _series_desde_bloques(db: Database, version: str, filtros: dict, dias: list[int]) -> dict:
    """Series por tienda sumando producto × tienda cuando se filtra por producto."""
    productos = _productos_del_filtro(db, version, filtros)
    if not productos:
        return {tienda: [0] * len(dias) for tienda in filtros["tiendas"]}
    por_tienda: dict[str, list[int | None]] = {tienda: [0] * len(dias) for tienda in filtros["tiendas"]}
    consulta = {
        "version": version,
        "item_id": {"$in": productos},
        "store_id": {"$in": filtros["tiendas"]},
    }
    for documento in db[M5_BLOQUES].find(consulta, {"item_id": 0, "store_id": 1, "d_inicio": 1, "unidades": 1}):
        tienda = documento["store_id"]
        destino = por_tienda.get(tienda)
        if destino is None:
            continue
        inicio = documento["d_inicio"]
        unidades = documento["unidades"]
        for indice, dia in enumerate(dias):
            posicion = dia - inicio
            if 0 <= posicion < len(unidades):
                destino[indice] = (destino[indice] or 0) + unidades[posicion]
    return por_tienda


def _productos_del_filtro(db: Database, version: str, filtros: dict) -> list[str]:
    """Productos seleccionados, acotados por categoría y departamento."""
    if filtros["productos"]:
        base = list(filtros["productos"])
    else:
        base = [
            documento["item_id"]
            for documento in db[M5_CATALOGO].find(
                {"version": version, "tipo": "producto"}, {"item_id": 1, "_id": 0}
            )
        ]
    if not filtros["departamentos"] and set(filtros["categorias"]) == set(_todas_las_categorias(db, version)):
        return base
    consulta = {"version": version, "tipo": "producto", "item_id": {"$in": base}}
    if filtros["categorias"]:
        consulta["cat_id"] = {"$in": filtros["categorias"]}
    if filtros["departamentos"]:
        consulta["dept_id"] = {"$in": filtros["departamentos"]}
    return [documento["item_id"] for documento in db[M5_CATALOGO].find(consulta, {"item_id": 1, "_id": 0})]


def _todas_las_categorias(db: Database, version: str) -> list[str]:
    return sorted({d["cat_id"] for d in db[M5_CATALOGO].find({"version": version, "tipo": "categoria"},
                                                             {"cat_id": 1, "_id": 0})})


def _dias_de(ventana: Ventana, fechas: dict[int, date]) -> list[int]:
    """Días ``d_N`` de un intervalo inclusivo, en orden."""
    por_fecha = {fecha: dia for dia, fecha in fechas.items()}
    salida: list[int] = []
    cursor = ventana.desde
    while cursor <= ventana.hasta:
        dia = por_fecha.get(cursor)
        if dia is not None:
            salida.append(dia)
        cursor += timedelta(days=1)
    return salida


# --------------------------------------------------------------------------- #
# Comparación A/B y concentración
# --------------------------------------------------------------------------- #


def comparacion_ab(db: Database, version: str, *, filtros: dict, fechas: dict[int, date]) -> dict:
    """Totales en A y B por la clave del nivel activo, con diferencia y variación.

    - Nivel tienda: una fila por categoría (es la lectura de siempre).
    - Nivel departamento: una fila por departamento representado.
    - Nivel producto: una fila por producto seleccionado.

    Siempre con los mismos filtros no temporales en A y B.
    """
    a: Ventana | None = filtros["a"]
    b: Ventana | None = filtros["b"]
    nivel = filtros.get("nivel", NIVEL_POR_OMISION)
    dias_a = _dias_de(a, fechas) if a else []
    dias_b = _dias_de(b, fechas) if b else []

    if nivel == "tienda":
        claves = list(filtros["categorias"])
    else:
        claves = _claves_del_nivel(filtros)

    totales: dict[str, tuple[int, int]] = {}
    if nivel == "tienda":
        for clave in claves:
            totales[clave] = (
                sum(_suma_categoria(db, version, clave, tienda, dias_a) for tienda in filtros["tiendas"]),
                sum(_suma_categoria(db, version, clave, tienda, dias_b) for tienda in filtros["tiendas"]),
            )
    else:
        conjunto_a = set(dias_a)
        conjunto_b = set(dias_b)
        dias_union = sorted(conjunto_a | conjunto_b)
        vectores = _vectores(db, version, filtros, dias_union)
        for clave in claves:
            valores = vectores.get(clave) or []
            total_a = 0
            total_b = 0
            for dia, valor in zip(dias_union, valores, strict=False):
                if valor is None:
                    continue
                if dia in conjunto_a:
                    total_a += valor
                if dia in conjunto_b:
                    total_b += valor
            totales[clave] = (total_a, total_b)

    filas = []
    for clave in claves:
        total_a, total_b = totales.get(clave, (0, 0))
        diferencia = total_b - total_a
        variacion = round(100 * diferencia / total_a, 2) if total_a > 0 else None
        filas.append(
            {
                "clave": clave,
                "etiqueta": clave,
                "nivel": nivel,
                "categoria": clave if nivel == "tienda" else None,
                "a": total_a,
                "b": total_b,
                "diferencia": diferencia,
                "variacion_pct": variacion,
                "nota": None if total_a > 0 else "Variación no calculable: A = 0.",
            }
        )
    return {
        "nivel": nivel,
        "a": {"desde": a.desde.isoformat(), "hasta": a.hasta.isoformat(), "dias": a.dias} if a else None,
        "b": {"desde": b.desde.isoformat(), "hasta": b.hasta.isoformat(), "dias": b.dias} if b else None,
        "filas": filas,
        "no_causal": (
            "La diferencia es aritmética entre dos intervalos con los mismos filtros; "
            "no mide el efecto de ninguna intervención."
        ),
    }


def _suma_categoria(db: Database, version: str, categoria: str, tienda: str, dias: list[int]) -> int:
    documento = db[M5_AGREGADOS].find_one(
        {"version": version, "dimension": "categoria_tienda", "clave": f"{categoria}|{tienda}"},
        {"d_inicio": 1, "unidades": 1},
    )
    if not documento:
        return 0
    inicio = documento["d_inicio"]
    unidades = documento["unidades"]
    total = 0
    for dia in dias:
        posicion = dia - inicio
        if 0 <= posicion < len(unidades):
            total += unidades[posicion]
    return total


def _totales_por_producto(
    db: Database, version: str, *, productos: list[str], tiendas: list[str], desde: date, hasta: date
) -> dict[str, int]:
    """Suma unidades por producto en el intervalo, dentro de MongoDB.

    Cada bloque guarda 196 días; el servidor recorta el tramo con ``$slice`` y suma.
    Antes esto se hacía en Python documento a documento (≈92 s con 304 900 bloques);
    la agregación tarda segundos y mantiene el mismo resultado.

    Los bloques se acotan por solapamiento con el intervalo (``d_fin >= desde`` y
    ``d_inicio <= hasta``): sin ese filtro, un bloque posterior al intervalo producía
    un ``$slice`` de longitud 0, que MongoDB rechaza — es decir, cualquier consulta
    de un periodo que terminara antes del último bloque fallaba.
    """
    consulta: dict = {"version": version, "store_id": {"$in": tiendas}}
    if productos:
        consulta["item_id"] = {"$in": productos}
    fecha_a_d: dict[date, int] = {fecha: dia for dia, fecha in _fechas_version(db, version).items()}
    d_desde = fecha_a_d.get(desde)
    d_hasta = fecha_a_d.get(hasta)
    if d_desde is None or d_hasta is None:
        return {}
    consulta["d_inicio"] = {"$lte": d_hasta}
    consulta["d_fin"] = {"$gte": d_desde}
    pipeline = [
        {"$match": consulta},
        {
            "$project": {
                "item_id": 1,
                "tramo": {
                    "$let": {
                        "vars": {"inicio": {"$max": [0, {"$subtract": [d_desde, "$d_inicio"]}]}},
                        "in": {
                            "$slice": [
                                "$unidades",
                                "$$inicio",
                                {
                                    "$max": [
                                        0,
                                        {
                                            "$min": [
                                                {"$subtract": [{"$size": "$unidades"}, "$$inicio"]},
                                                {"$add": [{"$subtract": [d_hasta, "$d_inicio"]}, 1]},
                                            ]
                                        },
                                    ]
                                },
                            ]
                        },
                    }
                },
            }
        },
        {"$project": {"item_id": 1, "suma": {"$sum": "$tramo"}}},
        {"$group": {"_id": "$item_id", "unidades": {"$sum": "$suma"}}},
        {"$match": {"unidades": {"$gt": 0}}},
        {"$sort": {"unidades": -1, "_id": 1}},
    ]
    return {
        documento["_id"]: int(documento["unidades"])
        for documento in db[M5_BLOQUES].aggregate(pipeline, allowDiskUse=True)
    }


def _fechas_version(db: Database, version: str) -> dict[int, date]:
    """``d_N`` → fecha usando el calendario ya cargado (memoizado por proceso)."""
    global _FECHAS_CACHE
    cache = _FECHAS_CACHE.get(version)
    if cache is not None:
        return cache
    mapa = mapa_fechas(db)
    _FECHAS_CACHE[version] = mapa
    return mapa


_FECHAS_CACHE: dict[str, dict[int, date]] = {}


def _alcance_concentracion(filtros: dict) -> str:
    """Texto explícito del universo de la curva, según el nivel de exploración."""
    nivel = filtros.get("nivel", NIVEL_POR_OMISION)
    if nivel == "departamento":
        departamentos = ", ".join(filtros.get("departamentos_panel") or [])
        return f"productos con ventas positivas de {filtros.get('tienda')} en {departamentos}"
    if nivel == "producto":
        return "productos seleccionados (máximo %d)" % MAX_PRODUCTOS
    return "productos con ventas positivas en el intervalo"


def concentracion(db: Database, version: str, *, filtros: dict, fechas: dict[int, date],
                  limite: int = 100) -> dict:
    """Curva acumulada descendente por producto (no es la curva de Lorenz).

    Universo: productos con ventas positivas en el intervalo, dentro del alcance
    del nivel activo (tiendas seleccionadas, departamentos de la tienda en
    contexto o los productos seleccionados). Punto inicial (0, 0) y cierre
    (100, 100) si el total es positivo. Empates por identificador para que el
    orden sea estable.
    """
    ventana: Ventana = filtros["ventana"]
    alcance = _alcance_concentracion(filtros)
    productos = _productos_del_filtro(db, version, filtros)
    totales = _totales_por_producto(
        db, version, productos=productos, tiendas=filtros["tiendas"], desde=ventana.desde, hasta=ventana.hasta
    )
    ordenado = sorted(totales.items(), key=lambda par: (-par[1], par[0]))
    total = sum(valor for _, valor in ordenado)
    if total <= 0:
        return {
            "universo": alcance,
            "alcance": alcance,
            "productos": 0,
            "total": 0,
            "puntos": [],
            "estado": "vacio",
            "nota": "Total cero en el intervalo: no hay curva que calcular.",
        }
    acumulado = 0
    n = len(ordenado)
    puntos = [
        {
            "producto": None,
            "posicion": 0,
            "unidades": 0,
            "acumulado": 0,
            "participacion": 0.0,
            "porcentaje": 0.0,
            "porcentaje_productos": 0.0,
        }
    ]
    for posicion, (producto, unidades) in enumerate(ordenado, start=1):
        acumulado += unidades
        puntos.append(
            {
                "producto": producto,
                "posicion": posicion,
                "unidades": unidades,
                "acumulado": acumulado,
                "participacion": round(100 * unidades / total, 4),
                "porcentaje": round(100 * acumulado / total, 4),
                "porcentaje_productos": round(100 * posicion / n, 4),
            }
        )

    # Lectura del 80 %: se calcula sobre los productos REALES, no sobre la muestra
    # que se envía al gráfico. Es el menor número entero de productos cuya suma
    # acumulada alcanza o supera el 80 % de las unidades del universo.
    umbral = 0.8 * total
    acumulado_80 = 0
    productos_80 = 0
    for punto in puntos[1:]:
        acumulado_80 += punto["unidades"] or 0
        productos_80 += 1
        if acumulado_80 >= umbral:
            break
    lectura_80 = {
        "productos": productos_80,
        "universo": n,
        "porcentaje_productos": round(100 * productos_80 / n, 4),
        "unidades": acumulado_80,
        "porcentaje_unidades": round(100 * acumulado_80 / total, 4),
        "umbral_unidades": round(umbral, 2),
        "texto": (
            f"El {round(100 * productos_80 / n, 1):g} % de los productos "
            f"concentra al menos el 80 % de las unidades"
        ),
    }

    # Representación exacta compacta: una fila por producto (identificador, unidades,
    # categoría y departamento), ordenada de mayor a menor con desempate por identificador.
    # Permite calcular en el navegador el mínimo número de productos para CUALQUIER
    # objetivo (1–100 %) sin interpolar y sin enviar series diarias.
    catalogo = {
        documento["item_id"]: (documento.get("cat_id"), documento.get("dept_id"))
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "producto", "item_id": {"$in": [p for p, _ in ordenado]}},
            {"item_id": 1, "cat_id": 1, "dept_id": 1, "_id": 0},
        )
    }
    exacto = {
        "productos": [
            [
                producto,
                int(unidades),
                catalogo.get(producto, (None, None))[0],
                catalogo.get(producto, (None, None))[1],
            ]
            for producto, unidades in ordenado
        ],
        "total": total,
        "n": n,
        "orden": "unidades descendente, desempate por item_id ascendente",
        "lectura_80": lectura_80,
    }

    # Muestra uniforme sobre el porcentaje de productos: así la polilínea representa
    # la forma de la curva y no un puñado de productos gigantes contra el resto.
    if n > limite:
        indices = sorted({round(i * (len(puntos) - 1) / limite) for i in range(limite)} | {len(puntos) - 1})
        visibles = [puntos[i] for i in indices]
    else:
        visibles = puntos
    cola = max(0, n - len(visibles) + 1)
    if visibles:
        visibles[-1] = {
            **visibles[-1],
            "cierre": True,
            "porcentaje": 100.0,
            "porcentaje_productos": 100.0,
        }
    return {
        "universo": alcance,
        "alcance": alcance,
        "productos": len(ordenado),
        "total": total,
        "puntos": visibles,
        "cola": cola,
        "lectura_80": lectura_80,
        "exacto": exacto,
        "cierre": {"porcentaje_productos": 100.0, "porcentaje_unidades": 100.0},
        "estado": "unico" if n == 1 else "ok",
        "nota": (
            "Curva acumulada descendente por producto; no es la curva de Lorenz "
            "convencional. El universo excluye productos sin ventas en el intervalo."
            + (
                " Solo hay un producto en el alcance: la curva se reduce a ese punto."
                if n == 1
                else ""
            )
        ),
    }


#: Códigos de M5: el departamento es ``FOODS_1`` y la tienda ``CA_1`` (dos letras de estado).
_DEPARTAMENTO = re.compile(r"^[A-Z]+_\d+$")
_TIENDA = re.compile(r"^[A-Z]{2}_\d+$")


def clave_agregado(dimension: str, clave: str) -> str:
    """Traduce la clave de una predicción a la clave del agregado observado.

    Las dos colecciones no usan el mismo identificador: el total se llama ``total`` en la
    predicción y ``todas`` en el agregado; los departamentos se guardan como
    ``FOODS|FOODS_1`` y ``CA_1|FOODS_1``. Sin esta traducción la historia observada de esos
    niveles llega vacía y la gráfica de evaluación se queda sin el tramo previo al corte.
    """
    if dimension == "total" and clave == "total":
        return "todas"
    if dimension == "departamento":
        if "|" in clave:
            return clave
        return f"{clave.split('_')[0]}|{clave}"
    if dimension == "departamento_tienda" and "|" in clave:
        primero, segundo = clave.split("|", 1)
        if _DEPARTAMENTO.match(primero) and _TIENDA.match(segundo):
            return f"{segundo}|{primero}"
    return clave


def historico_antes_de(
    db: Database, version: str, *, nivel: str, claves: list[str], origen_d: int, dias: int
) -> dict[str, list[int | None]]:
    """Serie observada de los ``dias`` anteriores a ``origen_d`` (inclusive).

    Se usa para dibujar la historia junto al pronóstico. Los días sin observación
    vuelven como ``None``. El resultado se indexa con la clave que pidió el llamador, no con
    la clave interna del agregado.
    """
    desde = max(1, origen_d - dias + 1)
    pedidas = {clave_agregado(nivel, clave): clave for clave in claves}
    consulta = {"version": version, "dimension": nivel, "clave": {"$in": list(pedidas)}} if claves else {
        "version": version,
        "dimension": nivel,
    }
    salida: dict[str, list[int | None]] = {}
    for documento in db[M5_AGREGADOS].find(consulta, {"clave": 1, "d_inicio": 1, "unidades": 1}):
        inicio = documento["d_inicio"]
        unidades = documento["unidades"]
        serie: list[int | None] = []
        for dia in range(desde, origen_d + 1):
            posicion = dia - inicio
            serie.append(unidades[posicion] if 0 <= posicion < len(unidades) else None)
        salida[pedidas.get(documento["clave"], documento["clave"])] = serie
    return salida


def dias_de_historico(origen_d: int, dias: int) -> list[int]:
    """Días ``d`` del tramo observado que acompaña al pronóstico."""
    return list(range(max(1, origen_d - dias + 1), origen_d + 1))


def resumen(
    *,
    series: dict,
    filtros: dict,
    concentracion_datos: dict,
    total_contexto: int | None = None,
    total_seleccion: int | None = None,
) -> dict:
    """Resumen del intervalo, reconciliado con series y concentración.

    ``total_contexto`` es el total del ámbito que se está explorando (el
    departamento, al explorar productos) y ``total_seleccion`` el de las series
    realmente representadas. Solo difieren en el nivel de producto, donde la
    selección es parcial por diseño.
    """
    nivel = filtros.get("nivel", NIVEL_POR_OMISION)
    total = sum(serie["total"] for serie in series["series"])
    dias = series["dominio"]["dias"]
    dias_con_dato = sum(serie["dias_con_dato"] for serie in series["series"])
    contexto = total if total_contexto is None else total_contexto
    seleccion = total if total_seleccion is None else total_seleccion
    return {
        "total": total,
        "nivel": nivel,
        "series": len(series["series"]),
        "tiendas": len(filtros["tiendas"]),
        "total_contexto": contexto,
        "total_seleccion": seleccion,
        "participacion_pct": round(100 * seleccion / contexto, 2) if contexto > 0 else None,
        "dias": dias,
        "promedio_diario": round(total / dias, 2) if dias else None,
        "dias_con_dato_por_serie": dias_con_dato // max(1, len(series["series"])),
        "concentracion_total": concentracion_datos.get("total", 0),
        "reconcilia_con_concentracion": concentracion_datos.get("total", 0) == total,
        "unidad": "unidades vendidas",
        "desde": filtros["ventana"].desde.isoformat(),
        "hasta": filtros["ventana"].hasta.isoformat(),
    }


def total_departamento(
    db: Database,
    version: str,
    *,
    tiendas: list[str],
    departamento: str,
    ventana: Ventana,
    fechas: dict[int, date],
) -> int:
    """Unidades del departamento completo en el contexto territorial y el intervalo.

    Es la referencia rotulada del nivel de producto: se calcula con la misma fuente
    que los paneles de departamento (el derivado), no sumando la selección.
    """
    dias = _dias_de(ventana, fechas)
    vectores = _vectores(
        db,
        version,
        {
            "nivel": "departamento",
            "tiendas_contexto": list(tiendas),
            "departamentos_panel": [departamento],
        },
        dias,
    )
    return sum(
        valor for valores in vectores.values() for valor in valores if valor is not None
    )


# --------------------------------------------------------------------------- #
# Territorio, composición comercial y comparación histórica
# --------------------------------------------------------------------------- #


def _vector_contexto(
    db: Database,
    version: str,
    *,
    tiendas: list[str],
    categorias: list[str],
    departamentos: list[str],
    dias: list[int],
) -> list[int | None]:
    """Unidades diarias de un contexto (territorio × catálogo) en una sola serie.

    Se suman los agregados que **contienen** la intersección pedida:

    - con departamentos: el derivado ``departamento_tienda`` (tienda × departamento);
    - con categorías: ``categoria_tienda``;
    - sin filtro comercial: ``tienda``.

    Nunca se combinan marginales sueltos: cada nivel suma documentos que ya están
    desagregados por tienda.
    """
    todas = set(_todas_las_categorias(db, version))
    salida: list[int | None] = [0] * len(dias)
    documentos: list[dict] = []

    if departamentos:
        claves = [f"{tienda}|{departamento}" for tienda in tiendas for departamento in departamentos]
        documentos = list(
            db[M5_AGREGADOS].find(
                {"version": version, "dimension": derivados.NOMBRE_DERIVADO, "clave": {"$in": claves}},
                {"d_inicio": 1, "unidades": 1},
            )
        )
    elif categorias and set(categorias) != todas:
        claves = [f"{categoria}|{tienda}" for categoria in categorias for tienda in tiendas]
        documentos = list(
            db[M5_AGREGADOS].find(
                {"version": version, "dimension": "categoria_tienda", "clave": {"$in": claves}},
                {"d_inicio": 1, "unidades": 1},
            )
        )
    else:
        documentos = list(
            db[M5_AGREGADOS].find(
                {"version": version, "dimension": "tienda", "clave": {"$in": list(tiendas)}},
                {"d_inicio": 1, "unidades": 1},
            )
        )

    for documento in documentos:
        inicio = int(documento["d_inicio"])
        for indice, dia in enumerate(dias):
            posicion = dia - inicio
            if 0 <= posicion < len(documento["unidades"]):
                salida[indice] = (salida[indice] or 0) + documento["unidades"][posicion]
    return salida


def panorama(db: Database, version: str, *, filtros: dict, fechas: dict[int, date]) -> dict:
    """Panorama territorial: cada estado con sus tiendas, unidades y participación.

    El denominador de la participación es el **total del contexto territorial**
    (todas las tiendas seleccionadas) con los mismos filtros comerciales y periodo.
    """
    cal = catalogo(db, version)
    dias = _dias_de(filtros["ventana"], fechas)
    tiendas = list(filtros["tiendas"])
    por_estado: dict[str, list[str]] = {}
    estado_de = {t["store_id"]: t["state_id"] for t in cal["tiendas"]}
    for tienda in tiendas:
        por_estado.setdefault(estado_de.get(tienda, "?"), []).append(tienda)

    def _unidades(lista: list[str]) -> int:
        vector = _vector_contexto(
            db,
            version,
            tiendas=lista,
            categorias=list(filtros["categorias"]),
            departamentos=list(filtros["departamentos"]),
            dias=dias,
        )
        return sum(valor for valor in vector if valor is not None)

    total = _unidades(tiendas)
    estados = []
    for estado in cal["estados"]:
        if estado not in por_estado:
            continue
        unidades = _unidades(por_estado[estado])
        estados.append(
            {
                "estado": estado,
                "nombre": ESTADOS_NOMBRE.get(estado, estado),
                "tiendas": sorted(por_estado[estado]),
                "tiendas_incluidas": len(por_estado[estado]),
                "unidades": unidades,
                "participacion_pct": round(100 * unidades / total, 2) if total > 0 else None,
            }
        )
    return {
        "total_unidades": total,
        "denominador": (
            "Total del contexto territorial: todas las tiendas incluidas en los filtros "
            "vigentes, con el periodo y los filtros comerciales aplicados."
        ),
        "estados": estados,
        "estados_incluidos": len(estados),
        "estados_catalogo": len(cal["estados"]),
        "unidad": "unidades vendidas",
    }


def composicion(db: Database, version: str, *, filtros: dict, fechas: dict[int, date]) -> dict:
    """Composición comercial del territorio: categorías y sus departamentos.

    Es el **selector** del catálogo, así que se calcula sobre todo el catálogo del
    contexto territorial y del periodo (no sobre la categoría ya elegida). Los
    denominadores son explícitos y distintos:

    - categoría: participación en el total del contexto territorial (todas las
      categorías);
    - departamento: participación dentro de **su** categoría.
    """
    cal = catalogo(db, version)
    dias = _dias_de(filtros["ventana"], fechas)
    tiendas = list(filtros["tiendas"])

    def _unidades(categorias: list[str], departamentos: list[str]) -> int:
        vector = _vector_contexto(
            db,
            version,
            tiendas=tiendas,
            categorias=categorias,
            departamentos=departamentos,
            dias=dias,
        )
        return sum(valor for valor in vector if valor is not None)

    categorias = list(cal["categorias"])
    unidades_por_categoria = {
        categoria: _unidades([categoria], []) for categoria in categorias
    }
    total = sum(unidades_por_categoria.values())

    departamentos_por_categoria: dict[str, list[str]] = defaultdict(list)
    for departamento in cal["departamentos"]:
        departamentos_por_categoria[departamento["cat_id"]].append(departamento["dept_id"])

    filas_categorias = [
        {
            "codigo": categoria,
            "etiqueta": CATEGORIAS_NOMBRE.get(categoria, categoria),
            "unidades": unidades_por_categoria[categoria],
            "participacion_pct": (
                round(100 * unidades_por_categoria[categoria] / total, 2) if total > 0 else None
            ),
            "denominador": "total del contexto territorial",
        }
        for categoria in categorias
    ]

    filas_departamentos = []
    for categoria in categorias:
        base = unidades_por_categoria[categoria]
        for departamento in sorted(departamentos_por_categoria.get(categoria, [])):
            unidades = _unidades([], [departamento])
            filas_departamentos.append(
                {
                    "codigo": departamento,
                    "categoria": categoria,
                    "etiqueta": departamento,
                    "unidades": unidades,
                    "participacion_pct": round(100 * unidades / base, 2) if base > 0 else None,
                    "denominador": f"total de {CATEGORIAS_NOMBRE.get(categoria, categoria)} en el contexto",
                }
            )

    return {
        "total_unidades": total,
        "unidad": "unidades vendidas",
        "categorias": filas_categorias,
        "departamentos": filas_departamentos,
        "denominador_categorias": "total del contexto territorial (todas las categorías)",
        "denominador_departamentos": "total de su categoría en el contexto",
        "nota": (
            "Composición de todo el catálogo del contexto: es el selector, así que no se "
            "reduce a la categoría o departamento ya elegidos. Los porcentajes de categoría "
            "y de departamento usan denominadores distintos y se rotulan por separado."
        ),
    }


# --------------------------------------------------------------------------- #
# Comparación histórica (año analizado contra años anteriores)
# --------------------------------------------------------------------------- #

MODOS_HISTORICO = ("semana", "acumulado")

REGLA_SEMANA = (
    "Semanas ISO completas de lunes a domingo, comparadas por número de semana ISO. "
    "Las semanas del año analizado que quedan cortadas por la cobertura no se comparan; "
    "las semanas de referencia incompletas se excluyen de ese punto."
)
REGLA_ACUMULADO = (
    "Acumulado por mes y día: cada año suma sus ventas reales desde el 1 de enero hasta "
    "esa fecha (o desde el inicio disponible del conjunto, si es posterior). Primero se "
    "acumula cada año y después se promedian los acumulados; nunca se acumulan mínimos ni "
    "máximos diarios."
)

NOTA_BANDA = (
    "Rango histórico observado: mínimo y máximo de los años de referencia incluidos. No es "
    "un intervalo de confianza, no es una predicción y no procede de ningún modelo."
)


class HistoricoInvalido(FiltroInvalido):
    """Año o modo no representables con la versión publicada."""


def _anio_completo(anio: int, desde: date, hasta: date) -> bool:
    """Cobertura completa del 1 de enero al 31 de diciembre."""
    return desde <= date(anio, 1, 1) and hasta >= date(anio, 12, 31)


def _sumar_fechas(unidades: dict[date, int], fechas: list[date]) -> int | None:
    """Suma de un conjunto de fechas; ``None`` si no hay ningún dato."""
    valores = [unidades[fecha] for fecha in fechas if fecha in unidades]
    return sum(valores) if valores else None


def _acumulado(unidades: dict[date, int], desde: date, hasta: date) -> dict[date, int]:
    """Acumulado diario desde ``desde`` hasta ``hasta`` (suma corriente)."""
    salida: dict[date, int] = {}
    total = 0
    cursor = desde
    while cursor <= hasta:
        total += unidades.get(cursor, 0)
        salida[cursor] = total
        cursor += timedelta(days=1)
    return salida


def _puntos_semana(
    *, anio: int, unidades: dict[date, int], referencias: list[int], desde: date, hasta: date
) -> list[dict]:
    """Un punto por semana ISO del año analizado."""
    puntos: list[dict] = []
    for semana in range(1, 54):
        try:
            lunes = date.fromisocalendar(anio, semana, 1)
        except ValueError:
            continue
        domingo = lunes + timedelta(days=6)
        fechas = [lunes + timedelta(days=desplazamiento) for desplazamiento in range(7)]
        en_cobertura = [fecha for fecha in fechas if desde <= fecha <= hasta]
        parcial = len(en_cobertura) < 7
        observado = _sumar_fechas(unidades, fechas) if en_cobertura else None

        valores: list[int] = []
        for referencia in referencias:
            try:
                lunes_ref = date.fromisocalendar(referencia, semana, 1)
            except ValueError:
                continue
            fechas_ref = [lunes_ref + timedelta(days=desplazamiento) for desplazamiento in range(7)]
            if not all(desde <= fecha <= hasta for fecha in fechas_ref):
                continue
            valor = _sumar_fechas(unidades, fechas_ref)
            if valor is not None:
                valores.append(valor)

        motivo = None
        if parcial:
            motivo = "Semana parcial del año analizado: no se compara con semanas completas."
            valores = []
        elif observado is None:
            motivo = "Sin observación en el año analizado."

        promedio = round(sum(valores) / len(valores), 2) if valores else None
        punto = {
            "clave": f"{anio}-S{semana:02d}",
            "etiqueta": f"Sem {semana}",
            "semana": semana,
            "mes_dia": None,
            "fecha_desde": lunes.isoformat(),
            "fecha_hasta": domingo.isoformat(),
            "observado": observado,
            "referencia_promedio": promedio,
            "referencia_min": min(valores) if valores else None,
            "referencia_max": max(valores) if valores else None,
            "referencias": len(valores),
            "parcial": parcial,
            "estado": (
                "ausente" if observado is None else ("sin_referencia" if promedio is None else "ok")
            ),
            "motivo": motivo,
        }
        puntos.append(_con_diferencias(punto))
    return puntos


def _puntos_acumulado(
    *,
    anio: int,
    unidades: dict[date, int],
    referencias: list[int],
    desde: date,
    hasta: date,
) -> tuple[list[dict], dict]:
    """Un punto por mes y día del año analizado, con acumulados comparables."""
    inicio = max(date(anio, 1, 1), desde)
    fin_datos = min(date(anio, 12, 31), hasta)
    acumulados_observado: dict[date, int] = {}
    if inicio <= fin_datos:
        acumulados_observado = _acumulado(unidades, inicio, fin_datos)

    acumulados_referencia = {
        referencia: _acumulado(unidades, date(referencia, 1, 1), date(referencia, 12, 31))
        for referencia in referencias
    }

    puntos: list[dict] = []
    cursor = inicio
    while cursor <= date(anio, 12, 31):
        observado = acumulados_observado.get(cursor)
        valores: list[int] = []
        for referencia in referencias:
            try:
                fecha_ref = date(referencia, cursor.month, cursor.day)
            except ValueError:
                continue  # 29 de febrero en un año no bisiesto: esa referencia no aplica
            valor = acumulados_referencia[referencia].get(fecha_ref)
            if valor is not None:
                valores.append(valor)
        promedio = round(sum(valores) / len(valores), 2) if valores else None
        motivo = None
        if observado is None:
            motivo = "Sin observación en el año analizado: la serie observada termina en su último dato."
        elif promedio is None:
            motivo = "Sin referencias con esa fecha (por ejemplo, 29 de febrero)."

        punto = {
            "clave": f"{anio}-{cursor.month:02d}-{cursor.day:02d}",
            "etiqueta": f"{cursor.day} {MESES_ABREVIADOS[cursor.month - 1]}",
            "semana": None,
            "mes_dia": f"{cursor.month:02d}-{cursor.day:02d}",
            "fecha_desde": cursor.isoformat(),
            "fecha_hasta": cursor.isoformat(),
            "observado": observado,
            "referencia_promedio": promedio,
            "referencia_min": min(valores) if valores else None,
            "referencia_max": max(valores) if valores else None,
            "referencias": len(valores),
            "parcial": cursor > fin_datos,
            "estado": (
                "ausente" if observado is None else ("sin_referencia" if promedio is None else "ok")
            ),
            "motivo": motivo,
        }
        puntos.append(_con_diferencias(punto))
        cursor += timedelta(days=1)

    desde_completo = inicio == date(anio, 1, 1)
    hasta_completo = fin_datos >= date(anio, 12, 31)
    completo = desde_completo and hasta_completo
    if completo:
        etiqueta = f"Acumulado del año {anio}"
    elif not desde_completo:
        etiqueta = f"Acumulado desde el inicio disponible ({inicio.isoformat()})"
    else:
        etiqueta = f"Acumulado del año {anio} hasta el último dato ({fin_datos.isoformat()})"
    acumulado = {
        "completo": completo,
        "desde": inicio.isoformat(),
        "hasta": fin_datos.isoformat(),
        "etiqueta": etiqueta,
        "nota": (
            None
            if completo
            else (
                "El acumulado no cubre el año completo: empieza en la primera fecha disponible "
                "del conjunto y/o termina en el último dato observado. La referencia histórica sí "
                "puede llegar al 31 de diciembre y se identifica como referencia."
            )
        ),
    }
    return puntos, acumulado


MESES_ABREVIADOS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def _con_diferencias(punto: dict) -> dict:
    """Añade diferencia absoluta y porcentual cuando son calculables."""
    observado = punto.get("observado")
    promedio = punto.get("referencia_promedio")
    if observado is None or promedio is None:
        punto["diferencia"] = None
        punto["diferencia_pct"] = None
        return punto
    diferencia = round(observado - promedio, 2)
    punto["diferencia"] = diferencia
    punto["diferencia_pct"] = round(100 * diferencia / promedio, 2) if promedio > 0 else None
    return punto


def historico(
    db: Database,
    version: str,
    *,
    filtros: dict,
    anio: int,
    modo: str,
) -> dict:
    """Comparación descriptiva del año analizado contra años anteriores completos.

    Reglas (se publican en la respuesta):

    - solo años **anteriores** al analizado, con cobertura completa del 1 de enero al
      31 de diciembre; los incompletos se excluyen y se declara el motivo;
    - nunca se usan años posteriores: no hay información del futuro;
    - semanas ISO completas comparadas por número de semana; el acumulado se alinea por
      mes y día y se calcula por año antes de promediar;
    - sin años de referencia no se dibuja banda ni promedio;
    - un día sin dato es ``None``, nunca cero.
    """
    if modo not in MODOS_HISTORICO:
        raise HistoricoInvalido(
            f"Modo de comparación desconocido: «{modo}». Valores válidos: {', '.join(MODOS_HISTORICO)}"
        )
    cobertura = cobertura_publicada(db, version)
    desde, hasta = cobertura["desde"], cobertura["hasta"]
    anios_disponibles = list(range(desde.year, hasta.year + 1))
    if anio not in anios_disponibles:
        raise HistoricoInvalido(
            f"El año {anio} sale de la cobertura publicada ({desde.isoformat()} – {hasta.isoformat()}). "
            f"Años disponibles: {', '.join(str(a) for a in anios_disponibles)}."
        )

    total = db[M5_AGREGADOS].find_one(
        {"version": version, "dimension": "total"}, {"d_inicio": 1, "unidades": 1}
    )
    if not total:
        raise HistoricoInvalido("No hay agregados de la versión publicada")
    dias = list(range(int(total["d_inicio"]), int(total["d_inicio"]) + len(total["unidades"])))
    vector = _vector_contexto(
        db,
        version,
        tiendas=list(filtros["tiendas"]),
        categorias=list(filtros["categorias"]),
        departamentos=list(filtros["departamentos"]),
        dias=dias,
    )
    fechas_version = _fechas_version(db, version)
    unidades: dict[date, int] = {}
    for dia, valor in zip(dias, vector, strict=False):
        if valor is None:
            continue
        fecha = fechas_version.get(dia)
        if fecha is not None:
            unidades[fecha] = valor

    anios_completos = [a for a in anios_disponibles if _anio_completo(a, desde, hasta)]
    referencias = [a for a in anios_completos if a < anio]
    excluidos = [
        {
            "anio": a,
            "motivo": (
                "Cobertura incompleta: el conjunto empieza el "
                f"{desde.isoformat()} y no cubre desde el 1 de enero."
                if a == desde.year and desde > date(a, 1, 1)
                else (
                    "Cobertura incompleta: el conjunto termina el "
                    f"{hasta.isoformat()} y no llega al 31 de diciembre."
                )
            ),
        }
        for a in anios_disponibles
        if a < anio and a not in referencias
    ]

    if modo == "semana":
        puntos = _puntos_semana(
            anio=anio, unidades=unidades, referencias=referencias, desde=desde, hasta=hasta
        )
        acumulado = None
        regla = REGLA_SEMANA
        etiqueta_modo = "Por semana (ISO, lunes a domingo)"
    else:
        puntos, acumulado = _puntos_acumulado(
            anio=anio, unidades=unidades, referencias=referencias, desde=desde, hasta=hasta
        )
        regla = REGLA_ACUMULADO
        etiqueta_modo = acumulado["etiqueta"]

    con_referencia = [punto for punto in puntos if punto["estado"] == "ok"]
    anotaciones = _anotaciones(puntos, modo=modo, anio=anio)
    ultimo_con_dato = None
    for punto in reversed(puntos):
        if punto["observado"] is not None:
            ultimo_con_dato = punto
            break

    return {
        "anio": anio,
        "anios_disponibles": anios_disponibles,
        "modo": modo,
        "etiqueta_modo": etiqueta_modo,
        "regla_alineacion": regla,
        "cobertura_anio": {
            "desde": max(date(anio, 1, 1), desde).isoformat(),
            "hasta": min(date(anio, 12, 31), hasta).isoformat(),
            "completa": _anio_completo(anio, desde, hasta),
            "motivo": (
                None
                if _anio_completo(anio, desde, hasta)
                else "El año analizado no tiene cobertura completa del conjunto."
            ),
        },
        "referencias": {
            "anios_incluidos": referencias,
            "anios_excluidos": excluidos,
            "n_anios": len(referencias),
            "criterio": (
                "Años anteriores al analizado con cobertura completa del 1 de enero al "
                "31 de diciembre. Los años posteriores no se usan nunca."
            ),
        },
        "sin_referencia": len(referencias) == 0,
        "una_referencia": len(referencias) == 1,
        "unidad": "unidades vendidas",
        "puntos": puntos,
        "puntos_con_referencia": len(con_referencia),
        "anotaciones": anotaciones,
        "ultimo_con_dato": ultimo_con_dato,
        "acumulado": acumulado,
        "nota_banda": NOTA_BANDA,
        "nota_alcance": (
            "La referencia puede cubrir el año completo aunque el año analizado termine "
            "antes: el promedio histórico continúa y se identifica como referencia."
        ),
    }


def _anotaciones(puntos: list[dict], *, modo: str, anio: int) -> list[dict]:
    """Uno o dos hallazgos calculados (nunca juicios causales)."""
    salida: list[dict] = []
    comparables = [punto for punto in puntos if punto["estado"] == "ok"]
    if not comparables:
        return salida

    cierre = comparables[-1]
    salida.append(
        {
            "tipo": "cierre",
            "etiqueta": "Cierre del periodo observado",
            "clave": cierre["clave"],
            "fecha": cierre["fecha_hasta"],
            "observado": cierre["observado"],
            "referencia_promedio": cierre["referencia_promedio"],
            "diferencia": cierre["diferencia"],
            "diferencia_pct": cierre["diferencia_pct"],
            "texto": (
                "última "
                + ("semana" if modo == "semana" else "fecha")
                + f" con dato ({cierre['fecha_hasta']}): diferencia respecto al promedio histórico"
            ),
        }
    )
    candidatos = [punto for punto in comparables if punto["clave"] != cierre["clave"]]
    if candidatos:
        mayor = max(candidatos, key=lambda punto: abs(punto["diferencia"] or 0))
        # En el acumulado la diferencia crece con el año: si el máximo coincide con el
        # cierre (o casi), se omite para no repetir el mismo hallazgo.
        posiciones = {punto["clave"]: indice for indice, punto in enumerate(puntos)}
        distancia = abs(posiciones.get(mayor["clave"], 0) - posiciones.get(cierre["clave"], 0))
        parecido = abs(mayor["diferencia"] or 0) <= 1.05 * abs(cierre["diferencia"] or 0)
        redundante = distancia <= 2 or (distancia <= 10 and parecido)
        if abs(mayor["diferencia"] or 0) > 0 and not redundante:
            salida.append(
                {
                    "tipo": "maxima_diferencia",
                    "etiqueta": "Mayor diferencia del año",
                    "clave": mayor["clave"],
                    "fecha": mayor["fecha_hasta"],
                    "observado": mayor["observado"],
                    "referencia_promedio": mayor["referencia_promedio"],
                    "diferencia": mayor["diferencia"],
                    "diferencia_pct": mayor["diferencia_pct"],
                    "texto": (
                        "mayor diferencia absoluta respecto al promedio histórico en el año "
                        f"{anio}"
                    ),
                }
            )
    return salida
