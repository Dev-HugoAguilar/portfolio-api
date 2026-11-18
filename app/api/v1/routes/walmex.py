"""Endpoints del tablero «Pulso comercial · Walmex».

Sirve ventas, crecimiento y desempeño comercial por trimestre y mercado a partir de
la versión publicada en MongoDB Atlas. La API no descarga documentos ni consulta
Walmex: responde con lo publicado y declara la fuente de cada cifra.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import WalmexFiltrosDep, WalmexServiceDep

router = APIRouter(prefix="/walmex", tags=["Pulso comercial · Walmex"])

_NOTA = (
    "Datos oficiales de Walmart de México y Centroamérica publicados en walmex.mx "
    "(comunicados trimestrales y estados financieros). Importes en millones de pesos "
    "nominales; variaciones frente al mismo trimestre del año anterior."
)


def _cabeceras(respuesta: Response, servicio, desde_cache: bool) -> None:
    respuesta.headers["X-Walmex-Cache"] = "hit" if desde_cache else "miss"
    respuesta.headers["X-Walmex-Ms"] = str(getattr(servicio, "ultimo_calculo_ms", 0))


@router.get(
    "/estado",
    summary="Versión publicada, último trimestre, cobertura y persistencia",
    description=(
        "Indica la versión activa, el último trimestre publicado (con su fecha de "
        "comunicado), cuántos trimestres cubre, el modelo y la persistencia.\n\n" + _NOTA
    ),
)
def estado(servicio: WalmexServiceDep) -> dict:
    return servicio.estado()


@router.get(
    "/catalogo",
    summary="Indicadores, mercados, periodos, bases y combinaciones disponibles",
    description=(
        "Devuelve el catálogo publicado: definición y unidad de cada indicador, los "
        "mercados, los periodos con su fecha de publicación y las combinaciones que "
        "realmente existen. Nada fuera de esta lista se puede filtrar.\n\n" + _NOTA
    ),
)
def catalogo(filtros: WalmexFiltrosDep, servicio: WalmexServiceDep) -> dict:
    return servicio.catalogo(filtros)


@router.get(
    "/resumen",
    summary="Indicadores y series de ingresos y crecimiento por mercado",
    description=(
        "Indicadores del último periodo del rango (ingresos, crecimiento anual, "
        "unidades iguales, tráfico y ticket) y series trimestrales por mercado, con la "
        "conciliación México + Centroamérica = Consolidado.\n\n" + _NOTA
    ),
)
def resumen(
    filtros: WalmexFiltrosDep, servicio: WalmexServiceDep, respuesta: Response
) -> dict:
    datos, cacheado = servicio.desde_cache("resumen", filtros, lambda: servicio.resumen(filtros))
    _cabeceras(respuesta, servicio, cacheado)
    return datos


@router.get(
    "/motores",
    summary="Tráfico y ticket por trimestre y mercado",
    description=(
        "Puntos trimestrales de variación de tráfico (eje X) y de ticket (eje Y) por "
        "mercado, con los trimestres en los que la empresa no publicó alguno de los dos. "
        "No se presenta como descomposición exacta del crecimiento.\n\n" + _NOTA
    ),
)
def motores(
    filtros: WalmexFiltrosDep, servicio: WalmexServiceDep, respuesta: Response
) -> dict:
    datos, cacheado = servicio.desde_cache("motores", filtros, lambda: servicio.motores(filtros))
    _cabeceras(respuesta, servicio, cacheado)
    return datos


@router.get(
    "/desempeno",
    summary="Matriz de desempeño por mercado y trimestre, y comparación del último periodo",
    description=(
        "Matriz mercado × trimestre para el indicador elegido, con estado por celda "
        "(publicado, cero o no publicado) y el orden de los mercados en el último "
        "trimestre del rango.\n\n" + _NOTA
    ),
)
def desempeno(
    filtros: WalmexFiltrosDep, servicio: WalmexServiceDep, respuesta: Response
) -> dict:
    datos, cacheado = servicio.desde_cache(
        "desempeno", filtros, lambda: servicio.desempeno(filtros)
    )
    _cabeceras(respuesta, servicio, cacheado)
    return datos


@router.get(
    "/matriz",
    summary="Indicadores de variación × mercado × trimestre en una sola lectura",
    description=(
        "Devuelve las celdas de varios indicadores de variación (crecimiento anual, "
        "unidades iguales, tráfico y ticket) por mercado y trimestre, con su base, su "
        "unidad, su fuente y el estado de cada celda: publicado, cero o no publicado. "
        "Sirve al mapa de calor y a las series de motores sin colecciones nuevas.\n\n"
        + _NOTA
    ),
)
def matriz(
    filtros: WalmexFiltrosDep, servicio: WalmexServiceDep, respuesta: Response
) -> dict:
    datos, cacheado = servicio.desde_cache("matriz", filtros, lambda: servicio.matriz(filtros))
    _cabeceras(respuesta, servicio, cacheado)
    return datos


@router.get(
    "/fuentes",
    summary="Documentos oficiales con URL, hash, fecha y páginas",
    description=(
        "Lista los documentos usados en la versión: comunicados trimestrales y "
        "estados financieros, con su URL, hash SHA-256, fecha de publicación y la "
        "página o tabla de la que sale cada cifra.\n\n" + _NOTA
    ),
)
def fuentes(filtros: WalmexFiltrosDep, servicio: WalmexServiceDep) -> dict:
    return servicio.fuentes(filtros)


@router.get(
    "/exportar",
    summary="CSV con los datos agrupados del rango",
    description=(
        "Exporta los hechos publicados que cumplen los filtros, con su unidad, base y "
        "referencia de fuente, más una cabecera con versión, unidades y filtros.\n\n" + _NOTA
    ),
)
def exportar(filtros: WalmexFiltrosDep, servicio: WalmexServiceDep) -> Response:
    contenido, nombre = servicio.exportar(filtros)
    return Response(
        content=contenido.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@router.get(
    "/dimensiones",
    summary="Mercados e indicadores aceptados por los filtros",
    description="Lista cerrada de mercados e indicadores que acepta el tablero.",
)
def dimensiones() -> dict:
    return {
        "mercados": [
            {"valor": "consolidado", "etiqueta": "Consolidado"},
            {"valor": "mexico", "etiqueta": "México"},
            {"valor": "centroamerica", "etiqueta": "Centroamérica"},
        ],
        "indicadores": [
            {"valor": "crecimiento_anual", "etiqueta": "Crecimiento anual"},
            {"valor": "mismas_tiendas", "etiqueta": "Unidades iguales"},
            {"valor": "trafico", "etiqueta": "Tráfico"},
            {"valor": "ticket", "etiqueta": "Ticket"},
            {"valor": "total_ingresos", "etiqueta": "Total de ingresos"},
        ],
        "unidad_periodo": "trimestre",
    }
