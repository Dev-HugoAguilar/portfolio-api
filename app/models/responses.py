"""Modelos de salida de la API.

Convenciones (se repiten en la documentación):

* Todo importe monetario viaja en **centavos enteros** con sufijo ``_cent``.
  La moneda es siempre MXN en esta fase y se declara en ``moneda``.
* ``margen_pct`` está en **puntos porcentuales** (0–100) y se calcula a partir de
  las sumas agregadas, nunca promediando porcentajes de línea.
  Es ``null`` cuando la venta neta es 0.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Periodo(_Base):
    desde: date
    hasta: date
    dias: int


class DatasetRef(_Base):
    dataset_id: str
    dataset_version: int
    esquema_version: str
    origen: str
    moneda: str


class DatasetInfo(_Base):
    """Catálogo de datasets disponibles."""

    dataset_id: str
    nombre: str
    descripcion: str
    origen: str
    moneda: str
    dataset_version: int
    esquema_version: str
    periodo: Periodo
    filas: int
    pedidos: int
    clientes: int
    productos: int
    semilla: int | None = None
    generado_en: datetime
    inmutable: bool = True
    advertencia: str = (
        "Datos sintéticos de demostración. No representan información real de "
        "ninguna empresa."
    )


class Kpis(_Base):
    lineas: int
    pedidos: int
    clientes: int
    unidades: int
    venta_bruta_cent: int
    descuento_cent: int
    venta_neta_cent: int
    coste_cent: int
    margen_cent: int
    margen_pct: float | None
    ticket_medio_cent: int | None


class Comparacion(_Base):
    periodo: Periodo | None = None
    kpis: Kpis | None = None
    variacion_venta_neta_pct: float | None = None
    variacion_margen_pct: float | None = None
    variacion_margen_pt: float | None = None
    motivo: str | None = Field(
        default=None,
        description=(
            "Por qué no hay comparación: 'sin_cobertura_periodo_anterior', "
            "'base_cero' o 'sin_datos_actuales'."
        ),
    )


class PuntoSerie(_Base):
    periodo: str = Field(description="Año-mes en formato YYYY-MM")
    desde: date
    hasta: date
    lineas: int
    venta_neta_cent: int
    margen_cent: int
    margen_pct: float | None


class GrupoDimension(_Base):
    valor: str
    etiqueta: str
    lineas: int
    venta_neta_cent: int
    margen_cent: int
    margen_pct: float | None


class ProductoDestacado(_Base):
    producto_id: str
    nombre: str
    categoria: str
    lineas: int
    unidades: int
    venta_neta_cent: int
    margen_cent: int
    margen_pct: float | None


class PuntoDispersion(_Base):
    cliente_id: str
    cliente: str
    venta_neta_cent: int
    margen_cent: int
    margen_pct: float | None


class OpcionFiltro(_Base):
    valor: str
    etiqueta: str
    conteo: int = Field(description="Líneas disponibles con el resto de filtros aplicados")
    seleccionado: bool = False


class OpcionesFiltros(_Base):
    regiones: list[OpcionFiltro] = Field(default_factory=list)
    canales: list[OpcionFiltro] = Field(default_factory=list)
    categorias: list[OpcionFiltro] = Field(default_factory=list)
    productos: list[OpcionFiltro] = Field(default_factory=list)
    clientes: list[OpcionFiltro] = Field(default_factory=list)


class Meta(_Base):
    generado_en: datetime
    moneda: str = "MXN"
    granularidad: str = "mes"
    moneda_nota: str = "Importes en centavos de MXN; dividir entre 100 para mostrar."
    advertencias: list[str] = Field(default_factory=list)


class ProfitabilitySummary(_Base):
    dataset: DatasetRef
    dataset_version: int
    filtros: dict
    periodo: Periodo
    kpis: Kpis
    comparacion: Comparacion
    serie: list[PuntoSerie]
    por_region: list[GrupoDimension]
    por_canal: list[GrupoDimension]
    productos_destacados: list[ProductoDestacado]
    dispersion: list[PuntoDispersion]
    opciones_filtros: OpcionesFiltros
    meta: Meta


class SalesLine(_Base):
    linea_id: str
    pedido_id: str
    fecha: date
    cliente_id: str
    cliente: str
    region: str
    canal: str
    producto_id: str
    producto: str
    categoria: str
    cantidad: int
    precio_unitario_cent: int
    descuento_cent: int
    coste_unitario_cent: int
    venta_bruta_cent: int
    venta_neta_cent: int
    coste_cent: int
    margen_cent: int
    margen_pct: float | None
    moneda: str = "MXN"


class SalesPage(_Base):
    items: list[SalesLine]
    total: int
    page: int
    page_size: int
    pages: int
    orden: dict
    dataset: DatasetRef
    dataset_version: int
    filtros: dict
    meta: Meta


class HealthLive(_Base):
    status: str = "ok"
    version: str


class HealthReady(_Base):
    status: str
    mongo: str
    version: str
