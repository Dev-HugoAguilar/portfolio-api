"""Endpoints del tablero «Marketing y clientes» (GA4 · BigQuery → MongoDB Atlas).

Una sola versión publicada alimenta todas las vistas: resumen, adquisición,
conversión, cohortes, productos, tabla y exportación comparten los mismos filtros,
así que no pueden mostrar cifras distintas del mismo conjunto.

La API **no consulta Google**: lee la versión activa de MongoDB (Atlas). BigQuery
solo interviene en la extracción que prepara una versión nueva.

Las respuestas indican si vienen de caché y cuánto tardó el cálculo en las
cabeceras ``X-Marketing-Cache`` y ``X-Marketing-Ms``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import DatabaseDep, MarketingFiltrosDep, MarketingServiceDep
from app.marketing.ingesta import estado_publico
from app.models.marketing import DIMENSIONES_COHORTE

router = APIRouter(prefix="/marketing", tags=["Marketing y clientes · GA4"])

_NOTA = (
    "Datos: muestra histórica y ofuscada de eventos de comercio electrónico de "
    "Google Merchandise Store en el conjunto público "
    "«bigquery-public-data.ga4_obfuscated_sample_ecommerce» (1 nov 2020 – 31 ene "
    "2021), cargada en MongoDB Atlas. No son resultados actuales ni información "
    "comercial íntegra de Google."
)


def _cabeceras(respuesta: Response, servicio, desde_cache: bool) -> None:
    respuesta.headers["X-Marketing-Cache"] = "hit" if desde_cache else "miss"
    respuesta.headers["X-Marketing-Ms"] = str(getattr(servicio, "ultimo_calculo_ms", 0))


@router.get(
    "/estado",
    summary="Versión publicada, origen, periodo y estado de la persistencia",
    description=(
        "Indica si hay una versión publicada en MongoDB, cuál es, de cuándo se "
        "cargó, qué periodo cubre y dónde está la copia original. No consulta "
        "Google: responde con lo guardado.\n\n" + _NOTA
    ),
)
def estado(base: DatabaseDep) -> dict:
    return estado_publico(base.db)


@router.get(
    "/catalogo",
    summary="Fuente, dominios de filtro, definiciones y capacidades",
    description=(
        "Devuelve la procedencia, el periodo observado, los valores reales "
        "disponibles para cada filtro, las definiciones de cada métrica y las "
        "capacidades habilitadas según el esquema detectado.\n\n" + _NOTA
    ),
)
def catalogo(filtros: MarketingFiltrosDep, servicio: MarketingServiceDep) -> dict:
    return servicio.catalogo(filtros)


@router.get(
    "/resumen",
    summary="Indicadores, serie diaria y mapa de desempeño de canales",
    description=(
        "Eventos, usuarios distintos, sesiones, compradores, compras, ingresos y "
        "conversión del rango filtrado, con su definición y su base. Incluye la "
        "serie diaria y el mapa de canales (usuarios, conversión, compras).\n\n"
        + _NOTA
    ),
)
def resumen(
    filtros: MarketingFiltrosDep, servicio: MarketingServiceDep, respuesta: Response
) -> dict:
    datos, cacheado = servicio.desde_cache("resumen", filtros, lambda: servicio.resumen(filtros))
    _cabeceras(respuesta, servicio, cacheado)
    return datos


@router.get(
    "/adquisicion",
    summary="Ranking de canales con usuarios, conversión, compras e ingresos",
    description=(
        "Agrupa por canal de adquisición (fuente / medio de la **primera actividad "
        "observada** de cada usuario): cada persona pertenece a un solo canal, así "
        "que las filas suman el total de usuarios únicos. Con un canal filtrado "
        "devuelve además su evolución, dispositivos y países.\n\n" + _NOTA
    ),
)
def adquisicion(
    filtros: MarketingFiltrosDep, servicio: MarketingServiceDep, respuesta: Response
) -> dict:
    datos, cacheado = servicio.desde_cache(
        "adquisicion", filtros, lambda: servicio.adquisicion(filtros)
    )
    _cabeceras(respuesta, servicio, cacheado)
    return datos


@router.get(
    "/conversion",
    summary="Embudo por sesiones, transiciones y comparación por dimensión",
    description=(
        "Pasos ver artículo → añadir al carrito → iniciar pago → comprar, contados "
        "en **sesiones** que los cumplen en orden dentro de una ventana de 24 horas. "
        "Incluye el desglose por canal y dispositivo y la distribución del tiempo "
        "entre etapas.\n\n" + _NOTA
    ),
)
def conversion(
    filtros: MarketingFiltrosDep, servicio: MarketingServiceDep, respuesta: Response
) -> dict:
    datos, cacheado = servicio.desde_cache(
        "conversion", filtros, lambda: servicio.conversion(filtros)
    )
    _cabeceras(respuesta, servicio, cacheado)
    return datos


@router.get(
    "/cohortes",
    summary="Matriz de cohortes, curvas por segmento y puntos emparejados",
    description=(
        "Matriz de cohortes semanales y, para la dimensión elegida, curvas de "
        "retención por segmento con base elegible (solo cohortes que podían "
        "observar la semana) y puntos emparejados de retorno y compra en S1.\n\n"
        + _NOTA
    ),
)
def cohortes(
    filtros: MarketingFiltrosDep,
    servicio: MarketingServiceDep,
    respuesta: Response,
    dimension: Annotated[
        str, Query(pattern="^(canal|dispositivo|pais)$", description="Dimensión de la cohorte.")
    ] = "canal",
) -> dict:
    datos, cacheado = servicio.desde_cache(
        "cohortes", filtros, lambda: servicio.cohortes(filtros, dimension), extra=dimension
    )
    _cabeceras(respuesta, servicio, cacheado)
    return datos


@router.get(
    "/productos",
    summary="Artículos y categorías con granularidad declarada",
    description=(
        "Artículos más comprados y categorías. Declara si cada cifra cuenta compras "
        "que contienen la categoría, líneas de artículo, unidades o importe de la "
        "línea: nunca reparte el ingreso de una compra entre sus categorías.\n\n"
        + _NOTA
    ),
)
def productos(
    filtros: MarketingFiltrosDep,
    servicio: MarketingServiceDep,
    respuesta: Response,
    limite: Annotated[int, Query(ge=3, le=30)] = 10,
) -> dict:
    datos, cacheado = servicio.desde_cache(
        "productos", filtros, lambda: servicio.productos(filtros, limite), extra=str(limite)
    )
    _cabeceras(respuesta, servicio, cacheado)
    return datos


@router.get(
    "/tabla",
    summary="Extracto de eventos para la tabla de detalle",
    description=(
        "Filas paginadas de un extracto determinista de eventos del embudo (hasta N "
        "por día). La cobertura se declara en cada respuesta y no incluye "
        "identificadores de personas.\n\n" + _NOTA
    ),
)
def tabla(
    filtros: MarketingFiltrosDep,
    servicio: MarketingServiceDep,
    orden: Annotated[
        str, Query(pattern="^(fecha|evento|dispositivo|canal|articulo|ingresos)$")
    ] = "fecha",
    sentido: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
    pagina: Annotated[int, Query(ge=1)] = 1,
    tamano: Annotated[int, Query(ge=1, le=200)] = 25,
) -> dict:
    return servicio.tabla(
        filtros, orden=orden, descendente=sentido == "desc", pagina=pagina, tamano=tamano
    )


@router.get(
    "/exportar",
    summary="CSV con los agregados del rango: serie diaria, canales y total",
    description=(
        "Exporta lo que el tablero muestra: una fila por día, una por canal de "
        "adquisición y el total del rango filtrado, con una cabecera que documenta "
        "fuente, condición, versión, muestra (población, proporción y predicado), "
        "filtros y definiciones. El modelo compacto no guarda eventos uno a uno, así "
        "que no hay detalle de eventos que exportar; la afinidad de categorías no "
        "depende de los filtros y se consulta en `/productos`.\n\n" + _NOTA
    ),
)
def exportar(filtros: MarketingFiltrosDep, servicio: MarketingServiceDep) -> Response:
    contenido, nombre = servicio.exportar(filtros)
    return Response(
        content=contenido.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@router.get(
    "/dimensiones",
    summary="Dimensiones disponibles para la matriz de cohortes",
    description="Lista cerrada de dimensiones que acepta `/cohortes`.",
)
def dimensiones() -> dict:
    return {
        "dimensiones": [
            {"valor": clave, "etiqueta": etiqueta}
            for clave, etiqueta in DIMENSIONES_COHORTE.items()
        ],
        "unidad": "usuario",
        "granularidad": "semana",
        "estado_api": status.HTTP_200_OK,
    }
