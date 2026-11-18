"""Contratos de la API «Atlas comercial · Demanda y ventas» (Walmart M5).

Reglas del contrato:

- Cantidades: enteras **no negativas**; un día sin observación se representa con
  ``valor: null`` y nunca con cero.
- Porcentajes: ``float`` finito; si el denominador es cero, ``null`` y una nota.
- Fechas: ``date`` de día comercial (sin hora ni zona horaria).
- Estados: enumeraciones explícitas, no cadenas libres.
- El estado «pendiente» existe: si no hay importación publicada, la API lo dice y
  no devuelve cifras inventadas.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Cantidad = Annotated[int, Field(ge=0, description="Unidades vendidas (enteras, ≥ 0).")]
Porcentaje = Annotated[float, Field(allow_inf_nan=False, description="Porcentaje finito.")]


class Agregacion(str, Enum):
    """Granularidad temporal de las series."""

    dia = "dia"
    semana = "semana"
    mes = "mes"


class NivelPronostico(str, Enum):
    """Nivel de agregación del pronóstico y de su evaluación."""

    total = "total"
    estado = "estado"
    tienda = "tienda"
    categoria = "categoria"
    departamento = "departamento"
    categoria_tienda = "categoria_tienda"
    departamento_tienda = "departamento_tienda"
    producto = "producto"


class NivelExploracion(str, Enum):
    """Nivel de exploración de «Pulso de ventas».

    ``tienda``       una serie por tienda seleccionada (vista de entrada).
    ``departamento`` una serie por departamento del contexto territorial.
    ``producto``     una serie por producto seleccionado (máximo cinco).
    """

    tienda = "tienda"
    departamento = "departamento"
    producto = "producto"


class ModoHistorico(str, Enum):
    """Modo de lectura de la comparación histórica."""

    semana = "semana"
    acumulado = "acumulado"


class EstadoDatos(str, Enum):
    """Estado de la persistencia del proyecto."""

    pendiente = "pendiente"
    validada = "validada"
    publicada = "publicada"


class Cobertura(BaseModel):
    """Intervalo inclusivo de días comerciales importados."""

    model_config = ConfigDict(extra="forbid")
    desde: date
    hasta: date
    dias: Annotated[int, Field(ge=0)]


class Capacidades(BaseModel):
    """Qué puede resolver de verdad la versión publicada."""

    model_config = ConfigDict(extra="forbid")
    agregaciones: list[Agregacion]
    filtros: list[str]
    niveles_pronostico: list[NivelPronostico]
    niveles_exploracion: list[NivelExploracion] = Field(
        default_factory=list, description="Niveles del explorador de «Pulso de ventas»."
    )
    derivado_departamento_tienda: bool = Field(
        default=False, description="El derivado tienda × departamento está publicado."
    )
    detalle_producto_tienda: bool = Field(
        default=True, description="La concentración por producto se calcula sobre bloques locales."
    )
    ingresos: bool = Field(default=False, description="Esta entrega no incluye ingresos ni precios.")


class EstadoRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str | None
    con_datos: bool
    estado: EstadoDatos
    mensaje: str | None = None
    cobertura: Cobertura | None = None
    importada_en: datetime | None = None
    unidad: str | None = None
    esquema: str | None = None
    destino: str | None = Field(
        default=None,
        description="host:puerto/base del MongoDB local del proyecto, sin credenciales.",
    )
    base: str | None = Field(default=None, description="Base exclusiva del proyecto.")
    calendario: dict | None = Field(
        default=None, description="Días de calendario, días con ventas y si se comparte."
    )
    derivado: dict | None = Field(
        default=None,
        description="Estado del derivado local «departamento_tienda» que sirve el drill-down.",
    )


# --------------------------------------------------------------------------- #
# Catálogo
# --------------------------------------------------------------------------- #


class Tienda(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_id: str
    state_id: str


class Departamento(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cat_id: str
    dept_id: str


class Producto(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    cat_id: str
    dept_id: str


class CatalogoRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    tiendas: list[Tienda]
    estados: list[str]
    categorias: list[str]
    departamentos: list[Departamento]
    productos_total: Annotated[int, Field(ge=0)]
    productos: list[Producto]
    paginacion: dict
    cobertura: Cobertura
    capacidades: Capacidades
    nota: str


class ProductoConUnidades(BaseModel):
    """Producto del contexto con sus unidades reales en el intervalo."""

    model_config = ConfigDict(extra="forbid")
    item_id: str
    dept_id: str
    cat_id: str
    unidades: Cantidad
    participacion_pct: Porcentaje | None = Field(
        default=None, description="Participación en el universo del departamento (mismo contexto y periodo)."
    )


class ProductosRespuesta(BaseModel):
    """Búsqueda paginada de productos del departamento y contexto vigentes."""

    model_config = ConfigDict(extra="forbid")
    version: str
    contexto: dict
    orden: str
    total: Annotated[int, Field(ge=0)]
    productos: list[ProductoConUnidades]
    universo: dict = Field(
        default_factory=dict,
        description="Unidades y número de productos del departamento: denominador de la participación.",
    )
    seleccion_inicial: list[dict] = Field(
        default_factory=list,
        description="Cinco productos con más unidades en el periodo (desempate por identificador).",
    )
    seleccion_inicial_participacion_pct: Porcentaje | None = Field(
        default=None, description="Participación conjunta de la selección inicial en el universo."
    )
    paginacion: dict
    cobertura: Cobertura | None = None
    nota: str


# --------------------------------------------------------------------------- #
# Pulso de ventas
# --------------------------------------------------------------------------- #


class ParametrosPulso(BaseModel):
    """Filtros con los que se calculó la respuesta (se devuelven tal cual)."""

    model_config = ConfigDict(extra="forbid")
    desde: date
    hasta: date
    agregacion: Agregacion
    tiendas: list[str]
    categorias: list[str]
    departamentos: list[str]
    productos: list[str]
    nivel: NivelExploracion = NivelExploracion.tienda
    tienda: str | None = Field(default=None, description="Tienda en contexto (niveles departamento y producto).")
    estado: str | None = Field(default=None, description="Estado en contexto o como filtro territorial.")
    departamento: str | None = Field(default=None, description="Departamento en contexto (nivel producto).")
    a: Cobertura | None = None
    b: Cobertura | None = None
    ab_por_omision: bool = Field(
        default=False,
        description="True = A y B son las dos mitades del intervalo; no se eligieron.",
    )


class EstadoPanorama(BaseModel):
    """Un estado del panorama territorial."""

    model_config = ConfigDict(extra="forbid")
    estado: str
    nombre: str
    tiendas: list[str]
    tiendas_incluidas: Annotated[int, Field(ge=0)]
    unidades: Cantidad
    participacion_pct: Porcentaje | None = Field(
        default=None, description="Participación en el total del contexto territorial."
    )


class Panorama(BaseModel):
    """Panorama territorial: estados con sus tiendas, unidades y participación."""

    model_config = ConfigDict(extra="forbid")
    total_unidades: Cantidad
    denominador: str
    estados: list[EstadoPanorama]
    estados_incluidos: Annotated[int, Field(ge=0)]
    estados_catalogo: Annotated[int, Field(ge=0)]
    unidad: str


class FilaComposicion(BaseModel):
    """Fila de la composición comercial (categoría o departamento)."""

    model_config = ConfigDict(extra="forbid")
    codigo: str
    etiqueta: str
    categoria: str | None = None
    unidades: Cantidad
    participacion_pct: Porcentaje | None = None
    denominador: str


class Composicion(BaseModel):
    """Composición del catálogo en el contexto territorial y el periodo."""

    model_config = ConfigDict(extra="forbid")
    total_unidades: Cantidad
    unidad: str
    categorias: list[FilaComposicion]
    departamentos: list[FilaComposicion]
    denominador_categorias: str
    denominador_departamentos: str
    nota: str


class CuboSerie(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clave: str
    etiqueta: str
    total: Cantidad
    dias: Annotated[int, Field(ge=0)]
    dias_sin_observacion: Annotated[int, Field(ge=0)]
    parcial: bool


class SerieTienda(BaseModel):
    """Serie de un panel, sea tienda, departamento o producto.

    ``clave`` identifica la serie dentro del nivel (``store_id``, ``dept_id`` o
    ``item_id``); ``store_id`` se conserva para los consumidores anteriores y
    siempre indica la tienda del contexto.
    """

    model_config = ConfigDict(extra="forbid")
    clave: str | None = Field(default=None, description="Identificador real de la serie en su nivel.")
    nombre: str | None = Field(default=None, description="Etiqueta mostrada (identificador real de M5).")
    nivel: NivelExploracion | None = None
    store_id: str
    total: Cantidad
    dias_con_dato: Annotated[int, Field(ge=0)]
    valores: list[CuboSerie]


class MigaNavegacion(BaseModel):
    """Un paso de la ruta de exploración (migas navegables)."""

    model_config = ConfigDict(extra="forbid")
    nivel: NivelExploracion
    etiqueta: str
    tienda: str | None = None
    departamento: str | None = None


class ContextoPulso(BaseModel):
    """Ámbito efectivo de la respuesta: qué se explora y con qué alcance."""

    model_config = ConfigDict(extra="forbid")
    nivel: NivelExploracion
    estado: str | None = None
    tienda: str | None = None
    tiendas_contexto: list[str] = Field(
        default_factory=list, description="Tiendas del contexto territorial efectivo."
    )
    departamento: str | None = None
    departamentos: list[str] = Field(default_factory=list, description="Departamentos representados.")
    productos: list[str] = Field(default_factory=list, description="Productos representados.")
    productos_disponibles: Annotated[int, Field(ge=0)] = Field(
        default=0, description="Productos del departamento en contexto (catálogo real)."
    )
    seleccion_automatica: bool = Field(
        default=False, description="True = los cinco productos los eligió el servidor por volumen."
    )
    total_contexto: Cantidad = Field(default=0, description="Total del ámbito explorado.")
    total_seleccion: Cantidad = Field(default=0, description="Total de las series representadas.")
    participacion_pct: Porcentaje | None = Field(
        default=None, description="Peso de la selección sobre el ámbito; nulo si el ámbito es 0."
    )
    descripcion: str = ""


class DerivadoInfo(BaseModel):
    """Estado del derivado local que sirve este nivel (si aplica)."""

    model_config = ConfigDict(extra="forbid")
    nombre: str
    estado: str
    publicado: bool
    origen: str = Field(description="De dónde se leyó: derivado publicado o bloques locales.")
    version_datos: str
    huella: str | None = None
    documentos: Annotated[int, Field(ge=0)] | None = None
    dias: Annotated[int, Field(ge=0)] | None = None
    duracion_s: float | None = None
    construido_en: datetime | None = None
    reutilizado: bool = False
    reconciliacion: dict | None = None


class Dominio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desde: date
    hasta: date
    dias: Annotated[int, Field(ge=0)]
    agregacion: Agregacion


class ResumenPulso(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: Cantidad
    nivel: NivelExploracion = NivelExploracion.tienda
    series: Annotated[int, Field(ge=0)] = 0
    tiendas: Annotated[int, Field(ge=0)]
    total_contexto: Cantidad = 0
    total_seleccion: Cantidad = 0
    participacion_pct: Porcentaje | None = None
    dias: Annotated[int, Field(ge=0)]
    promedio_diario: Porcentaje | None
    unidad: str
    concentracion_total: Cantidad
    reconcilia_con_concentracion: bool


class FilaAB(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clave: str
    etiqueta: str = ""
    nivel: NivelExploracion | None = None
    categoria: str | None = Field(
        default=None, description="Compatibilidad con la vista anterior (solo nivel tienda)."
    )
    a: Cantidad
    b: Cantidad
    diferencia: int = Field(description="B − A, con signo.")
    variacion_pct: Porcentaje | None
    nota: str | None = None


class ComparacionAB(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nivel: NivelExploracion | None = None
    a: Cobertura | None
    b: Cobertura | None
    filas: list[FilaAB]
    no_causal: str


class PuntoConcentracion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    producto: str | None = None
    cierre: bool = Field(default=False, description="Punto de cierre (100 %, 100 %).")
    posicion: Annotated[int, Field(ge=0)] | None = None
    unidades: Cantidad | None = None
    acumulado: Cantidad
    participacion: Porcentaje | None = None
    porcentaje: Porcentaje
    porcentaje_productos: Porcentaje = Field(
        default=0.0, description="Porcentaje de productos acumulados (eje horizontal)."
    )


class Concentracion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    universo: str
    alcance: str = Field(default="", description="Ámbito explícito del universo (nivel y filtros).")
    productos: Annotated[int, Field(ge=0)]
    total: Cantidad
    puntos: list[PuntoConcentracion]
    cola: Annotated[int, Field(ge=0)] = 0
    lectura_80: dict | None = Field(
        default=None,
        description="Productos (enteros) que acumulan ≥ 80 % de las unidades, calculado con cantidades originales.",
    )
    exacto: dict | None = Field(
        default=None,
        description=(
            "Productos exactos del universo [item_id, unidades, cat_id, dept_id] en orden "
            "descendente con desempate por identificador. Permite calcular el mínimo número de "
            "productos para cualquier objetivo sin interpolar; no incluye series diarias."
        ),
    )
    cierre: dict | None = None
    estado: str = "ok"
    nota: str


class PlanificacionRespuesta(BaseModel):
    """Plan de ventas a cuatro semanas por artículo × tienda (página + totales filtrados)."""

    model_config = ConfigDict(extra="forbid")
    version: str
    run_id: str
    revision: str | None = None
    revision_politica: str | None = None
    revision_anterior: str | None = None
    politica_precision: str | None = None
    tiendas_contexto: list[str] = Field(default_factory=list)
    unidad: str
    origen_fecha: str | None = None
    horizonte_inicio: str | None = None
    horizonte_fin: str | None = None
    observado_desde: str | None = None
    observado_hasta: str | None = None
    orden: str
    orden_aplicado: list[dict] = Field(default_factory=list)
    desempate: str
    filtros: dict
    pagina: Annotated[int, Field(ge=1)]
    tamano: Annotated[int, Field(ge=1)]
    total: Annotated[int, Field(ge=0)]
    paginas: Annotated[int, Field(ge=1)]
    filas_ordenables: Annotated[int, Field(ge=0)]
    excluidas_por_orden: Annotated[int, Field(ge=0)] = 0
    semanas: list[str]
    semanas_fechas: list[dict] = Field(default_factory=list)
    resumen: dict
    filas: list[dict]
    nota: str


class DetallePlanRespuesta(BaseModel):
    """Detalle de una serie artículo × tienda dentro del plan."""

    model_config = ConfigDict(extra="forbid")
    version: str
    run_id: str
    revision: str | None = None
    unidad: str
    origen_fecha: str
    horizonte_inicio: str
    horizonte_fin: str
    observado_desde: str
    observado_hasta: str
    serie: dict
    fila: dict
    historico: list[dict]
    pronostico_diario: list[dict]
    semanas: list[dict]
    evaluacion_horizonte: list[dict]
    evaluacion_resumen: dict
    nota: str


class EvaluacionHorizonteRespuesta(BaseModel):
    """Evaluación del total de 28 días por serie y corte, con diagnóstico descriptivo."""

    model_config = ConfigDict(extra="forbid")
    version: str
    run_id: str
    revision: str | None = None
    unidad: str
    disponible: bool
    motivo: str | None = None
    granos: dict | None = None
    cortes: list[dict]
    consolidado: dict | None = None
    por_estado: list[dict] = Field(default_factory=list)
    por_categoria: list[dict] = Field(default_factory=list)
    por_departamento: list[dict] = Field(default_factory=list)
    por_volumen: list[dict] = Field(default_factory=list)
    por_intermitencia: list[dict] = Field(default_factory=list)
    sesgo: dict | None = None
    estados: list[dict] = Field(default_factory=list)
    cobertura: dict
    nota: str | None = None


class PulsoRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    version_derivado: str | None = Field(
        default=None, description="Huella del derivado local usado, si el nivel lo requiere."
    )
    generado_en: datetime
    nivel: NivelExploracion = NivelExploracion.tienda
    parametros: ParametrosPulso
    contexto: ContextoPulso | None = None
    migas: list[MigaNavegacion] = Field(default_factory=list)
    derivado: DerivadoInfo | None = None
    cobertura: Cobertura | None = None
    resumen: ResumenPulso
    dominio: Dominio
    series: list[SerieTienda]
    comparacion: ComparacionAB | None
    concentracion: Concentracion
    panorama: Panorama | None = None
    composicion: Composicion | None = None
    nota: str


# --------------------------------------------------------------------------- #
# Comparación histórica (año analizado contra años anteriores)
# --------------------------------------------------------------------------- #


class FiltrosHistoricos(BaseModel):
    """Filtros efectivos y alcance temporal de la comparación histórica."""

    model_config = ConfigDict(extra="forbid")
    tiendas: list[str]
    categorias: list[str]
    departamentos: list[str]
    estado: str | None = None
    tienda: str | None = None
    descripcion: str
    periodo_global: Cobertura = Field(description="Cobertura del conjunto; el detalle anual no la modifica.")
    alcance_temporal: str = Field(
        description="Qué años se usan y que el periodo global, A/B y concentración no cambian."
    )


class CoberturaAnio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desde: date
    hasta: date
    completa: bool
    motivo: str | None = None


class ReferenciasHistoricas(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anios_incluidos: list[int]
    anios_excluidos: list[dict]
    n_anios: Annotated[int, Field(ge=0)]
    criterio: str


class AcumuladoInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completo: bool
    desde: date
    hasta: date | None = None
    etiqueta: str
    nota: str | None = None


class PuntoHistorico(BaseModel):
    """Un punto de la comparación: semana ISO o fecha del año analizado."""

    model_config = ConfigDict(extra="forbid")
    clave: str
    etiqueta: str
    semana: Annotated[int, Field(ge=1, le=53)] | None = None
    mes_dia: str | None = None
    fecha_desde: date
    fecha_hasta: date
    observado: Cantidad | None = None
    referencia_promedio: float | None = Field(default=None, allow_inf_nan=False)
    referencia_min: Cantidad | None = None
    referencia_max: Cantidad | None = None
    referencias: Annotated[int, Field(ge=0)] = 0
    parcial: bool = False
    estado: str = Field(description="ok · sin_referencia · ausente")
    motivo: str | None = None
    diferencia: float | None = Field(default=None, allow_inf_nan=False)
    diferencia_pct: Porcentaje | None = None


class AnotacionHistorica(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tipo: str
    etiqueta: str
    clave: str
    fecha: date
    observado: Cantidad | None = None
    referencia_promedio: float | None = None
    diferencia: float | None = None
    diferencia_pct: Porcentaje | None = None
    texto: str


class HistoricoRespuesta(BaseModel):
    """Comparación descriptiva del año analizado contra años anteriores completos."""

    model_config = ConfigDict(extra="forbid")
    version: str
    generado_en: datetime
    anio: Annotated[int, Field(ge=2000, le=2100)]
    anios_disponibles: list[int]
    modo: ModoHistorico
    etiqueta_modo: str
    regla_alineacion: str
    cobertura_anio: CoberturaAnio
    referencias: ReferenciasHistoricas
    sin_referencia: bool
    una_referencia: bool
    unidad: str
    filtros: FiltrosHistoricos
    puntos: list[PuntoHistorico]
    puntos_con_referencia: Annotated[int, Field(ge=0)]
    anotaciones: list[AnotacionHistorica]
    ultimo_con_dato: PuntoHistorico | None = None
    acumulado: AcumuladoInfo | None = None
    nota_banda: str
    nota_alcance: str


# --------------------------------------------------------------------------- #
# Demanda futura
# --------------------------------------------------------------------------- #


class Historico(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clave: str
    valores: list[CuboSerie]


class SeriePronostico(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clave: str
    # La publicación decimal conserva los valores canónicos; el redondeo es de presentación.
    observado: list[float | None]
    pronostico: list[float]
    baseline: list[float] = Field(default_factory=list)
    metodo: str = Field(default="modelo", description="modelo · baseline (fallback) · estacional")


class CoberturaModelo(BaseModel):
    """Cobertura del candidato y del sistema completo."""

    model_config = ConfigDict(extra="forbid")
    series: Annotated[int, Field(ge=0)] = 0
    series_modelo: Annotated[int, Field(ge=0)] = 0
    fallback: Annotated[int, Field(ge=0)] = 0
    cobertura_candidato_pct: Porcentaje | None = None
    cobertura_sistema_pct: Porcentaje | None = None
    motivo_fallback: str | None = None
    nota: str | None = None


class ModeloResumen(BaseModel):
    """Identidad y estado de la ejecución que sirve la respuesta."""

    model_config = ConfigDict(extra="forbid")
    run_id: str
    variante: str
    etiqueta: str
    metodo: str
    estado: str
    recomendado: bool = False
    horizonte: Annotated[int, Field(ge=1)] = 28
    semilla: int | None = None
    version_precios: str | None = None
    supuestos: dict = Field(default_factory=dict)
    importancias: list[dict] = Field(default_factory=list)
    variables: list[str] = Field(default_factory=list)


class PronosticosRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    #: Revisión de publicación de la que salen las predicciones y el observado.
    revision: str | None = None
    politica_precision: str | None = None
    generado_en: datetime
    nivel: NivelPronostico
    metodo: str
    unidad: str
    origen_fecha: date
    origen_d: Annotated[int, Field(ge=1)]
    horizonte: Annotated[int, Field(ge=1, le=90)]
    corte: str = Field(description="Texto legible de la fecha de corte del pronóstico.")
    es_futuro: bool = Field(description="True = no existen valores reales posteriores en los datos.")
    nota: str
    dias: list[dict]
    historico_dias: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list)
    series: list[SeriePronostico]
    # --- modelo predictivo (incremento de demanda futura) -------------------
    run_id: str | None = None
    modelo: ModeloResumen | None = None
    baseline_metodo: str = "ingenuo estacional semanal"
    rango_objetivo: Cobertura | None = Field(
        default=None, description="Primera y última fecha pronosticada."
    )
    cobertura: CoberturaModelo | None = None
    supuestos: dict = Field(default_factory=dict)
    estado_evaluacion: str = Field(
        default="sin_observaciones",
        description="sin_observaciones (pronóstico posterior al corte) · con_observaciones",
    )
    metricas: "Metricas | None" = None
    baseline_metricas: "Metricas | None" = None
    filtros: dict = Field(default_factory=dict)


class Metricas(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: Annotated[int, Field(ge=0)]
    mae: Porcentaje | None
    sesgo: float | None = Field(default=None, allow_inf_nan=False)
    wape: Porcentaje | None
    denominador: Cantidad
    denominador_cero: bool = False
    ventanas_sin_base: Annotated[int, Field(ge=0)] | None = None
    ventanas: Annotated[int, Field(ge=0)] | None = Field(
        default=None, description="Número de ventanas agregadas (solo en el resumen)."
    )
    nota: str | None = None


class VentanaEvaluacion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nivel: NivelPronostico
    origen_d: Annotated[int, Field(ge=1)]
    origen_fecha: date
    fin_fecha: date
    horizonte: Annotated[int, Field(ge=1)]
    series: Annotated[int, Field(ge=0)]
    metricas: Metricas
    baseline: Metricas | None = None
    mejora_pct: Porcentaje | None = Field(
        default=None, description="Mejora del WAPE frente al baseline (positivo = mejor)."
    )


class PuntoHorizonte(BaseModel):
    """Error del día 1 al 28 (agregado sobre los orígenes evaluados)."""

    model_config = ConfigDict(extra="forbid")
    horizonte: Annotated[int, Field(ge=1, le=90)]
    candidato: Metricas
    baseline: Metricas
    mejora_pct: Porcentaje | None = None


class SegmentoEvaluacion(BaseModel):
    """Error de un segmento (estado, tienda, categoría o departamento)."""

    model_config = ConfigDict(extra="forbid")
    nivel: str
    clave: str
    series: Annotated[int, Field(ge=0)]
    candidato: Metricas
    baseline: Metricas
    mejora_pct: Porcentaje | None = None
    agregado: dict | None = Field(
        default=None,
        description="Error de la serie agregada del segmento (predicción sumada antes de medir).",
    )


class EvaluacionRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    nivel: NivelPronostico
    metodo: str
    unidad: str
    horizonte: Annotated[int, Field(ge=1)]
    resumen: Metricas
    definiciones: dict
    ventanas: list[VentanaEvaluacion]
    calculada_en: datetime | None = None
    limitaciones: list[str]
    # --- modelo predictivo (incremento de demanda futura) -------------------
    run_id: str | None = None
    variante: str | None = None
    etapa: str = Field(default="final", description="desarrollo (selección) · final (reservado)")
    baseline_resumen: Metricas | None = None
    mejora_pct: Porcentaje | None = None
    por_horizonte: list[PuntoHorizonte] = Field(default_factory=list)
    segmentos: list[SegmentoEvaluacion] = Field(default_factory=list)
    mejoras: dict | None = Field(
        default=None, description="Segmentos que mejoran y empeoran frente al baseline."
    )
    cobertura: CoberturaModelo | None = None
    modelo: ModeloResumen | None = None
    supuestos: dict = Field(default_factory=dict)


class EjecucionModelo(BaseModel):
    """Una ejecución publicada del modelo (metadatos y métricas)."""

    model_config = ConfigDict(extra="forbid")
    run_id: str
    variante: str
    etiqueta: str
    metodo: str
    estado: str
    recomendado: bool = False
    version_precios: str | None = None
    horizonte: Annotated[int, Field(ge=1)] = 28
    semilla: int | None = None
    origenes_entrenamiento: list[int] = Field(default_factory=list)
    ventana_entrenamiento: dict = Field(default_factory=dict)
    variables: list[str] = Field(default_factory=list)
    importancias: list[dict] = Field(default_factory=list)
    supuestos: dict = Field(default_factory=dict)
    cobertura: dict = Field(default_factory=dict)
    metricas_resumen: dict = Field(default_factory=dict)
    duracion_s: float | None = None
    memoria: dict = Field(default_factory=dict)
    entorno: dict = Field(default_factory=dict)
    artefacto: str | None = None
    creado_en: datetime | None = None


class ModelosRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    version_precios: str | None = None
    recomendado: str | None = None
    ejecuciones: list[EjecucionModelo] = Field(default_factory=list)
    nota: str


# --------------------------------------------------------------------------- #
# Exploración descriptiva (comparación A/B, estacionalidad, matriz, precios)
# --------------------------------------------------------------------------- #


class DimensionEstacionalidad(str, Enum):
    """Corte de la estacionalidad semanal: un grupo por estado, tienda o categoría."""

    estado = "estado"
    tienda = "tienda"
    categoria = "categoria"


class MetricaAB(str, Enum):
    """Métrica de la comparación entre periodos."""

    total = "total"
    promedio_diario = "promedio_diario"


class PeriodoInclusivo(BaseModel):
    """Intervalo inclusivo de días comerciales."""

    model_config = ConfigDict(extra="forbid")
    desde: date
    hasta: date
    dias: Annotated[int, Field(ge=1)]


class MetricaFila(BaseModel):
    """Valor de una métrica en A y B, con su diferencia y variación."""

    model_config = ConfigDict(extra="forbid")
    a: float | None = None
    b: float | None = None
    diferencia: float | None = None
    variacion_pct: Porcentaje | None = None
    motivo: str | None = Field(
        default=None, description="Por qué la variación no es calculable, si aplica."
    )


class FilaComparacionTiendaCategoria(BaseModel):
    """Una combinación real tienda × categoría comparada entre los periodos A y B."""

    model_config = ConfigDict(extra="forbid")
    clave: str = Field(description="Clave estable «store_id|cat_id».")
    store_id: str
    state_id: str | None = None
    cat_id: str
    etiqueta: str
    series: Annotated[int, Field(ge=0)]
    unidades_a: Cantidad
    unidades_b: Cantidad
    dias_validos_a: Annotated[int, Field(ge=0)]
    dias_esperados_a: Annotated[int, Field(ge=0)]
    dias_validos_b: Annotated[int, Field(ge=0)]
    dias_esperados_b: Annotated[int, Field(ge=0)]
    dias_cero_a: Annotated[int, Field(ge=0)]
    dias_cero_b: Annotated[int, Field(ge=0)]
    unidades_periodo: Cantidad
    dias_validos_periodo: Annotated[int, Field(ge=0)]
    dias_esperados_periodo: Annotated[int, Field(ge=0)]
    cobertura: str = Field(description="completa, parcial o sin_datos.")
    metricas: dict[str, MetricaFila]
    motivo: str | None = None


class GrupoTiendaAB(BaseModel):
    """Filas de una tienda, para la agrupación inicial del gráfico."""

    model_config = ConfigDict(extra="forbid")
    store_id: str
    state_id: str | None = None
    filas: list[str]


class ConteosContexto(BaseModel):
    """Conteos distintos del contexto; no se suman marginales entre tiendas."""

    model_config = ConfigDict(extra="forbid")
    tiendas: Annotated[int, Field(ge=0)]
    estados: Annotated[int, Field(ge=0)]
    categorias_unicas: Annotated[int, Field(ge=0)]
    combinaciones_tienda_categoria: Annotated[int, Field(ge=0)]
    departamentos_unicos: Annotated[int, Field(ge=0)]
    combinaciones_tienda_departamento: Annotated[int, Field(ge=0)]
    articulos_unicos: Annotated[int, Field(ge=0)]
    series_articulo_tienda: Annotated[int, Field(ge=0)]
    regla: str


class ReconciliacionAB(BaseModel):
    """Contraste entre la suma de las filas y el total del contexto."""

    model_config = ConfigDict(extra="forbid")
    filas_a: Cantidad
    contexto_a: Cantidad
    cuadra_a: bool
    filas_b: Cantidad
    contexto_b: Cantidad
    cuadra_b: bool
    nota: str


class ResumenFilasAB(BaseModel):
    """Cuántas filas se publican y con qué cobertura."""

    model_config = ConfigDict(extra="forbid")
    tiendas: Annotated[int, Field(ge=0)]
    categorias: Annotated[int, Field(ge=0)]
    combinaciones: Annotated[int, Field(ge=0)]
    filas_con_cobertura_completa: Annotated[int, Field(ge=0)]
    filas_con_cobertura_parcial: Annotated[int, Field(ge=0)]
    filas_sin_datos: Annotated[int, Field(ge=0)]


class ComparacionTiendaCategoriaRespuesta(BaseModel):
    """Comparación entre dos periodos por tienda × categoría."""

    model_config = ConfigDict(extra="forbid")
    version: str
    unidad: str
    metrica_inicial: MetricaAB
    metricas: list[MetricaAB]
    regla_promedio: str
    periodo: PeriodoInclusivo
    a: PeriodoInclusivo
    b: PeriodoInclusivo
    por_omision: bool = Field(
        description="True si A y B se dedujeron como las dos mitades del periodo."
    )
    filas: list[FilaComparacionTiendaCategoria]
    tiendas: list[GrupoTiendaAB]
    conteos: ConteosContexto
    resumen_filas: ResumenFilasAB
    reconciliacion: ReconciliacionAB
    filtros: dict = Field(default_factory=dict)
    calculo_ms: float | None = None


class PuntoSemanal(BaseModel):
    """Un día de la semana en el perfil semanal."""

    model_config = ConfigDict(extra="forbid")
    dia_semana: Annotated[int, Field(ge=1, le=7)]
    etiqueta: str
    unidades: Cantidad
    fechas_validas: Annotated[int, Field(ge=0)]
    promedio: float | None = None
    dias_cero: Annotated[int, Field(ge=0)]


class GrupoSemanal(BaseModel):
    """Perfil semanal de un grupo (estado, tienda o categoría)."""

    model_config = ConfigDict(extra="forbid")
    clave: str
    etiqueta: str
    tiendas: list[str] | None = None
    puntos: list[PuntoSemanal]
    total: Cantidad
    fechas_validas: Annotated[int, Field(ge=0)]


class SemanalInfo(BaseModel):
    """Bloque semanal de la estacionalidad."""

    model_config = ConfigDict(extra="forbid")
    grupos: list[GrupoSemanal]
    escala_comun_max: float | None = None
    metrica_inicial: str
    metricas: list[str]
    regla: str
    advertencia: str


class MesEstacional(BaseModel):
    """Un mes de un año con **cuatro** conteos distintos, que no se confunden.

    ``dias_del_mes`` es lo que dura el mes en el calendario; ``dias_en_intervalo`` son los
    que caen dentro del periodo solicitado; ``dias_con_cobertura`` los que además tienen
    cobertura de ventas publicada; y ``dias_observados`` los que tienen valor. Un día sin
    dato nunca se rellena con cero.
    """

    model_config = ConfigDict(extra="forbid")
    mes: Annotated[int, Field(ge=1, le=12)]
    etiqueta: str
    dias_del_mes: Annotated[int, Field(ge=0, le=31)] = Field(
        description="Días que tiene el mes en el calendario."
    )
    dias_en_intervalo: Annotated[int, Field(ge=0, le=31)] = Field(
        description="Días del mes dentro del periodo solicitado."
    )
    dias_con_cobertura: Annotated[int, Field(ge=0, le=31)] = Field(
        description="Días del mes dentro del periodo con cobertura de ventas publicada."
    )
    dias_observados: Annotated[int, Field(ge=0, le=31)] = Field(
        description="Días con valor observado; denominador del promedio."
    )
    dias_sin_dato: Annotated[int, Field(ge=0, le=31)] = Field(
        default=0, description="Días con cobertura pero sin observación."
    )
    dias_fuera_intervalo: Annotated[int, Field(ge=0, le=31)] = Field(
        default=0, description="Días del mes que quedan fuera del periodo solicitado."
    )
    unidades: Cantidad | None = None
    promedio_diario: float | None = Field(
        default=None, description="Unidades ÷ días observados (nunca días del mes)."
    )
    dias_cero: Annotated[int, Field(ge=0)] | None = None
    completo: bool = Field(description="True solo si hay observación en todos los días del mes.")
    parcial: bool
    motivo: str | None = Field(
        default=None, description="Qué límite deja el mes incompleto (datos o periodo)."
    )


class AnioEstacional(BaseModel):
    """Meses de un año."""

    model_config = ConfigDict(extra="forbid")
    anio: Annotated[int, Field(ge=2000, le=2100)]
    meses: list[MesEstacional]


class MensualInfo(BaseModel):
    """Bloque mensual de la estacionalidad."""

    model_config = ConfigDict(extra="forbid")
    anios: list[AnioEstacional]
    metrica_inicial: str
    metricas: list[str]
    nota: str
    cobertura: dict


class EstacionalidadRespuesta(BaseModel):
    """Estacionalidad semanal y mensual del contexto."""

    model_config = ConfigDict(extra="forbid")
    version: str
    unidad: str
    dimension: DimensionEstacionalidad
    dimensiones: list[DimensionEstacionalidad]
    periodo: PeriodoInclusivo
    dominio: dict = Field(
        default_factory=dict, description="Rango de fechas que se puede pedir en esta vista."
    )
    cobertura_ventas: dict = Field(
        default_factory=dict,
        description="Cobertura de ventas y cuántos días del periodo quedan fuera de ella.",
    )
    semanal: SemanalInfo
    mensual: MensualInfo
    filtros: dict = Field(default_factory=dict)
    calculo_ms: float | None = None


class DiaMatriz(BaseModel):
    """Columna de la matriz temporal: una fecha del año con su estado."""

    model_config = ConfigDict(extra="forbid")
    d: Annotated[int, Field(ge=1)] | None = Field(
        default=None,
        description="Día del conjunto de ventas (d_N) o null si la fecha no existe en el calendario importado.",
    )
    fecha: date
    mes: Annotated[int, Field(ge=1, le=12)]
    estado: Literal["observado", "sin_dato", "fuera_del_filtro"] = Field(
        default="observado",
        description=(
            "observado = fecha con cobertura y dentro del periodo; sin_dato = fecha sin "
            "cobertura de ventas; fuera_del_filtro = fecha con cobertura excluida por el periodo."
        ),
    )


class MarcaMes(BaseModel):
    """Marca de mes dentro de la matriz."""

    model_config = ConfigDict(extra="forbid")
    mes: Annotated[int, Field(ge=1, le=12)]
    etiqueta: str
    indice: Annotated[int, Field(ge=0)]
    dias: Annotated[int, Field(ge=1)]


class FilaMatrizTemporal(BaseModel):
    """Fila de la matriz temporal (una tienda)."""

    model_config = ConfigDict(extra="forbid")
    store_id: str
    state_id: str | None = None
    valores: list[Cantidad | None]
    total: Cantidad
    dias_observados: Annotated[int, Field(ge=0)]


class EscalaMatriz(BaseModel):
    """Dominio de color de la matriz."""

    model_config = ConfigDict(extra="forbid")
    dominio: list[float]
    tipo: str
    nota: str


class CoberturaMatriz(BaseModel):
    """Celdas de la matriz y su estado, con el periodo y el dominio declarados."""

    model_config = ConfigDict(extra="forbid")
    desde: date = Field(description="Primer día con cobertura de ventas.")
    hasta: date = Field(description="Último día con cobertura de ventas.")
    periodo_desde: date | None = Field(default=None, description="Inicio del periodo solicitado.")
    periodo_hasta: date | None = Field(default=None, description="Fin del periodo solicitado.")
    dias_del_anio: Annotated[int, Field(ge=365, le=366)] = Field(
        default=365, description="Columnas del eje anual (366 en año bisiesto)."
    )
    celdas: Annotated[int, Field(ge=0)]
    celdas_observadas: Annotated[int, Field(ge=0)] = 0
    celdas_sin_dato: Annotated[int, Field(ge=0)]
    celdas_fuera_filtro: Annotated[int, Field(ge=0)] = 0
    nota: str


class MatrizTemporalRespuesta(BaseModel):
    """Matriz tienda × día de un año del calendario."""

    model_config = ConfigDict(extra="forbid")
    version: str
    anio: Annotated[int, Field(ge=2000, le=2100)]
    unidad: str
    dias: list[DiaMatriz]
    meses: list[MarcaMes]
    tiendas: list[FilaMatrizTemporal]
    escala: EscalaMatriz
    cobertura: CoberturaMatriz
    calculo_ms: float | None = None


class PuntoVentaDiaria(BaseModel):
    """Venta observada de un día (o ausencia)."""

    model_config = ConfigDict(extra="forbid")
    d: Annotated[int, Field(ge=1)]
    fecha: date
    unidades: Cantidad | None = None


class PuntoPrecio(BaseModel):
    """Precio de una semana con sus fechas reales dentro del periodo."""

    model_config = ConfigDict(extra="forbid")
    wm_yr_wk: int
    inicio: date | None = None
    fin: date | None = None
    precio: float = Field(gt=0, description="USD por unidad; nunca cero.")


class InfoPrecios(BaseModel):
    """Precio semanal del artículo en la tienda."""

    model_config = ConfigDict(extra="forbid")
    version_precios: str | None = None
    puntos: list[PuntoPrecio] = Field(default_factory=list)
    motivo: str | None = None


class MarcaSnap(BaseModel):
    """Día con SNAP activo en el estado de la tienda."""

    model_config = ConfigDict(extra="forbid")
    fecha: date
    d: Annotated[int, Field(ge=1)]


class EventoCalendario(BaseModel):
    """Evento del calendario (nombre y tipo reales)."""

    model_config = ConfigDict(extra="forbid")
    fecha: date
    d: Annotated[int, Field(ge=1)]
    nombre: str
    tipo: str | None = None


class CapasCalendario(BaseModel):
    """Capas opcionales de contexto temporal."""

    model_config = ConfigDict(extra="forbid")
    snap: list[MarcaSnap] = Field(default_factory=list)
    eventos: list[EventoCalendario] = Field(default_factory=list)
    estado: str | None = None
    nota: str | None = None
    motivo: str | None = None


class IntermitenciaSerie(BaseModel):
    """Resumen de ceros de una serie; los huecos no son ceros."""

    model_config = ConfigDict(extra="forbid")
    dias_validos: Annotated[int, Field(ge=0)]
    dias_positivos: Annotated[int, Field(ge=0)]
    dias_cero: Annotated[int, Field(ge=0)]
    pct_cero: Porcentaje | None = None
    racha_maxima_ceros: Annotated[int, Field(ge=0)]
    primera_venta_positiva: date | None = None
    ultima_venta_positiva: date | None = None
    unidades: Cantidad


class ArticuloDetalle(BaseModel):
    """Identidad del artículo × tienda."""

    model_config = ConfigDict(extra="forbid")
    item_id: str
    tienda: str
    estado: str | None = None
    cat_id: str
    dept_id: str
    categoria: str


class ArticuloRespuesta(BaseModel):
    """Detalle de artículo × tienda: ventas, precio semanal y capas de calendario."""

    model_config = ConfigDict(extra="forbid")
    version: str
    articulo: ArticuloDetalle
    periodo: PeriodoInclusivo
    unidad: str
    ventas: list[PuntoVentaDiaria]
    precios: InfoPrecios
    capas: CapasCalendario
    intermitencia: IntermitenciaSerie
    nota: str
    filtros: dict = Field(default_factory=dict)


class TramoDistribucion(BaseModel):
    """Tramo del histograma de porcentaje de ceros."""

    model_config = ConfigDict(extra="forbid")
    etiqueta: str
    series: Annotated[int, Field(ge=0)]


class ResumenIntermitencia(BaseModel):
    """Cabecera del resumen de intermitencia."""

    model_config = ConfigDict(extra="forbid")
    series: Annotated[int, Field(ge=0)]
    series_con_datos: Annotated[int, Field(ge=0)]
    series_sin_datos: Annotated[int, Field(ge=0)]
    series_todas_cero: Annotated[int, Field(ge=0)]
    pct_cero_mediana: Porcentaje | None = None
    pct_cero_media: Porcentaje | None = None
    distribucion: list[TramoDistribucion]


class FilaIntermitencia(BaseModel):
    """Una serie artículo × tienda en la tabla de intermitencia."""

    model_config = ConfigDict(extra="forbid")
    item_id: str
    cat_id: str | None = None
    dept_id: str | None = None
    store_id: str
    dias_validos: Annotated[int, Field(ge=0)]
    dias_positivos: Annotated[int, Field(ge=0)]
    dias_cero: Annotated[int, Field(ge=0)]
    pct_cero: Porcentaje | None = None
    racha_maxima_ceros: Annotated[int, Field(ge=0)]
    unidades: Cantidad
    primera_venta_positiva: date | None = None
    ultima_venta_positiva: date | None = None


class Paginacion(BaseModel):
    """Paginación de la tabla."""

    model_config = ConfigDict(extra="forbid")
    pagina: Annotated[int, Field(ge=1)]
    tamano: Annotated[int, Field(ge=1)]
    total: Annotated[int, Field(ge=0)]
    paginas: Annotated[int, Field(ge=0)]
    orden: str


class IntermitenciaRespuesta(BaseModel):
    """Intermitencia de las series artículo × tienda de una tienda."""

    model_config = ConfigDict(extra="forbid")
    version: str
    tienda: str
    unidad: str
    periodo: PeriodoInclusivo
    resumen: ResumenIntermitencia
    filas: list[FilaIntermitencia]
    paginacion: Paginacion
    reglas: str
    calculo_ms: float | None = None


# --------------------------------------------------------------------------- #
# Demanda futura: matriz de desempeño por corte y horizonte
# --------------------------------------------------------------------------- #


class CeldaHorizonte(BaseModel):
    """Desempeño de un horizonte dentro de un origen de evaluación."""

    model_config = ConfigDict(extra="forbid")
    horizonte: Annotated[int, Field(ge=1, le=90)]
    fecha: date | None = None
    n: Annotated[int, Field(ge=0)]
    denominador: Annotated[int, Field(ge=0)]
    mae_candidato: float | None = None
    mae_baseline: float | None = None
    ventaja: float | None = Field(
        default=None, description="MAE_baseline − MAE_candidato; positivo = candidato mejor."
    )
    sesgo_candidato: float | None = None
    sesgo_baseline: float | None = None
    motivo: str | None = None


class FilaOrigenHorizontes(BaseModel):
    """Un origen de evaluación completo (28 celdas)."""

    model_config = ConfigDict(extra="forbid")
    origen_d: Annotated[int, Field(ge=1)]
    origen_fecha: date
    fin_fecha: date | None = None
    etapa: str
    series: Annotated[int, Field(ge=0)]
    celdas: list[CeldaHorizonte]


class EscalaMatrizDesempeno(BaseModel):
    """Dominio de las escalas divergentes de la matriz."""

    model_config = ConfigDict(extra="forbid")
    max_abs_ventaja: float
    max_abs_sesgo: float
    nota: str


class MetricasVentana(BaseModel):
    """Métricas de una ventana para un método y un grano."""

    model_config = ConfigDict(extra="forbid")
    n: Annotated[int, Field(ge=0)]
    observaciones: Annotated[int, Field(ge=0)] = 0
    mae: float | None = None
    sesgo: float | None = None
    wape_pct: Porcentaje | None = None
    wape_razon: float | None = Field(
        default=None, description="Σ|error| ÷ Σobservado, sin multiplicar por 100."
    )
    denominador: Annotated[int, Field(ge=0)] = 0
    wape_no_calculable: bool = False


class GranoVentana(BaseModel):
    """Comparación candidato frente a baseline en un grano declarado."""

    model_config = ConfigDict(extra="forbid")
    grano: str = Field(description="«detalle artículo × tienda» o «total del segmento».")
    candidato: MetricasVentana
    baseline: MetricasVentana
    diferencia_absoluta: float | None = Field(
        default=None, description="MAE_baseline − MAE_candidato, en unidades por serie."
    )
    mejora_relativa_pct: Porcentaje | None = None
    motivo: str | None = None


class VentanaEvaluada(BaseModel):
    """Una ventana completa de 28 días evaluada, con los dos granos."""

    model_config = ConfigDict(extra="forbid")
    origen_d: Annotated[int, Field(ge=1)]
    origen_fecha: date
    fin_fecha: date
    etapa: str
    series: Annotated[int, Field(ge=0)]
    dias: Annotated[int, Field(ge=1)]
    detalle: GranoVentana
    agregado: GranoVentana
    cobertura: dict = Field(default_factory=dict)
    fallback: Annotated[int, Field(ge=0)] = 0


class SegmentoVentana(BaseModel):
    """Resultado de un segmento consolidado sobre las ventanas de la etapa."""

    model_config = ConfigDict(extra="forbid")
    nivel: str
    clave: str
    series: Annotated[int, Field(ge=0)]
    detalle: GranoVentana
    agregado: GranoVentana


class FilaAuditoriaTemporal(BaseModel):
    """Una evaluación concreta con la evidencia disponible sobre su separación temporal."""

    model_config = ConfigDict(extra="forbid")

    version: str | None = None
    run_id: str
    variante: str
    etapa: str
    artefacto: str | None = None
    origenes_entrenamiento: list[int] = Field(default_factory=list)
    horizonte: int
    corte: int
    fecha_corte: str | None = None
    inicio_horizonte: str | None = None
    fin_horizonte: str | None = None
    ultima_etiqueta_entrenamiento: int | None = None
    fecha_ultima_etiqueta: str | None = None
    dias_entre_etiqueta_y_corte: int | None = None
    entrenado: bool
    resultado: str = Field(
        description="separacion_verificada · contaminada · no_verificable"
    )
    detalle: str
    uso_del_corte: str
    early_stopping: str
    seleccion_de_hiperparametros: str
    transformaciones: str
    fuentes: dict[str, str] = Field(default_factory=dict)
    series_evaluadas: int = 0


class AuditoriaTemporalRespuesta(BaseModel):
    """Auditoría reproducible de la separación entre entrenamiento y evaluación."""

    model_config = ConfigDict(extra="forbid")

    version: str
    esquema: str
    generado_en: str
    disponible: bool = True
    resumen: dict[str, int] = Field(default_factory=dict)
    procedencia_metricas_desarrollo: dict | None = None
    filas: list[FilaAuditoriaTemporal] = Field(default_factory=list)
    nota: str | None = None
    motivo: str | None = None


class VentanasEvaluacionRespuesta(BaseModel):
    """Tabla completa de evaluación del modelo publicado, por ventana y segmento."""

    model_config = ConfigDict(extra="forbid")
    version: str
    #: Revisión publicada y política de precisión con la que se calcularon estas cifras.
    revision: str | None = None
    politica_precision: str | None = None
    run_id: str
    variante: str | None = None
    etapa: str
    horizonte: Annotated[int, Field(ge=1)]
    granos: list[str]
    formulas: dict
    advertencias: list[str] = Field(default_factory=list)
    ventanas: list[VentanaEvaluada]
    #: Orígenes con predicciones por día publicadas (los que la gráfica puede dibujar).
    origenes_graficables: list[int] = Field(default_factory=list)
    consolidado: dict = Field(default_factory=dict)
    por_estado: list[SegmentoVentana] = Field(default_factory=list)
    por_categoria: list[SegmentoVentana] = Field(default_factory=list)
    reconciliacion: dict = Field(default_factory=dict)
    cobertura_modelo: CoberturaModelo | None = None
    modelo: ModeloResumen | None = None
    calculo_ms: float | None = None


class MatrizDesempenoRespuesta(BaseModel):
    """Matriz de desempeño por corte y horizonte del modelo frente al baseline."""

    model_config = ConfigDict(extra="forbid")
    version: str
    run_id: str
    variante: str | None = None
    horizonte: Annotated[int, Field(ge=1)]
    etapa: str
    nivel: str
    origenes: list[FilaOrigenHorizontes]
    escala: EscalaMatrizDesempeno
    formulas: dict
    cobertura: CoberturaModelo | None = None
    modelo: ModeloResumen | None = None
    calculo_ms: float | None = None


# --------------------------------------------------------------------------- #
# Precios (distribución, cambios y evolución) — métrica separada de las unidades
# --------------------------------------------------------------------------- #


class PeriodoPrecios(BaseModel):
    """Periodo en el que se calculan los precios."""

    model_config = ConfigDict(extra="forbid")
    desde: date
    hasta: date
    dias: Annotated[int, Field(ge=1)]


class CajaPrecio(BaseModel):
    """Estadísticas de precio medio por artículo para una categoría."""

    model_config = ConfigDict(extra="forbid")
    categoria: str
    etiqueta: str
    n: Annotated[int, Field(ge=0)]
    min: float | None = None
    q1: float | None = None
    mediana: float | None = None
    q3: float | None = None
    max: float | None = None


class CuartilesPrecio(BaseModel):
    """Cuartiles del precio medio por artículo × tienda."""

    model_config = ConfigDict(extra="forbid")
    min: float | None = None
    q1: float | None = None
    mediana: float | None = None
    q3: float | None = None
    max: float | None = None


class ValorPrecio(BaseModel):
    """Precio medio ponderado por días cubiertos de un artículo × tienda."""

    model_config = ConfigDict(extra="forbid")
    item_id: str
    cat_id: str | None = None
    dept_id: str | None = None
    precio_medio: float | None = Field(
        default=None, description="USD por unidad; null si no hubo precio en el periodo."
    )
    semanas_con_precio: Annotated[int, Field(ge=0)]
    dias_cubiertos: Annotated[int, Field(ge=0)]


class CoberturaPrecios(BaseModel):
    """Desglose mutuamente excluyente de la cobertura de precio por combinación.

    ``completa + parcial + sin_precio`` suma el universo. ``pct_con_algun_precio`` cuenta las
    combinaciones con al menos un precio y **no** es cobertura de días: para eso está
    ``pct_dias_cubiertos``.
    """

    model_config = ConfigDict(extra="forbid")
    completa: Annotated[int, Field(ge=0)] = Field(
        description="Combinaciones con precio en todos los días del intervalo."
    )
    parcial: Annotated[int, Field(ge=0)]
    sin_precio: Annotated[int, Field(ge=0)]
    pct_completa: Porcentaje | None = None
    pct_con_algun_precio: Porcentaje | None = None
    dias_del_intervalo: Annotated[int, Field(ge=0)] = 0
    dias_cubiertos_total: Annotated[int, Field(ge=0)] = 0
    dias_posibles_total: Annotated[int, Field(ge=0)] = 0
    pct_dias_cubiertos: Porcentaje | None = None
    dias_cubiertos_mediana: Annotated[int, Field(ge=0)] = 0
    nota: str = ""


class DistribucionPreciosRespuesta(BaseModel):
    """Distribución del precio medio por artículo × tienda en una tienda."""

    model_config = ConfigDict(extra="forbid")
    unidad: str
    tienda: str
    periodo: PeriodoPrecios
    regla_ponderacion: str
    combinaciones_incluidas: Annotated[int, Field(ge=0)]
    articulos: Annotated[int, Field(ge=0)]
    cobertura: CoberturaPrecios
    cuartiles: CuartilesPrecio | None = None
    media: float | None = None
    cajas: list[CajaPrecio] = Field(default_factory=list)
    valores: list[ValorPrecio] = Field(default_factory=list)
    sin_precio: dict = Field(
        default_factory=dict,
        description="Explicación de las combinaciones sin precio dentro del intervalo.",
    )
    motivo: str | None = None


class UmbralPrecio(BaseModel):
    """Umbral **comercial** opcional declarado (no es una tolerancia técnica).

    Las diferencias se calculan en centavos enteros; ``valor_usd`` es 0 por omisión, así que
    un cambio de un centavo cuenta.
    """

    model_config = ConfigDict(extra="forbid")
    valor_usd: float = Field(ge=0)
    tipo: str
    precision: str = ""
    nota: str = ""


class TotalesCambiosPrecio(BaseModel):
    """Conteo de transiciones de precio del contexto."""

    model_config = ConfigDict(extra="forbid")
    transiciones: Annotated[int, Field(ge=0)]
    aumentos: Annotated[int, Field(ge=0)]
    disminuciones: Annotated[int, Field(ge=0)]
    sin_cambio: Annotated[int, Field(ge=0)]
    pct_aumentos_entre_cambios: Porcentaje | None = None
    motivo_pct: str | None = None
    pct_sin_cambio: Porcentaje | None = None


class CategoriaCambiosPrecio(BaseModel):
    """Transiciones de precio por categoría."""

    model_config = ConfigDict(extra="forbid")
    cat_id: str
    etiqueta: str
    transiciones: Annotated[int, Field(ge=0)]
    aumentos: Annotated[int, Field(ge=0)]
    disminuciones: Annotated[int, Field(ge=0)]
    sin_cambio: Annotated[int, Field(ge=0)]
    pct_aumentos_entre_cambios: Porcentaje | None = None


class SerieCambiosPrecio(BaseModel):
    """Transiciones de precio de una serie artículo × tienda."""

    model_config = ConfigDict(extra="forbid")
    item_id: str
    cat_id: str | None = None
    dept_id: str | None = None
    aumentos: Annotated[int, Field(ge=0)]
    disminuciones: Annotated[int, Field(ge=0)]
    sin_cambio: Annotated[int, Field(ge=0)]
    transiciones: Annotated[int, Field(ge=0)]
    semanas_con_precio: Annotated[int, Field(ge=0)]
    primera_semana: int | None = None
    ultima_semana: int | None = None


class CambiosPreciosRespuesta(BaseModel):
    """Frecuencia y dirección de los cambios de precio en una tienda."""

    model_config = ConfigDict(extra="forbid")
    unidad: str
    tienda: str
    periodo: PeriodoPrecios
    umbral: UmbralPrecio
    semanas: Annotated[int, Field(ge=0)]
    semanas_del_intervalo: Annotated[int, Field(ge=0)] = 0
    precision_comparacion: str = "centavos"
    series_con_precio: Annotated[int, Field(ge=0)]
    totales: TotalesCambiosPrecio
    por_categoria: list[CategoriaCambiosPrecio] = Field(default_factory=list)
    series: list[SerieCambiosPrecio] = Field(default_factory=list)
    nota: str


class CambiosSeriePrecio(BaseModel):
    """Conteo de cambios de una serie concreta."""

    model_config = ConfigDict(extra="forbid")
    aumentos: Annotated[int, Field(ge=0)]
    disminuciones: Annotated[int, Field(ge=0)]
    sin_cambio: Annotated[int, Field(ge=0)]
    transiciones: Annotated[int, Field(ge=0)]


class EvolucionPrecioRespuesta(BaseModel):
    """Serie semanal del precio de un artículo en una tienda."""

    model_config = ConfigDict(extra="forbid")
    unidad: str
    tienda: str
    item_id: str
    periodo: PeriodoPrecios
    puntos: list[PuntoPrecio] = Field(default_factory=list)
    semanas_con_precio: Annotated[int, Field(ge=0)]
    semanas_sin_precio: Annotated[int, Field(ge=0)]
    cambios: CambiosSeriePrecio
    umbral: UmbralPrecio
    motivo: str | None = None


