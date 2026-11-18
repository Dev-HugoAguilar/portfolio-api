"""Endpoints del proyecto «Atlas comercial · Demanda y ventas» (Walmart M5).

Convención de rutas del repositorio: la API vive bajo ``/api/v1``. Por eso los
endpoints quedan en ``/api/v1/comercial/m5/...`` (el encargo proponía
``/api/comercial/m5/...``; se respeta la convención existente y se documenta).

- ``GET /catalogo``      tiendas, estados, categorías, departamentos y productos.
- ``GET /productos``     búsqueda paginada de productos del departamento en contexto.
- ``GET /pulso``         resumen, series (nivel tienda, departamento o producto), A/B
                         y concentración.
- ``GET /pronosticos``   baseline desde la última fecha disponible (28 días).
- ``GET /evaluacion``    backtesting fuera de muestra con MAE, sesgo y WAPE.
- ``GET /estado``        versión publicada, cobertura, derivado y capacidades.
- ``GET /inventario``    documentos, tamaño de datos e índices (para Atlas).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import ComercialServicioDep
from app.comercial.consultas import FiltroInvalido
from app.models.comercial import (
    Agregacion,
    ArticuloRespuesta,
    CambiosPreciosRespuesta,
    CatalogoRespuesta,
    ComparacionTiendaCategoriaRespuesta,
    DimensionEstacionalidad,
    DistribucionPreciosRespuesta,
    EstacionalidadRespuesta,
    EstadoRespuesta,
    EvaluacionRespuesta,
    EvolucionPrecioRespuesta,
    HistoricoRespuesta,
    IntermitenciaRespuesta,
    MatrizDesempenoRespuesta,
    MatrizTemporalRespuesta,
    ModelosRespuesta,
    ModoHistorico,
    DetallePlanRespuesta,
    EvaluacionHorizonteRespuesta,
    NivelExploracion,
    NivelPronostico,
    PlanificacionRespuesta,
    ProductosRespuesta,
    PronosticosRespuesta,
    PulsoRespuesta,
    AuditoriaTemporalRespuesta,
    VentanasEvaluacionRespuesta,
)
from app.services.comercial_service import DatosPendientes

router = APIRouter(prefix="/comercial/m5", tags=["Atlas comercial · Demanda y ventas (M5)"])

_NOTA_FUENTE = (
    "Fuente: «M5 Forecasting - Accuracy» (Walmart, competición de Kaggle). Datos "
    "históricos de Estados Unidos (CA, TX, WI) importados a MongoDB local. "
    "Este proyecto no está vinculado ni patrocinado por Walmart."
)


def _pendiente(detalle: str) -> HTTPException:
    """503 explícito: faltan datos, no hay cifras que devolver."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "estado": "pendiente",
            "mensaje": "Datos pendientes de importar",
            "detalle": detalle,
            "accion": (
                "Descarga sales_train_evaluation.csv y calendar.csv desde "
                "https://www.kaggle.com/competitions/m5-forecasting-accuracy/data en "
                "backend/data/m5 y ejecuta: python -m scripts.importar_m5"
            ),
        },
    )


def _filtrar_error(exc: FiltroInvalido) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"estado": "filtro_invalido", "mensaje": str(exc)},
    )


def _cabeceras(respuesta: Response, servicio, cache: str) -> None:
    respuesta.headers["X-Comercial-Cache"] = cache
    respuesta.headers["X-Comercial-Ms"] = str(getattr(servicio, "ultimo_calculo_ms", 0))


@router.get(
    "/estado",
    response_model=EstadoRespuesta,
    summary="Versión publicada, cobertura y estado de los datos",
    description=(
        "Si no hay importación publicada devuelve `con_datos: false` y el estado "
        "«pendiente», con la acción mínima para cargar los archivos. No inventa cifras."
    ),
)
def estado(servicio: ComercialServicioDep, respuesta: Response) -> EstadoRespuesta:
    _cabeceras(respuesta, servicio, "n/a")
    return servicio.estado()


@router.get(
    "/catalogo",
    response_model=CatalogoRespuesta,
    summary="Tiendas, estados, categorías, departamentos y productos",
    description=(
        "Catálogo importado. Los productos se paginan y se pueden buscar por "
        "identificador, categoría o departamento."
    ),
)
def catalogo(
    servicio: ComercialServicioDep,
    respuesta: Response,
    texto: Annotated[str | None, Query(max_length=60, description="Búsqueda por item_id, categoría o departamento.")] = None,
    limite: Annotated[int, Query(ge=1, le=200, description="Productos por página.")] = 50,
    desplazamiento: Annotated[int, Query(ge=0, description="Productos a saltar.")] = 0,
) -> CatalogoRespuesta:
    try:
        resultado = servicio.catalogo(texto=texto, limite=limite, desplazamiento=desplazamiento)
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    _cabeceras(respuesta, servicio, "n/a")
    return resultado


