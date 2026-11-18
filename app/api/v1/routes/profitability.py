"""Endpoints del dashboard de rentabilidad comercial.

Los tres endpoints comparten el mismo normalizador de filtros
(:func:`app.api.deps.resolve_sales_filters`), de modo que el resumen, el detalle
y la exportación siempre hablan del mismo conjunto de líneas.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.deps import CatalogDep, DashboardDep, FiltersDep, QueryDep, SettingsDep
from app.models.responses import ProfitabilitySummary, SalesPage
from app.services.export_service import csv_rows, export_headers

router = APIRouter(prefix="/dashboards", tags=["Dashboards"])

_FILTROS_DOC = (
    "Filtros: `dataset_id`, `date_from`, `date_to` (días naturales UTC, inclusivos) y "
    "listas multi-valor `region`, `channel`, `category`, `product`, `customer`. "
    "Los valores son slugs en minúsculas; se deduplican y se ordenan."
)


@router.get(
    "/profitability/summary",
    response_model=ProfitabilitySummary,
    summary="Resumen de rentabilidad con comparación de periodos",
    description=(
        "KPIs del periodo, comparación con el periodo anterior equivalente, serie "
        "mensual, desglose por región y canal, productos destacados, dispersión "
        "venta/margen por cliente y opciones para los filtros.\n\n" + _FILTROS_DOC
    ),
)
def profitability_summary(filters: FiltersDep, dashboard: DashboardDep) -> ProfitabilitySummary:
    return dashboard.summary(filters)


@router.get(
    "/profitability/sales",
    response_model=SalesPage,
    summary="Detalle de líneas de venta (paginado)",
    description=(
        "Mismas métricas y filtros que el resumen, a nivel de línea. Orden permitido: "
        "`fecha`, `venta_neta_cent`, `margen_cent`, `margen_pct`, `cantidad`, "
        "`producto`, `cliente`, `region`, `canal`.\n\n" + _FILTROS_DOC
    ),
)
def profitability_sales(
    filters: FiltersDep, query: QueryDep, dashboard: DashboardDep
) -> SalesPage:
    return dashboard.sales_page(filters, query)


@router.get(
    "/profitability/sales.csv",
    summary="Exportación CSV del detalle filtrado",
    description=(
        "Aplica exactamente los mismos filtros que el detalle. Hay un límite de filas "
        "por exportación; si se supera, la respuesta es 413 y hay que acotar el rango. "
        "Las celdas de texto que empiezan por `=`, `+`, `-`, `@`, tabulador o retorno "
        "de carro se prefijan con un apóstrofo para evitar inyección de fórmulas; las "
        "celdas numéricas no se modifican.\n\n" + _FILTROS_DOC
    ),
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/csv": {}}, "description": "CSV en UTF-8 con BOM."},
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "El filtro supera el máximo de filas exportables."
        },
    },
)
def profitability_sales_csv(
    filters: FiltersDep,
    query: QueryDep,
    dashboard: DashboardDep,
    catalog: CatalogDep,
    settings: SettingsDep,
) -> StreamingResponse:
    total = dashboard.export_count(filters)
    if total > settings.max_export_rows:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"La exportación tiene {total} filas y el máximo es "
                f"{settings.max_export_rows}. Acota el rango de fechas o añade filtros."
            ),
        )

    meta = catalog.metadata_or_404(filters.dataset_id)
    version = int(meta.get("dataset_version", 1))

    return StreamingResponse(
        csv_rows(dashboard.export_lines(filters, query)),
        media_type="text/csv; charset=utf-8",
        headers=export_headers(filters.dataset_id, version, total),
    )
