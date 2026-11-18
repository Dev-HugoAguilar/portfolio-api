"""Endpoints del comparativo financiero del sector bancario mexicano (CNBV).

Datos **públicos reales** del Boletín Estadístico de Banca Múltiple de la CNBV.
No hay respaldo con el dataset sintético de desarrollo: si falta la fuente real,
estos endpoints fallan con un mensaje explícito.

Todo lo que se devuelve es verificable en el archivo importado:

* ``valor`` en millones de pesos (unidad del boletín, sin conversión);
* la institución ``sistema`` es el agregado publicado, no una suma nuestra;
* los conceptos de estado de resultados se marcan como flujo acumulado y se
  comparan contra el mismo mes del año anterior;
* participaciones y concentración se calculan solo cuando existe el agregado del
  sistema en el boletín.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CnbvFiltrosDep, CnbvQueryDep, CnbvServiceDep
from app.models.cnbv import (
    CnbvCatalogo,
    CnbvDetalle,
    CnbvIndicadores,
    CnbvResumen,
    CnbvSeries,
)

router = APIRouter(prefix="/cnbv/banca-multiple", tags=["CNBV · Banca Múltiple"])

_NOTA_FUENTE = (
    "Datos públicos reales. Fuente: Comisión Nacional Bancaria y de Valores (CNBV), "
    "Boletín Estadístico de Banca Múltiple. Cifras en millones de pesos, tal como se "
    "publican."
)


@router.get(
    "/catalogo",
    response_model=CnbvCatalogo,
    summary="Fuente, boletines importados, conceptos e instituciones",
    description=(
        "Describe la procedencia (portal, archivo, hash y fecha de importación de cada "
        "boletín), las condiciones de uso, los periodos cubiertos, el catálogo de "
        "conceptos financieros con su unidad y naturaleza, y las instituciones "
        "disponibles.\n\n" + _NOTA_FUENTE
    ),
)
def catalogo(servicio: CnbvServiceDep) -> CnbvCatalogo:
    return servicio.catalogo()


@router.get(
    "/resumen",
    response_model=CnbvResumen,
    summary="Comparativo de un concepto en un periodo",
    description=(
        "Valor publicado del sector (columna «Sistema»), comparación con el periodo "
        "comparable, tabla por institución con participación y variación, serie del "
        "sector y advertencias.\n\n"
        "La comparación es la siguiente: para saldos, el periodo importado inmediato "
        "anterior; para flujos acumulados (estado de resultados), el mismo mes del año "
        "anterior. Si no existe el periodo comparable, `variacion_*` es `null` y "
        "`comparacion.motivo` lo explica.\n\n" + _NOTA_FUENTE
    ),
)
def resumen(filtros: CnbvFiltrosDep, servicio: CnbvServiceDep) -> CnbvResumen:
    return servicio.resumen(filtros)


@router.get(
    "/series",
    response_model=CnbvSeries,
    summary="Serie por concepto e institución",
    description=(
        "Serie de los periodos importados para un concepto, con una serie por "
        "institución y el valor del sistema en cada periodo.\n\n" + _NOTA_FUENTE
    ),
)
def series(
    servicio: CnbvServiceDep,
    concepto: Annotated[str | None, Query(description="Concepto financiero del catálogo.")] = None,
    instituciones: Annotated[
        list[str] | None, Query(alias="institucion", description="Institución; repetir para varias.")
    ] = None,
    periodo_desde: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
    periodo_hasta: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
) -> CnbvSeries:
    from app.cnbv import conceptos as catalogo_conceptos

    return servicio.series(
        concepto or catalogo_conceptos.CONCEPTO_POR_OMISION,
        instituciones=tuple(instituciones or ()),
        desde=periodo_desde,
        hasta=periodo_hasta,
    )


@router.get(
    "/detalle",
    response_model=CnbvDetalle,
    summary="Detalle paginado de hechos importados",
    description=(
        "Tabla de hechos (periodo × institución × concepto) con los filtros "
        "indicados. Cada fila conserva el archivo y la fila de origen para poder "
        "conciliarla contra el archivo de la CNBV.\n\n" + _NOTA_FUENTE
    ),
)
def detalle(
    servicio: CnbvServiceDep,
    consulta: CnbvQueryDep,
    periodo: Annotated[
        str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="AAAA-MM")
    ] = None,
    concepto: Annotated[str | None, Query()] = None,
    instituciones: Annotated[list[str] | None, Query(alias="institucion")] = None,
    seccion: Annotated[
        str | None,
        Query(pattern="^(balance|resultados|cuentas_orden|sin_clasificar)$"),
    ] = None,
) -> CnbvDetalle:
    return servicio.detalle(
        periodo=periodo,
        concepto=concepto,
        instituciones=tuple(instituciones or ()),
        seccion=seccion,
        consulta=consulta,
    )


@router.get(
    "/indicadores",
    response_model=CnbvIndicadores,
    summary="Indicadores publicados por la CNBV (ROA y ROE)",
    description=(
        "Porcentajes tal como los publica el boletín, con su nota de metodología. No "
        "se recalculan ni se promedian.\n\n" + _NOTA_FUENTE
    ),
)
def indicadores(
    servicio: CnbvServiceDep,
    periodo: Annotated[
        str | None,
        Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Periodo del boletín."),
    ] = None,
) -> CnbvIndicadores:
    return servicio.indicadores(periodo_boletin=periodo)