@router.get(
    "/pulso",
    response_model=PulsoRespuesta,
    summary="Pulso de ventas: resumen, series, A/B y concentración",
    description=(
        "Una sola respuesta coordinada para las tres gráficas de «Pulso de ventas». "
        "Unidades vendidas; no hay ingresos en esta entrega. Fechas inclusivas y "
        "semanas de lunes a domingo. Una selección inválida devuelve 422, nunca «todos».\n\n"
        "**Niveles** (`nivel`): `tienda` (por omisión) sirve una serie por tienda; "
        "`departamento` exige `tienda` y sirve los departamentos permitidos por los "
        "filtros; `producto` exige `tienda` y `departamento` y sirve hasta cinco "
        "productos (`productos`, o los cinco de más unidades si no se envían)."
    ),
)
def pulso(
    servicio: ComercialServicioDep,
    respuesta: Response,
    desde: Annotated[str | None, Query(description="Fecha inicial (AAAA-MM-DD), inclusiva.")] = None,
    hasta: Annotated[str | None, Query(description="Fecha final (AAAA-MM-DD), inclusiva.")] = None,
    agregacion: Agregacion = Agregacion.dia,
    tiendas: Annotated[list[str] | None, Query(description="Códigos de tienda (CA_1…WI_3).")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías: FOODS, HOBBIES, HOUSEHOLD.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos (dept_id).")] = None,
    productos: Annotated[list[str] | None, Query(description="Productos (item_id); máximo 5 en nivel producto.")] = None,
    a_desde: Annotated[str | None, Query(description="Inicio del intervalo A.")] = None,
    a_hasta: Annotated[str | None, Query(description="Fin del intervalo A.")] = None,
    b_desde: Annotated[str | None, Query(description="Inicio del intervalo B.")] = None,
    b_hasta: Annotated[str | None, Query(description="Fin del intervalo B.")] = None,
    nivel: Annotated[NivelExploracion, Query(description="Nivel de exploración.")] = NivelExploracion.tienda,
    tienda: Annotated[str | None, Query(description="Tienda en contexto (niveles departamento y producto).")] = None,
    estado: Annotated[str | None, Query(description="Estado en contexto o como filtro territorial (CA, TX, WI).")] = None,
    departamento: Annotated[str | None, Query(description="Departamento en contexto (nivel producto).")] = None,
) -> PulsoRespuesta:
    from datetime import date as _date

    def _fecha(valor: str | None, campo: str):
        if valor in (None, ""):
            return None
        try:
            return _date.fromisoformat(valor)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"estado": "filtro_invalido", "mensaje": f"Fecha no válida en «{campo}»: {valor}"},
            ) from exc

    try:
        resultado = servicio.pulso(
            desde=_fecha(desde, "desde"),
            hasta=_fecha(hasta, "hasta"),
            agregacion=agregacion,
            tiendas=[t for t in (tiendas or []) if t],
            categorias=[c for c in (categorias or []) if c],
            departamentos=[d for d in (departamentos or []) if d],
            productos=[p for p in (productos or []) if p],
            a_desde=_fecha(a_desde, "a_desde"),
            a_hasta=_fecha(a_hasta, "a_hasta"),
            b_desde=_fecha(b_desde, "b_desde"),
            b_hasta=_fecha(b_hasta, "b_hasta"),
            nivel=nivel,
            tienda=tienda,
            estado=estado,
            departamento=departamento,
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/productos",
    response_model=ProductosRespuesta,
    summary="Búsqueda paginada de productos del departamento y contexto vigentes",
    description=(
        "Explorador de productos: identificadores reales de M5, unidades del intervalo "
        "en la tienda en contexto, orden estable y paginación. `seleccion_inicial` "
        "devuelve los cinco productos con más unidades para no empezar en vacío."
    ),
)
def productos(
    servicio: ComercialServicioDep,
    respuesta: Response,
    tienda: Annotated[str | None, Query(description="Tienda en contexto (alternativa a `estado`).")] = None,
    estado: Annotated[str | None, Query(description="Estado en contexto: agrega sus tiendas.")] = None,
    tiendas: Annotated[list[str] | None, Query(description="Tiendas del contexto (alternativa explícita).")] = None,
    departamento: Annotated[str | None, Query(description="Departamento del que se listan productos.")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías permitidas.")] = None,
    desde: Annotated[str | None, Query(description="Fecha inicial (AAAA-MM-DD), inclusiva.")] = None,
    hasta: Annotated[str | None, Query(description="Fecha final (AAAA-MM-DD), inclusiva.")] = None,
    texto: Annotated[str | None, Query(max_length=40, description="Búsqueda por identificador de producto.")] = None,
    orden: Annotated[str, Query(pattern="^(unidades|item_id)$", description="Orden estable.")] = "unidades",
    limite: Annotated[int, Query(ge=1, le=100, description="Productos por página.")] = 20,
    desplazamiento: Annotated[int, Query(ge=0, description="Productos a saltar.")] = 0,
) -> ProductosRespuesta:
    from datetime import date as _date

    def _fecha(valor: str | None, campo: str):
        if valor in (None, ""):
            return None
        try:
            return _date.fromisoformat(valor)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"estado": "filtro_invalido", "mensaje": f"Fecha no válida en «{campo}»: {valor}"},
            ) from exc

    try:
        resultado = servicio.productos_contexto(
            tienda=tienda,
            estado=estado,
            tiendas=[t for t in (tiendas or []) if t],
            departamento=departamento,
            categorias=[c for c in (categorias or []) if c],
            desde=_fecha(desde, "desde"),
            hasta=_fecha(hasta, "hasta"),
            texto=texto,
            orden=orden,
            limite=limite,
            desplazamiento=desplazamiento,
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/historico",
    response_model=HistoricoRespuesta,
    summary="Comparación histórica: año analizado contra años anteriores completos",
    description=(
        "Comparación **descriptiva**: línea del año seleccionado, promedio de años anteriores "
        "con cobertura completa, rango observado mínimo–máximo y diferencias calculadas.\n\n"
        "No es un intervalo de confianza ni una predicción, y no sustituye a «Demanda futura». "
        "Solo usa años anteriores al analizado; los incompletos se excluyen y se declara el "
        "motivo. El detalle anual usa la cobertura completa del conjunto y no cambia el periodo "
        "global, la comparación A/B ni la concentración."
    ),
)
def historico(
    servicio: ComercialServicioDep,
    respuesta: Response,
    anio: Annotated[int, Query(ge=2000, le=2100, description="Año analizado.")],
    modo: Annotated[ModoHistorico, Query(description="Semana ISO o acumulado del año.")] = ModoHistorico.semana,
    tiendas: Annotated[list[str] | None, Query(description="Códigos de tienda del contexto.")] = None,
    estado: Annotated[str | None, Query(description="Estado del contexto (CA, TX, WI).")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías del contexto.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos del contexto.")] = None,
) -> HistoricoRespuesta:
    try:
        resultado = servicio.historico(
            anio=anio,
            modo=modo,
            tiendas=[t for t in (tiendas or []) if t],
            categorias=[c for c in (categorias or []) if c],
            departamentos=[d for d in (departamentos or []) if d],
            estado=estado,
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/pronosticos",
    response_model=PronosticosRespuesta,
    summary="Demanda futura: 28 días de pronóstico diario del modelo y del baseline",
    description=(
        "Pronóstico diario de unidades a 28 días del modelo global de árboles publicado, "
        "con el baseline estacional semanal en paralelo.\n\n"
        "El observado aparece **solo** cuando ya existe (cortes de evaluación histórica). "
        "No se dibujan intervalos de confianza: no se han calculado ni evaluado.\n\n"
        "La fecha de corte sale de las ventas observadas, no del calendario ni del reloj. "
        "Son unidades del conjunto histórico de M5, no ventas actuales."
    ),
)
def pronosticos(
    servicio: ComercialServicioDep,
    respuesta: Response,
    nivel: Annotated[NivelPronostico, Query(description="Nivel: total, estado, tienda, categoria, departamento, categoria_tienda, departamento_tienda o producto.")] = NivelPronostico.tienda,
    dias_historico: Annotated[int, Query(ge=7, le=180, description="Días de historia que acompañan al gráfico.")] = 90,
    run_id: Annotated[str | None, Query(description="Ejecución del modelo; por omisión la recomendada.")] = None,
    origen: Annotated[str | None, Query(description="«final» o un d_N con predicciones publicadas.")] = None,
    tienda: Annotated[str | None, Query(description="Tienda del contexto.")] = None,
    estado: Annotated[str | None, Query(description="Estado del contexto (CA, TX, WI).")] = None,
    categoria: Annotated[str | None, Query(description="Categoría del contexto.")] = None,
    departamento: Annotated[str | None, Query(description="Departamento del contexto.")] = None,
    item: Annotated[str | None, Query(description="Artículo (nivel producto).")] = None,
    items: Annotated[list[str] | None, Query(description="Hasta cinco artículos (nivel producto).")] = None,
) -> PronosticosRespuesta:
    try:
        resultado = servicio.pronosticos(
            nivel,
            dias_historico=dias_historico,
            run_id=run_id,
            origen=origen,
            tienda=tienda,
            estado=estado,
            categoria=categoria,
            departamento=departamento,
            item=item,
            items=[i for i in (items or []) if i],
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/evaluacion",
    response_model=EvaluacionRespuesta,
    summary="Evaluación del modelo frente al baseline (cortes reservados y desarrollo)",
    description=(
        "Compara candidato y baseline en las mismas series, fechas y filtros, a grano "
        "artículo × tienda, con resultados por horizonte (día 1 a 28) y por estado, tienda, "
        "categoría y departamento.\n\n"
        "Etapa `desarrollo` elige la variante; etapa `final` usa los cortes reservados "
        "(d_1885 y d_1913) que no participaron en la selección. El error de las series "
        "agregadas se calcula sumando predicción y observado antes de medir, y no es "
        "intercambiable con el error por serie."
    ),
)
def evaluacion(
    servicio: ComercialServicioDep,
    respuesta: Response,
    nivel: Annotated[NivelPronostico, Query(description="Nivel del informe de evaluación.")] = NivelPronostico.total,
    run_id: Annotated[str | None, Query(description="Ejecución del modelo; por omisión la recomendada.")] = None,
    etapa: Annotated[str, Query(pattern="^(desarrollo|final)$", description="Cortes de selección o reservados.")] = "final",
    clave: Annotated[str | None, Query(description="Segmento concreto (CA_1, FOODS, FOODS_1…).")] = None,
) -> EvaluacionRespuesta:
    try:
        resultado = servicio.evaluacion(nivel, run_id=run_id, etapa=etapa, clave=clave)
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/modelos",
    response_model=ModelosRespuesta,
    summary="Ejecuciones del modelo predictivo (metadatos, variables y métricas)",
    description=(
        "Lista las ejecuciones guardadas con su semilla, ventana de entrenamiento, variables, "
        "importancias por ganancia, supuestos, cobertura y métricas. No expone los modelos "
        "pesados (viven en disco, fuera de Git)."
    ),
)
def modelos(servicio: ComercialServicioDep, respuesta: Response) -> ModelosRespuesta:
    try:
        resultado = servicio.modelos()
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    _cabeceras(respuesta, servicio, "n/a")
    return resultado


