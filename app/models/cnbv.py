"""Modelos de la API del comparativo bancario (CNBV, datos reales).

Convenciones propias de esta fuente (distintas del dashboard comercial)

* ``valor`` va en **millones de pesos** (``unidad = "millones_mxn"``), tal como lo
  publica la CNBV: no se convierte ni se normaliza a otra escala.
* Los indicadores (ROA, ROE) son **porcentajes** publicados por la CNBV y no se
  recalculan.
* ``naturaleza`` distingue ``saldo`` (estado de situación financiera, comparable
  entre meses) de ``flujo_acumulado`` (estado de resultado integral, acumulado
  del ejercicio: no comparable contra el mes inmediato anterior).
* La institución ``sistema`` es el agregado publicado por la CNBV; nunca se suma
  con las demás instituciones.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.cnbv import sources

PERIODO_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CnbvFiltros(BaseModel):
    """Filtros normalizados de una consulta del comparativo bancario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str = sources.DATASET_ID
    periodo: str
    concepto: str
    instituciones: tuple[str, ...] = ()
    incluir_sistema: bool = True

    @field_validator("periodo")
    @classmethod
    def _periodo_valido(cls, valor: str) -> str:
        valor = (valor or "").strip()
        if not PERIODO_PATTERN.match(valor):
            raise ValueError("periodo debe tener el formato AAAA-MM (por ejemplo 2024-10)")
        return valor

    @field_validator("concepto")
    @classmethod
    def _concepto_valido(cls, valor: str) -> str:
        valor = (valor or "").strip()
        if not valor or len(valor) > 160:
            raise ValueError("concepto debe ser un identificador del catálogo")
        return valor

    @field_validator("instituciones", mode="before")
    @classmethod
    def _instituciones_normalizadas(cls, valor: object) -> tuple[str, ...]:
        if valor is None:
            return ()
        valores = [valor] if isinstance(valor, str) else list(valor)
        limpios = [str(item).strip().lower() for item in valores if str(item).strip()]
        return tuple(sorted(set(limpios)))

    def normalized(self) -> dict:
        return {
            "dataset_id": self.dataset_id,
            "periodo": self.periodo,
            "concepto": self.concepto,
            "instituciones": list(self.instituciones),
            "incluir_sistema": self.incluir_sistema,
        }


class CnbvConsulta(BaseModel):
    """Paginación del detalle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1)
    orden: str = "valor"

    @field_validator("orden")
    @classmethod
    def _orden_permitido(cls, valor: str) -> str:
        permitidos = {"valor", "institucion", "concepto", "periodo"}
        if valor not in permitidos:
            raise ValueError(f"orden no permitido; usa uno de: {', '.join(sorted(permitidos))}")
        return valor


# ---------------------------------------------------------------- catálogo
class CnbvFuente(_Base):
    dataset_id: str
    nombre: str
    organismo: str
    portal: str
    pagina_estadistica: str
    sector: str
    sector_nombre: str
    url_base_archivos: str
    patron_archivo: str
    moneda: str
    unidad_saldos: str
    unidad_indicadores: str
    atribucion: str
    condiciones_uso: list[str]
    advertencias_metrica: list[str]
    es_sintetico: bool = False
    periodos_verificados: list[str]
    periodos_por_omision: list[str]


class CnbvBoletin(_Base):
    periodo: str
    fecha: date
    archivo: str
    url: str
    sha256: str
    revision: int
    bytes: int
    titulo_hoja: str | None = None
    unidad_declarada: str | None = None
    importado_en: datetime | None = None
    instituciones: int
    conceptos: int
    hechos: int
    indicadores: int
    celdas_ausentes: int
    rechazados: int


class CnbvConcepto(_Base):
    concepto_id: str
    nombre: str
    seccion: str
    naturaleza: str
    unidad: str
    orden: int
    periodos_con_dato: int
    destacado: bool = False


class CnbvInstitucion(_Base):
    institucion_id: str
    nombre: str
    es_sistema: bool
    periodos_con_dato: int


class CnbvCatalogo(_Base):
    fuente: CnbvFuente
    boletines: list[CnbvBoletin]
    periodos: list[str]
    conceptos: list[CnbvConcepto]
    instituciones: list[CnbvInstitucion]
    indicadores: list[dict]
    cobertura: dict
    advertencias: list[str]


# ----------------------------------------------------------------- resumen
class CnbvKpis(_Base):
    valor_sistema: float | None
    unidad: str
    naturaleza: str
    instituciones_con_dato: int
    instituciones_sin_dato: int
    participacion_mayor_pct: float | None
    institucion_mayor: str | None
    concentracion_top5_pct: float | None
    mediana_instituciones: float | None
    maximo_institucion: float | None
    minimo_institucion: float | None


class CnbvComparacion(_Base):
    periodo_comparado: str | None = None
    tipo: str | None = Field(
        default=None,
        description=(
            "'periodo_anterior' para saldos; 'mismo_mes_anio_anterior' para flujos "
            "acumulados."
        ),
    )
    valor_sistema_periodo_comparado: float | None = None
    variacion: float | None = None
    variacion_sistema: float | None = None
    variacion_sistema_pct: float | None = None
    motivo: str | None = None


class CnbvFilaInstitucion(_Base):
    institucion_id: str
    institucion: str
    es_sistema: bool
    valor: float | None
    participacion_pct: float | None
    valor_periodo_comparado: float | None = None
    variacion: float | None = None
    variacion_pct: float | None = None
    posicion: int | None = None


class CnbvPuntoSerie(_Base):
    periodo: str
    fecha: date
    valor: float | None
    valor_sistema: float | None = None
    instituciones_con_dato: int


class CnbvResumen(_Base):
    fuente: CnbvFuente
    boletin: CnbvBoletin
    filtros: dict
    concepto: CnbvConcepto
    periodo: str
    fecha: date
    kpis: CnbvKpis
    comparacion: CnbvComparacion
    por_institucion: list[CnbvFilaInstitucion]
    serie: list[CnbvPuntoSerie]
    valores_ausentes: dict
    cobertura: dict
    advertencias: list[str]
    generado_en: datetime


class CnbvSerieInstitucion(_Base):
    institucion_id: str
    institucion: str
    es_sistema: bool
    puntos: list[CnbvPuntoSerie]


class CnbvSeries(_Base):
    fuente: CnbvFuente
    filtros: dict
    concepto: CnbvConcepto
    periodos: list[str]
    series: list[CnbvSerieInstitucion]
    advertencias: list[str]
    generado_en: datetime


class CnbvDetalleFila(_Base):
    periodo: str
    fecha: date
    concepto_id: str
    concepto: str
    seccion: str
    naturaleza: str
    institucion_id: str
    institucion: str
    es_sistema: bool
    valor: float
    unidad: str
    moneda: str
    archivo: str
    fila_origen: int


class CnbvDetalle(_Base):
    items: list[CnbvDetalleFila]
    total: int
    page: int
    page_size: int
    pages: int
    orden: str
    filtros: dict
    unidad: str
    advertencias: list[str]
    generado_en: datetime


class CnbvIndicador(_Base):
    indicador_id: str
    nombre: str
    nota: str | None = None
    unidad: str
    periodo: str
    valores: list[dict]


class CnbvIndicadores(_Base):
    fuente: CnbvFuente
    periodo_boletin: str
    indicadores: list[CnbvIndicador]
    advertencias: list[str]
    generado_en: datetime
