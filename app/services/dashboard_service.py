"""Servicio del dashboard de rentabilidad.

Orquesta repositorios y aplica las reglas de negocio:

* los KPIs salen de sumas agregadas en MongoDB;
* los porcentajes se derivan de esas sumas (nunca del promedio de porcentajes);
* la comparación solo cambia las fechas y conserva los filtros de negocio;
* las opciones de filtro se calculan respetando el resto de filtros.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone

from app.config import Settings
from app.models.filters import SORT_FIELDS, SalesFilters, SalesQuery
from app.models.responses import (
    Comparacion,
    GrupoDimension,
    Kpis,
    Meta,
    OpcionFiltro,
    OpcionesFiltros,
    Periodo,
    ProductoDestacado,
    ProfitabilitySummary,
    PuntoDispersion,
    PuntoSerie,
    SalesPage,
)
from app.repositories.sales_repository import OPTION_FIELDS, SalesRepository
from app.services import metrics
from app.services.catalog_service import CatalogService

logger = logging.getLogger(__name__)

MONEDA = "MXN"

ADVERTENCIA_SINTETICA = (
    "Datos sintéticos de demostración: no representan información real de ninguna empresa."
)
ADVERTENCIA_DEVOLUCIONES = (
    "No se incluyen devoluciones ni impuestos: la venta neta es la venta bruta menos el "
    "descuento de línea."
)
ADVERTENCIA_OPEX = (
    "No se calcula utilidad operativa porque no se modelan gastos operativos."
)

#: Dimensión de filtro -> campo equivalente en ``OpcionesFiltros``.
OPTION_FIELD_NAMES: dict[str, str] = {
    "regions": "regiones",
    "channels": "canales",
    "categories": "categorias",
    "products": "productos",
    "customers": "clientes",
}

# Ambas tablas deben cubrir exactamente las mismas dimensiones.
assert set(OPTION_FIELD_NAMES) == set(OPTION_FIELDS), "dimensiones de filtro desalineadas"


def month_bounds(clave: str) -> tuple[date, date]:
    """Primer y último día del mes ``YYYY-MM``."""
    anio, mes = (int(parte) for parte in clave.split("-")[:2])
    desde = date(anio, mes, 1)
    hasta = date(anio + (1 if mes == 12 else 0), (mes % 12) + 1, 1) - timedelta(days=1)
    return desde, hasta


def map_line(doc: dict) -> dict:
    """Documento de MongoDB -> línea de detalle de la API."""
    return {
        "linea_id": doc["linea_id"],
        "pedido_id": doc["pedido_id"],
        "fecha": doc["fecha"].date() if hasattr(doc["fecha"], "date") else doc["fecha"],
        "cliente_id": doc["cliente_id"],
        "cliente": doc["cliente_nombre"],
        "region": doc["region"],
        "canal": doc["canal"],
        "producto_id": doc["producto_id"],
        "producto": doc["producto_nombre"],
        "categoria": doc["categoria"],
        "cantidad": doc["cantidad"],
        "precio_unitario_cent": doc["precio_unitario_cent"],
        "descuento_cent": doc["descuento_cent"],
        "coste_unitario_cent": doc["coste_unitario_cent"],
        "venta_bruta_cent": doc["venta_bruta_cent"],
        "venta_neta_cent": doc["venta_neta_cent"],
        "coste_cent": doc["coste_cent"],
        "margen_cent": doc["margen_cent"],
        "margen_pct": metrics.margin_pct(doc["venta_neta_cent"], doc["margen_cent"]),
        "moneda": doc.get("moneda", MONEDA),
    }


class DashboardService:
    def __init__(
        self,
        sales: SalesRepository,
        catalog: CatalogService,
        settings: Settings,
    ) -> None:
        self._sales = sales
        self._catalog = catalog
        self._settings = settings

    # ------------------------------------------------------------ periodo
    def resolve_period(self, dataset_id: str) -> tuple[date, date]:
        """Rango por defecto: los últimos ``default_range_days`` con datos."""
        meta = self._catalog.metadata_or_404(dataset_id)
        desde_ds, hasta_ds = self._catalog.period_of(meta)
        desde = max(desde_ds, hasta_ds - timedelta(days=self._settings.default_range_days - 1))
        return desde, hasta_ds

    # ------------------------------------------------------------- resumen
    def summary(self, filters: SalesFilters) -> ProfitabilitySummary:
        meta = self._catalog.metadata_or_404(filters.dataset_id)
        ref = self._catalog.dataset_ref(meta)
        bundle = self._sales.summary_bundle(filters.mongo_match())

        totales = self._first_total(bundle.get("totales"))
        kpis = self._kpis(totales)
        comparacion = self._comparison(filters, meta, kpis)

        return ProfitabilitySummary(
            dataset=ref,
            dataset_version=ref.dataset_version,
            filtros=filters.normalized(),
            periodo=self._periodo(filters),
            kpis=kpis,
            comparacion=comparacion,
            serie=self._serie(bundle.get("serie") or [], filters),
            por_region=self._grupos(bundle.get("regiones") or []),
            por_canal=self._grupos(bundle.get("canales") or []),
            productos_destacados=self._productos(bundle.get("productos") or []),
            dispersion=self._dispersion(bundle.get("clientes") or []),
            opciones_filtros=self._opciones(filters),
            meta=self._meta(filters, totales, comparacion),
        )

    # -------------------------------------------------------------- detalle
    def sales_page(self, filters: SalesFilters, query: SalesQuery) -> SalesPage:
        meta = self._catalog.metadata_or_404(filters.dataset_id)
        ref = self._catalog.dataset_ref(meta)
        match = filters.mongo_match()

        total = self._sales.count(match)
        pages = math.ceil(total / query.page_size) if total else 0
        skip = (query.page - 1) * query.page_size

        items: list[dict] = []
        if total and skip < total:
            docs = self._sales.page(
                match,
                sort_field=SORT_FIELDS[query.sort],
                direction=-1 if query.order == "desc" else 1,
                skip=skip,
                limit=query.page_size,
            )
            items = [map_line(doc) for doc in docs]

        return SalesPage(
            items=items,
            total=total,
            page=query.page,
            page_size=query.page_size,
            pages=pages,
            orden={"campo": query.sort, "direccion": query.order},
            dataset=ref,
            dataset_version=ref.dataset_version,
            filtros=filters.normalized(),
            meta=self._meta(filters, self._first_total([{"lineas": total}]), None),
        )

    def export_lines(self, filters: SalesFilters, query: SalesQuery):
        """Generador de líneas para la exportación (no materializa todo)."""
        documentos = self._sales.iter_lines(
            filters.mongo_match(),
            sort_field=SORT_FIELDS[query.sort],
            direction=-1 if query.order == "desc" else 1,
            limit=self._settings.max_export_rows,
        )
        for documento in documentos:
            yield map_line(documento)

    def export_count(self, filters: SalesFilters) -> int:
        return self._sales.count(filters.mongo_match())

    # ------------------------------------------------------------- internos
    @staticmethod
    def _periodo(filters: SalesFilters) -> Periodo:
        return Periodo(desde=filters.date_from, hasta=filters.date_to, dias=filters.days)

    @staticmethod
    def _first_total(filas: list[dict] | None) -> dict:
        vacio = {
            "lineas": 0,
            "unidades": 0,
            "venta_bruta_cent": 0,
            "descuento_cent": 0,
            "venta_neta_cent": 0,
            "coste_cent": 0,
            "margen_cent": 0,
            "pedidos": 0,
            "clientes": 0,
        }
        if not filas:
            return vacio
        return {**vacio, **filas[0]}

    @staticmethod
    def _kpis(totales: dict) -> Kpis:
        return Kpis(
            lineas=totales["lineas"],
            pedidos=totales["pedidos"],
            clientes=totales["clientes"],
            unidades=totales["unidades"],
            venta_bruta_cent=totales["venta_bruta_cent"],
            descuento_cent=totales["descuento_cent"],
            venta_neta_cent=totales["venta_neta_cent"],
            coste_cent=totales["coste_cent"],
            margen_cent=totales["margen_cent"],
            margen_pct=metrics.margin_pct(totales["venta_neta_cent"], totales["margen_cent"]),
            ticket_medio_cent=metrics.ticket_medio(
                totales["venta_neta_cent"], totales["pedidos"]
            ),
        )

    def _comparison(
        self, filters: SalesFilters, meta: dict, kpis: Kpis
    ) -> Comparacion:
        anterior = metrics.previous_period(filters.date_from, filters.date_to)
        periodo = Periodo(desde=anterior.desde, hasta=anterior.hasta, dias=anterior.dias)

        desde_ds, hasta_ds = self._catalog.period_of(meta)
        cubierto = anterior.desde >= desde_ds and anterior.hasta <= hasta_ds

        if kpis.lineas == 0:
            return Comparacion(periodo=periodo, motivo="sin_datos_actuales")
        if not cubierto:
            return Comparacion(periodo=periodo, motivo="sin_cobertura_periodo_anterior")

        previos = self._first_total(
            [self._sales.totals(filters.with_dates(anterior.desde, anterior.hasta).mongo_match())]
        )
        if previos["lineas"] == 0 or previos["venta_neta_cent"] == 0:
            return Comparacion(
                periodo=periodo,
                kpis=self._kpis(previos) if previos["lineas"] else None,
                motivo="base_cero",
            )

        previos_kpis = self._kpis(previos)
        return Comparacion(
            periodo=periodo,
            kpis=previos_kpis,
            variacion_venta_neta_pct=metrics.variation_pct(
                kpis.venta_neta_cent, previos_kpis.venta_neta_cent
            ),
            variacion_margen_pct=metrics.variation_pct(
                kpis.margen_cent, previos_kpis.margen_cent
            ),
            variacion_margen_pt=metrics.variation_points(
                kpis.margen_pct, previos_kpis.margen_pct
            ),
        )

    @staticmethod
    def _serie(filas: list[dict], filters: SalesFilters) -> list[PuntoSerie]:
        serie: list[PuntoSerie] = []
        for fila in filas:
            clave = fila["_id"]
            desde, hasta = month_bounds(clave)
            desde = max(desde, filters.date_from)
            hasta = min(hasta, filters.date_to)
            if hasta < desde:
                continue
            serie.append(
                PuntoSerie(
                    periodo=clave,
                    desde=desde,
                    hasta=hasta,
                    lineas=fila["lineas"],
                    venta_neta_cent=fila["venta_neta_cent"],
                    margen_cent=fila["margen_cent"],
                    margen_pct=metrics.margin_pct(
                        fila["venta_neta_cent"], fila["margen_cent"]
                    ),
                )
            )
        return serie

    @staticmethod
    def _grupos(filas: list[dict]) -> list[GrupoDimension]:
        return [
            GrupoDimension(
                valor=fila["_id"]["v"],
                etiqueta=fila["_id"]["e"],
                lineas=fila["lineas"],
                venta_neta_cent=fila["venta_neta_cent"],
                margen_cent=fila["margen_cent"],
                margen_pct=metrics.margin_pct(fila["venta_neta_cent"], fila["margen_cent"]),
            )
            for fila in filas
        ]

    @staticmethod
    def _productos(filas: list[dict]) -> list[ProductoDestacado]:
        return [
            ProductoDestacado(
                producto_id=fila["_id"]["v"],
                nombre=fila["_id"]["e"],
                categoria=fila["_id"]["c"],
                lineas=fila["lineas"],
                unidades=fila["unidades"],
                venta_neta_cent=fila["venta_neta_cent"],
                margen_cent=fila["margen_cent"],
                margen_pct=metrics.margin_pct(fila["venta_neta_cent"], fila["margen_cent"]),
            )
            for fila in filas
        ]

    @staticmethod
    def _dispersion(filas: list[dict]) -> list[PuntoDispersion]:
        return [
            PuntoDispersion(
                cliente_id=fila["_id"]["v"],
                cliente=fila["_id"]["e"],
                venta_neta_cent=fila["venta_neta_cent"],
                margen_cent=fila["margen_cent"],
                margen_pct=metrics.margin_pct(fila["venta_neta_cent"], fila["margen_cent"]),
            )
            for fila in filas
        ]

    def _opciones(self, filters: SalesFilters) -> OpcionesFiltros:
        filas = self._sales.option_rows(filters)

        resultado: dict[str, list[OpcionFiltro]] = {}
        for dimension, campo in OPTION_FIELD_NAMES.items():
            seleccionados = set(filters.values_for(dimension))
            opciones = [
                OpcionFiltro(
                    valor=fila["_id"]["v"],
                    etiqueta=fila["_id"]["e"],
                    conteo=fila["conteo"],
                    seleccionado=fila["_id"]["v"] in seleccionados,
                )
                for fila in filas.get(dimension, [])
            ]
            # Una selección activa siempre permanece visible, aunque tenga 0 filas.
            # Solo en ese caso hay que buscar su etiqueta fuera de las opciones.
            presentes = {opcion.valor for opcion in opciones}
            faltantes = [valor for valor in filters.values_for(dimension) if valor not in presentes]
            if faltantes:
                etiquetas = self._sales.labels_for(
                    filters.dataset_id, dimension=dimension, values=faltantes
                )
                opciones.extend(
                    OpcionFiltro(
                        valor=valor,
                        etiqueta=etiquetas.get(valor, valor),
                        conteo=0,
                        seleccionado=True,
                    )
                    for valor in faltantes
                )
            resultado[campo] = opciones

        return OpcionesFiltros(**resultado)

    def _meta(
        self, filters: SalesFilters, totales: dict, comparacion: Comparacion | None
    ) -> Meta:
        avisos = [ADVERTENCIA_SINTETICA, ADVERTENCIA_DEVOLUCIONES, ADVERTENCIA_OPEX]
        if totales["lineas"] == 0:
            avisos.append("Ningún dato coincide con los filtros actuales.")
        if comparacion is not None:
            if comparacion.motivo == "sin_cobertura_periodo_anterior":
                avisos.append(
                    "El periodo anterior no está cubierto por el dataset; no se calculan "
                    "variaciones."
                )
            elif comparacion.motivo == "base_cero":
                avisos.append(
                    "El periodo anterior no tiene venta neta: las variaciones relativas no "
                    "son calculables."
                )
            elif comparacion.motivo == "sin_datos_actuales":
                avisos.append(
                    "El periodo actual no tiene datos: no se calculan variaciones."
                )
        return Meta(generado_en=datetime.now(timezone.utc), moneda=MONEDA, advertencias=avisos)