@router.get(
    "/inventario",
    summary="Documentos, tamaño de datos e índices de las colecciones del proyecto",
    description=(
        "Se mide antes de cualquier migración a Atlas. Este endpoint no migra ni "
        "publica nada: solo informa del tamaño real."
    ),
)
def inventario(servicio: ComercialServicioDep):  # noqa: ANN201 - dict documentado
    return servicio.inventario()


# --------------------------------------------------------------------------- #
# Exploración descriptiva (territorio, catálogo, estacionalidad, calendario)
# --------------------------------------------------------------------------- #


@router.get(
    "/comparacion",
    response_model=ComparacionTiendaCategoriaRespuesta,
    summary="Comparación entre periodos por tienda × categoría",
    description=(
        "Compara los periodos A y B en cada combinación **real** tienda × categoría del "
        "contexto (California sin filtros comerciales: 4 tiendas × 3 categorías = 12 filas).\n\n"
        "Métricas: «unidades totales» y «promedio diario» (días con observación). La "
        "diferencia es B − A y la variación es relativa a A; si A = 0 la variación no es "
        "calculable y se devuelve el motivo. La suma de las filas se contrasta con el total "
        "del contexto en la propia respuesta.\n\n"
        "No es una estimación: cada fila sale de los agregados importados de esa tienda y esa "
        "categoría, y nunca se reparte el total estatal entre tiendas."
    ),
)
def comparacion(
    servicio: ComercialServicioDep,
    respuesta: Response,
    desde: Annotated[str | None, Query(description="Fecha inicial del periodo (AAAA-MM-DD).")] = None,
    hasta: Annotated[str | None, Query(description="Fecha final del periodo (AAAA-MM-DD).")] = None,
    a_desde: Annotated[str | None, Query(description="Inicio del periodo A.")] = None,
    a_hasta: Annotated[str | None, Query(description="Fin del periodo A.")] = None,
    b_desde: Annotated[str | None, Query(description="Inicio del periodo B.")] = None,
    b_hasta: Annotated[str | None, Query(description="Fin del periodo B.")] = None,
    tiendas: Annotated[list[str] | None, Query(description="Códigos de tienda del contexto.")] = None,
    estado: Annotated[str | None, Query(description="Estado del contexto (CA, TX, WI).")] = None,
    tienda: Annotated[str | None, Query(description="Acota el contexto a una tienda.")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías del contexto.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos del contexto.")] = None,
    productos: Annotated[list[str] | None, Query(description="Artículos seleccionados (máx. 5).")] = None,
) -> ComparacionTiendaCategoriaRespuesta:
    try:
        resultado = servicio.comparacion(
            desde=desde,
            hasta=hasta,
            a_desde=a_desde,
            a_hasta=a_hasta,
            b_desde=b_desde,
            b_hasta=b_hasta,
            tiendas=[t for t in (tiendas or []) if t],
            estado=estado,
            tienda=tienda,
            categorias=[c for c in (categorias or []) if c],
            departamentos=[d for d in (departamentos or []) if d],
            productos=[p for p in (productos or []) if p],
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/estacionalidad",
    response_model=EstacionalidadRespuesta,
    summary="Estacionalidad semanal (lunes a domingo) y mensual (enero a diciembre)",
    description=(
        "Perfil semanal por grupo (estado, tienda o categoría) con unidades, fechas con "
        "observación y promedio diario, y perfil mensual del contexto por año con días "
        "observados y esperados.\n\n"
        "Un día con cero ventas observado cuenta en el denominador; un día sin dato no. Los "
        "meses incompletos (enero de 2011, mayo de 2016) se marcan y no se comparan como si "
        "fueran completos. El patrón es observado, no un efecto causal."
    ),
)
def estacionalidad(
    servicio: ComercialServicioDep,
    respuesta: Response,
    dimension: Annotated[DimensionEstacionalidad, Query(description="Corte del perfil semanal.")] = DimensionEstacionalidad.estado,
    desde: Annotated[str | None, Query(description="Fecha inicial del periodo (AAAA-MM-DD).")] = None,
    hasta: Annotated[str | None, Query(description="Fecha final del periodo (AAAA-MM-DD).")] = None,
    tiendas: Annotated[list[str] | None, Query(description="Códigos de tienda del contexto.")] = None,
    estado: Annotated[str | None, Query(description="Estado del contexto (CA, TX, WI).")] = None,
    tienda: Annotated[str | None, Query(description="Acota el contexto a una tienda.")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías del contexto.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos del contexto.")] = None,
) -> EstacionalidadRespuesta:
    try:
        resultado = servicio.estacionalidad(
            dimension=dimension,
            desde=desde,
            hasta=hasta,
            tiendas=[t for t in (tiendas or []) if t],
            estado=estado,
            tienda=tienda,
            categorias=[c for c in (categorias or []) if c],
            departamentos=[d for d in (departamentos or []) if d],
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/matriz-temporal",
    response_model=MatrizTemporalRespuesta,
    summary="Matriz tienda × día de un año (unidades vendidas)",
    description=(
        "Una fila por tienda del contexto y una columna por día del año seleccionado, con "
        "unidades vendidas por celda.\n\n"
        "Distingue tres estados: cero observado, valor positivo y **sin dato** (fecha fuera "
        "de la cobertura publicada). La escala de color es común a las tiendas visibles y su "
        "dominio se declara para no recortar extremos en silencio."
    ),
)
def matriz_temporal(
    servicio: ComercialServicioDep,
    respuesta: Response,
    anio: Annotated[int, Query(ge=2000, le=2100, description="Año del calendario.")],
    desde: Annotated[str | None, Query(description="Fecha inicial del periodo (AAAA-MM-DD).")] = None,
    hasta: Annotated[str | None, Query(description="Fecha final del periodo (AAAA-MM-DD).")] = None,
    tiendas: Annotated[list[str] | None, Query(description="Códigos de tienda del contexto.")] = None,
    estado: Annotated[str | None, Query(description="Estado del contexto (CA, TX, WI).")] = None,
    tienda: Annotated[str | None, Query(description="Acota el contexto a una tienda.")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías del contexto.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos del contexto.")] = None,
) -> MatrizTemporalRespuesta:
    try:
        resultado = servicio.matriz_temporal(
            anio=anio,
            desde=desde,
            hasta=hasta,
            tiendas=[t for t in (tiendas or []) if t],
            estado=estado,
            tienda=tienda,
            categorias=[c for c in (categorias or []) if c],
            departamentos=[d for d in (departamentos or []) if d],
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/articulo",
    response_model=ArticuloRespuesta,
    summary="Detalle de artículo × tienda: ventas diarias, precio semanal y calendario",
    description=(
        "Ventas diarias observadas y precio semanal (trazo escalonado) del artículo en una "
        "tienda, con las capas opcionales de SNAP del estado y eventos del calendario, más el "
        "resumen de intermitencia de esa serie.\n\n"
        "Ventas y precio son métricas separadas: no se multiplican ni se convierten en "
        "ingresos. Las capas muestran coincidencias temporales, no efectos causales."
    ),
)
def articulo(
    servicio: ComercialServicioDep,
    respuesta: Response,
    item: Annotated[str, Query(min_length=3, max_length=40, description="item_id del catálogo.")],
    tienda: Annotated[str, Query(min_length=2, max_length=12, description="store_id del catálogo.")],
    desde: Annotated[str | None, Query(description="Fecha inicial del periodo (AAAA-MM-DD).")] = None,
    hasta: Annotated[str | None, Query(description="Fecha final del periodo (AAAA-MM-DD).")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías del contexto.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos del contexto.")] = None,
) -> ArticuloRespuesta:
    try:
        resultado = servicio.articulo(
            item=item,
            tienda=tienda,
            desde=desde,
            hasta=hasta,
            categorias=[c for c in (categorias or []) if c],
            departamentos=[d for d in (departamentos or []) if d],
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/intermitencia",
    response_model=IntermitenciaRespuesta,
    summary="Intermitencia (ceros observados) de las series artículo × tienda de una tienda",
    description=(
        "Días válidos, días con ventas positivas, días con cero, porcentaje de ceros, mayor "
        "racha de ceros y primera/última venta positiva observada, con tabla paginada.\n\n"
        "Un día sin dato no cuenta como cero y corta la racha. Se incluyen las series con "
        "ventas siempre cero. Las fechas no significan apertura, cierre, inventario ni rotura "
        "de stock."
    ),
)
def intermitencia(
    servicio: ComercialServicioDep,
    respuesta: Response,
    tienda: Annotated[str, Query(min_length=2, max_length=12, description="store_id del catálogo.")],
    desde: Annotated[str | None, Query(description="Fecha inicial del periodo (AAAA-MM-DD).")] = None,
    hasta: Annotated[str | None, Query(description="Fecha final del periodo (AAAA-MM-DD).")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías del contexto.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos del contexto.")] = None,
    pagina: Annotated[int, Query(ge=1, description="Página de la tabla.")] = 1,
    tamano: Annotated[int, Query(ge=1, le=200, description="Series por página.")] = 25,
    orden: Annotated[str, Query(pattern="^(ceros|unidades|racha)$", description="Orden de la tabla.")] = "ceros",
) -> IntermitenciaRespuesta:
    try:
        resultado = servicio.intermitencia(
            tienda=tienda,
            desde=desde,
            hasta=hasta,
            categorias=[c for c in (categorias or []) if c],
            departamentos=[d for d in (departamentos or []) if d],
            pagina=pagina,
            tamano=tamano,
            orden=orden,
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/matriz-horizontes",
    response_model=MatrizDesempenoRespuesta,
    summary="Demanda futura: desempeño por corte y horizonte (día 1 a 28)",
    description=(
        "Matriz origen × horizonte con dos lecturas: **ventaja** del candidato frente al "
        "baseline (MAE_baseline − MAE_candidato) y **sesgo** (media de predicción − "
        "observado) para candidato o baseline.\n\n"
        "Se calcula al grano artículo × tienda: el error se mide antes de agregar series. "
        "Candidato y baseline comparten series, fechas y filtros. Una celda sin "
        "observaciones se devuelve sin valor y con su motivo."
    ),
)
def matriz_horizontes(
    servicio: ComercialServicioDep,
    respuesta: Response,
    nivel: Annotated[NivelPronostico, Query(description="Nivel del análisis (solo etiqueta la vista).")] = NivelPronostico.total,
    etapa: Annotated[str, Query(pattern="^(desarrollo|final)$", description="Cortes de selección o reservados.")] = "final",
    run_id: Annotated[str | None, Query(description="Ejecución concreta; por omisión la recomendada.")] = None,
) -> MatrizDesempenoRespuesta:
    try:
        resultado = servicio.matriz_desempeno(nivel=nivel, etapa=etapa, run_id=run_id)
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "n/a")
    return resultado



# --------------------------------------------------------------------------- #
# Precios: distribución, cambios y evolución (métrica separada de las unidades)
# --------------------------------------------------------------------------- #


@router.get(
    "/auditoria-temporal",
    response_model=AuditoriaTemporalRespuesta,
    summary="Auditoría de separación temporal entre entrenamiento y evaluación",
    description=(
        "Evidencia reproducible por ejecución y corte: orígenes de entrenamiento, última fecha de "
        "etiqueta usada para ajustar (origen + horizonte), ventana evaluada, uso del corte y "
        "resultado de la comprobación. La genera «python -m scripts.auditar_separacion_temporal»; "
        "si no existe, la respuesta lo declara en lugar de inventar una conclusión."
    ),
)
def auditoria_temporal(
    servicio: ComercialServicioDep,
    respuesta: Response,
) -> AuditoriaTemporalRespuesta:
    try:
        resultado = servicio.auditoria_temporal()
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    _cabeceras(respuesta, servicio, "n/a")
    return resultado


@router.get(
    "/evaluacion-ventanas",
    response_model=VentanasEvaluacionRespuesta,
    summary="Evaluación completa por ventana de 28 días, en dos granos",
    description=(
        "Una fila por ventana completa de 28 días con MAE, sesgo y WAPE de candidato y "
        "baseline, diferencia absoluta y mejora relativa, en **dos granos**: detalle artículo "
        "× tienda y total del segmento (sumando predicción y observado del día antes de "
        "medir)."
        "Incluye el consolidado de la etapa (desde numeradores y denominadores), los resultados "
        "por estado y por categoría, la cobertura y el fallback declarados, y una comprobación "
        "de que el agregado publicado es la suma del detalle. El WAPE se publica como razón y "
        "como porcentaje; con denominador cero queda no calculable."
    ),
)
def evaluacion_ventanas(
    servicio: ComercialServicioDep,
    respuesta: Response,
    etapa: Annotated[str, Query(pattern="^(desarrollo|final)$", description="Cortes de selección o reservados.")] = "final",
    run_id: Annotated[str | None, Query(description="Ejecución concreta; por omisión la recomendada.")] = None,
) -> VentanasEvaluacionRespuesta:
    try:
        resultado = servicio.evaluacion_ventanas(etapa=etapa, run_id=run_id)
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "n/a")
    return resultado


@router.get(
    "/precios/distribucion",
    response_model=DistribucionPreciosRespuesta,
    summary="Distribución del precio medio por artículo × tienda",
    description=(
        "Una observación por artículo × tienda: **precio medio ponderado por los días "
        "cubiertos** dentro del periodo, nunca ponderado por unidades vendidas.\n\n"
        "Expone la regla de ponderación, las combinaciones incluidas, la cobertura de precio "
        "y los cuartiles (interpolación lineal). Un artículo sin precio en el periodo queda "
        "como ausencia, no como cero. No se calculan ingresos."
    ),
)
def precios_distribucion(
    servicio: ComercialServicioDep,
    respuesta: Response,
    tienda: Annotated[str, Query(min_length=2, max_length=12, description="store_id del catálogo.")],
    desde: Annotated[str | None, Query(description="Fecha inicial del periodo (AAAA-MM-DD).")] = None,
    hasta: Annotated[str | None, Query(description="Fecha final del periodo (AAAA-MM-DD).")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías del contexto.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos del contexto.")] = None,
    items: Annotated[list[str] | None, Query(description="Artículos concretos.")] = None,
) -> DistribucionPreciosRespuesta:
    try:
        resultado = servicio.precios_distribucion(
            tienda=tienda,
            desde=desde,
            hasta=hasta,
            categorias=[c for c in (categorias or []) if c],
            departamentos=[d for d in (departamentos or []) if d],
            items=[i for i in (items or []) if i],
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/precios/cambios",
    response_model=CambiosPreciosRespuesta,
    summary="Frecuencia y dirección de los cambios de precio",
    description=(
        "Compara semanas consecutivas **con precio disponible**: un hueco entre semanas no "
        "cuenta como transición observada. La tolerancia es una diferencia absoluta en USD "
        "y se declara en la respuesta.\n\n"
        "Cuenta aumentos, disminuciones y semanas sin cambio. Si no hay cambios, la "
        "proporción de aumentos entre cambios no es calculable y se devuelve el motivo. Una "
        "disminución de precio **no** es una promoción (M5 no trae campañas)."
    ),
)
def precios_cambios(
    servicio: ComercialServicioDep,
    respuesta: Response,
    tienda: Annotated[str, Query(min_length=2, max_length=12, description="store_id del catálogo.")],
    desde: Annotated[str | None, Query(description="Fecha inicial del periodo (AAAA-MM-DD).")] = None,
    hasta: Annotated[str | None, Query(description="Fecha final del periodo (AAAA-MM-DD).")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías del contexto.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos del contexto.")] = None,
) -> CambiosPreciosRespuesta:
    try:
        resultado = servicio.precios_cambios(
            tienda=tienda,
            desde=desde,
            hasta=hasta,
            categorias=[c for c in (categorias or []) if c],
            departamentos=[d for d in (departamentos or []) if d],
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado


@router.get(
    "/precios/evolucion",
    response_model=EvolucionPrecioRespuesta,
    summary="Evolución semanal del precio de un artículo en una tienda",
    description=(
        "Serie semanal del precio del artículo, solo con semanas que tienen precio dentro "
        "del periodo, con las fechas reales que cubre cada semana (la primera y la última "
        "pueden quedar parciales)."
    ),
)
def precios_evolucion(
    servicio: ComercialServicioDep,
    respuesta: Response,
    tienda: Annotated[str, Query(min_length=2, max_length=12, description="store_id del catálogo.")],
    item: Annotated[str, Query(min_length=3, max_length=40, description="item_id del catálogo.")],
    desde: Annotated[str | None, Query(description="Fecha inicial del periodo (AAAA-MM-DD).")] = None,
    hasta: Annotated[str | None, Query(description="Fecha final del periodo (AAAA-MM-DD).")] = None,
) -> EvolucionPrecioRespuesta:
    try:
        resultado = servicio.precios_evolucion(
            tienda=tienda, item_id=item, desde=desde, hasta=hasta
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "hit" if getattr(servicio, "ultimo_desde_cache", False) else "miss")
    return resultado

# --------------------------------------------------------------------------- #
# Planificación de ventas a cuatro semanas y evaluación al horizonte de 28 días
# --------------------------------------------------------------------------- #


@router.get(
    "/planificacion",
    response_model=PlanificacionRespuesta,
    summary="Plan de ventas a cuatro semanas por artículo × tienda",
    description=(
        "Una fila por serie artículo × tienda con lo observado en los 28 días anteriores al "
        "corte, el pronóstico del horizonte, la diferencia, la variación y las cuatro semanas "
        "(días 1–7, 8–14, 15–21 y 22–28). Paginación y ordenación en el servidor, con "
        "desempate estable por tienda y artículo. El resumen corresponde al conjunto filtrado "
        "completo, no a la página. Los órdenes porcentuales dejan fuera los registros sin base "
        "histórica y lo declaran."
    ),
)
def planificacion(
    servicio: ComercialServicioDep,
    respuesta: Response,
    tiendas: Annotated[list[str] | None, Query(description="Tiendas (store_id).")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías: FOODS, HOBBIES, HOUSEHOLD.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos (dept_id).")] = None,
    texto: Annotated[str | None, Query(max_length=60, description="Busca por artículo, categoría, departamento o tienda.")] = None,
    orden: Annotated[
        str,
        Query(
            pattern="^(volumen_previsto|volumen_observado|aumento_absoluto|disminucion_absoluta|aumento_porcentual|disminucion_porcentual)$",
            description="Orden aplicado al conjunto completo.",
        ),
    ] = "volumen_previsto",
    minimo_observado: Annotated[float | None, Query(ge=0, description="Volumen histórico mínimo para el ranking.")] = None,
    pagina: Annotated[int, Query(ge=1)] = 1,
    tamano: Annotated[int, Query(ge=1, le=200)] = 25,
    run_id: Annotated[str | None, Query(description="Ejecución concreta; por omisión la recomendada.")] = None,
) -> PlanificacionRespuesta:
    try:
        resultado = servicio.planificacion(
            tiendas=tiendas,
            categorias=categorias,
            departamentos=departamentos,
            texto=texto,
            orden=orden,
            minimo_observado=minimo_observado,
            pagina=pagina,
            tamano=tamano,
            run_id=run_id,
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "n/a")
    return resultado


@router.get(
    "/planificacion/serie",
    response_model=DetallePlanRespuesta,
    summary="Detalle de una serie artículo × tienda del plan",
    description=(
        "Histórico reciente, pronóstico diario de 28 días con su baseline, totales semanales y "
        "evaluación a 28 días de esa misma serie en cada corte disponible. Nunca devuelve "
        "métricas agregadas presentadas como si evaluaran esa serie."
    ),
)
def detalle_plan(
    servicio: ComercialServicioDep,
    respuesta: Response,
    tienda: Annotated[str, Query(description="Tienda (store_id).")],
    item: Annotated[str, Query(description="Artículo (item_id).")],
    run_id: Annotated[str | None, Query(description="Ejecución concreta; por omisión la recomendada.")] = None,
) -> DetallePlanRespuesta:
    try:
        resultado = servicio.detalle_plan(tienda=tienda, item=item, run_id=run_id)
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "n/a")
    return resultado


@router.get(
    "/planificacion/exportar",
    summary="Plan filtrado completo para descarga",
    description=(
        "Devuelve todas las filas del filtro (hasta 20 000) con las mismas cifras que la tabla, "
        "sin filas de totales que puedan sumarse como si fueran artículos. Declara si el "
        "conjunto se recortó."
    ),
)
def exportar_plan(
    servicio: ComercialServicioDep,
    respuesta: Response,
    tiendas: Annotated[list[str] | None, Query(description="Tiendas (store_id).")] = None,
    categorias: Annotated[list[str] | None, Query(description="Categorías.")] = None,
    departamentos: Annotated[list[str] | None, Query(description="Departamentos.")] = None,
    texto: Annotated[str | None, Query(max_length=60)] = None,
    orden: Annotated[
        str,
        Query(
            pattern="^(volumen_previsto|volumen_observado|aumento_absoluto|disminucion_absoluta|aumento_porcentual|disminucion_porcentual)$"
        ),
    ] = "volumen_previsto",
    minimo_observado: Annotated[float | None, Query(ge=0)] = None,
    run_id: Annotated[str | None, Query()] = None,
) -> dict:
    try:
        resultado = servicio.planificacion_completa(
            tiendas=tiendas,
            categorias=categorias,
            departamentos=departamentos,
            texto=texto,
            orden=orden,
            minimo_observado=minimo_observado,
            run_id=run_id,
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "n/a")
    return resultado


@router.get(
    "/evaluacion-horizonte",
    response_model=EvaluacionHorizonteRespuesta,
    summary="Evaluación del total de 28 días por serie y corte",
    description=(
        "Se suman primero el observado y el pronóstico del horizonte y solo después se "
        "restan: **no** es la suma de errores diarios. Publica MAE_28, WAPE_28 y sesgo por "
        "corte y por segmento, el estado descriptivo de cada serie con el número de cortes "
        "que lo sustentan, la cobertura de series evaluadas y los grupos de volumen e "
        "intermitencia calculados con información anterior al corte."
    ),
)
def evaluacion_horizonte(
    servicio: ComercialServicioDep,
    respuesta: Response,
    tienda: Annotated[str | None, Query(description="Tienda (store_id).")] = None,
    categoria: Annotated[str | None, Query(description="Categoría.")] = None,
    departamento: Annotated[str | None, Query(description="Departamento.")] = None,
    run_id: Annotated[str | None, Query(description="Ejecución concreta; por omisión la recomendada.")] = None,
) -> EvaluacionHorizonteRespuesta:
    try:
        resultado = servicio.evaluacion_horizonte(
            tienda=tienda, categoria=categoria, departamento=departamento, run_id=run_id
        )
    except DatosPendientes as exc:
        raise _pendiente(_NOTA_FUENTE) from exc
    except FiltroInvalido as exc:
        raise _filtrar_error(exc) from exc
    _cabeceras(respuesta, servicio, "n/a")
    return resultado
