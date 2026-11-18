"""Servicio del Atlas comercial: valida parámetros y arma las respuestas.

Una misma versión publicada y los mismos filtros alimentan las tres gráficas de
«Pulso de ventas»; el servicio devuelve una sola estructura coordinada y rechaza
combinaciones inválidas en lugar de convertirlas en «todos».
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import numpy as np

from app.comercial import consultas, derivados, modelo, planificacion
from app.comercial.consultas import FiltroInvalido
from app.comercial.esquema import Ventana
from app.models.comercial import (
    AuditoriaTemporalRespuesta,
    DetallePlanRespuesta,
    EvaluacionHorizonteRespuesta,
    PlanificacionRespuesta,
    AcumuladoInfo,
    Agregacion,
    AnioEstacional,
    AnotacionHistorica,
    ArticuloDetalle,
    ArticuloRespuesta,
    Capacidades,
    CambiosPreciosRespuesta,
    CambiosSeriePrecio,
    CapasCalendario,
    CatalogoRespuesta,
    CategoriaCambiosPrecio,
    CajaPrecio,
    CeldaHorizonte,
    Cobertura,
    CoberturaAnio,
    CoberturaMatriz,
    CoberturaModelo,
    CoberturaPrecios,
    ComparacionAB,
    ComparacionTiendaCategoriaRespuesta,
    Composicion,
    Concentracion,
    ConteosContexto,
    ContextoPulso,
    CuartilesPrecio,
    DerivadoInfo,
    DiaMatriz,
    DimensionEstacionalidad,
    DistribucionPreciosRespuesta,
    Dominio,
    EjecucionModelo,
    GranoVentana,
    MetricasVentana,
    EscalaMatriz,
    EscalaMatrizDesempeno,
    EstacionalidadRespuesta,
    EstadoDatos,
    EstadoRespuesta,
    EvaluacionRespuesta,
    EvolucionPrecioRespuesta,
    FilaComparacionTiendaCategoria,
    FilaIntermitencia,
    FilaMatrizTemporal,
    FilaOrigenHorizontes,
    FiltrosHistoricos,
    GrupoSemanal,
    GrupoTiendaAB,
    HistoricoRespuesta,
    InfoPrecios,
    IntermitenciaRespuesta,
    IntermitenciaSerie,
    MarcaMes,
    MatrizDesempenoRespuesta,
    MatrizTemporalRespuesta,
    MensualInfo,
    MesEstacional,
    MetricaAB,
    MetricaFila,
    MigaNavegacion,
    ModeloResumen,
    ModelosRespuesta,
    ModoHistorico,
    NivelExploracion,
    NivelPronostico,
    Paginacion,
    Panorama,
    ParametrosPulso,
    PeriodoInclusivo,
    PeriodoPrecios,
    Producto,
    ProductoConUnidades,
    ProductosRespuesta,
    PronosticosRespuesta,
    PulsoRespuesta,
    PuntoHistorico,
    PuntoHorizonte,
    PuntoPrecio,
    PuntoSemanal,
    PuntoVentaDiaria,
    ReconciliacionAB,
    ReferenciasHistoricas,
    ResumenFilasAB,
    ResumenIntermitencia,
    ResumenPulso,
    SegmentoEvaluacion,
    SegmentoVentana,
    SemanalInfo,
    SerieCambiosPrecio,
    SeriePronostico,
    SerieTienda,
    Tienda,
    UmbralPrecio,
    TotalesCambiosPrecio,
    TramoDistribucion,
    ValorPrecio,
    VentanaEvaluada,
    VentanasEvaluacionRespuesta,
    VentanaEvaluacion,
)
from app.repositories.comercial_repository import ComercialRepositorio

CAPACIDADES = Capacidades(
    agregaciones=[Agregacion.dia, Agregacion.semana, Agregacion.mes],
    filtros=["tiendas", "categorias", "departamentos", "productos", "desde", "hasta", "a/b"],
    niveles_pronostico=[NivelPronostico.total, NivelPronostico.tienda, NivelPronostico.categoria],
    niveles_exploracion=[
        NivelExploracion.tienda,
        NivelExploracion.departamento,
        NivelExploracion.producto,
    ],
    derivado_departamento_tienda=True,
    detalle_producto_tienda=True,
    ingresos=False,
)

NOTA_PULSO = (
    "Unidades vendidas (no ingresos). Datos históricos de Estados Unidos del "
    "conjunto Walmart M5; las tiendas se identifican por su código original "
    "(CA_1…WI_3) y no se renombran como ciudades. Los días sin observación se "
    "devuelven como nulos; no se imputan."
)

LIMITACIONES_EVALUACION = [
    "El modelo pronostica unidades vendidas del conjunto histórico de M5: no son ventas actuales ni ingresos.",
    "El error se mide a grano artículo × tienda; el MAE por serie-día es bajo porque la mayoría de las celdas no vende, y el WAPE es alto por la misma razón.",
    "No se declara WRMSSE oficial de M5: la jerarquía completa, el escalamiento y las ponderaciones no están implementados.",
    "No se publican intervalos de predicción: no se han calculado ni evaluado.",
    "El precio de los 28 días pronosticados es el último precio conocido al origen (supuesto explícito), no el precio realizado.",
    "Las ventanas se separan un horizonte completo: no es una evaluación rolling con actualización diaria.",
    "Los cortes finales (d_1885 y d_1913) se reservaron y no participaron en la elección de la variante.",
]

DEFINICIONES_EVALUACION = {
    "mae": "media de |pronóstico − observado| por serie-día (unidades)",
    "sesgo": "media de (pronóstico − observado); positivo = sobreestimación",
    "wape": "100 × Σ|error| / Σobservado; no calculable si Σobservado = 0",
    "grano": "errores al grano artículo × tienda; los agregados se calculan sumando predicción y observado antes de medir",
    "mejora_pct": "100 × (WAPE_baseline − WAPE_candidato) / WAPE_baseline; positivo = menos error",
    "horizonte": "día 1 a 28 dentro de cada ventana de 28 días",
    "etapas": "desarrollo elige la variante; final (reservado) solo se reporta",
}


class DatosPendientes(RuntimeError):
    """No hay versión publicada: la API lo dice en lugar de inventar cifras."""

    def __init__(self) -> None:
        super().__init__("Datos pendientes de importar")


class ComercialServicio:
    def __init__(self, repositorio: ComercialRepositorio) -> None:
        self._repo = repositorio
        self.ultimo_calculo_ms = 0
        self.ultimo_desde_cache = False

    # -- estado --------------------------------------------------------------
    def estado(self) -> EstadoRespuesta:
        datos = self._repo.estado()
        cobertura = datos.get("cobertura")
        version = datos.get("version")
        return EstadoRespuesta(
            version=version,
            con_datos=bool(datos.get("con_datos")),
            estado=EstadoDatos(datos.get("estado") or ("pendiente" if not datos.get("con_datos") else "publicada")),
            mensaje=datos.get("mensaje") or datos.get("detalle"),
            cobertura=Cobertura(**cobertura) if cobertura else None,
            importada_en=datos.get("importada_en"),
            unidad=datos.get("unidad"),
            esquema=datos.get("esquema"),
            destino=datos.get("destino"),
            base=datos.get("base"),
            calendario=datos.get("calendario"),
            derivado=derivados.info(self._repo.db, version) if version else None,
        )

    def _version(self) -> str:
        version = self._repo.version()
        if not version:
            raise DatosPendientes()
        return version

    # -- catálogo ------------------------------------------------------------
    def catalogo(self, *, texto: str | None, limite: int, desplazamiento: int) -> CatalogoRespuesta:
        version = self._version()
        cal = self._repo.catalogo(version)
        cobertura = self._repo.cobertura(version)
        productos, total = self._repo.productos(
            version, texto=texto, limite=limite, desplazamiento=desplazamiento
        )
        return CatalogoRespuesta(
            version=version,
            tiendas=[Tienda(**t) for t in cal["tiendas"]],
            estados=cal["estados"],
            categorias=cal["categorias"],
            departamentos=cal["departamentos"],
            productos_total=total,
            productos=[Producto(**p) for p in productos],
            paginacion={
                "limite": limite,
                "desplazamiento": desplazamiento,
                "total": total,
                "hay_mas": desplazamiento + len(productos) < total,
            },
            cobertura=Cobertura(
                desde=cobertura["desde"], hasta=cobertura["hasta"], dias=cobertura["dias"]
            ),
            capacidades=CAPACIDADES.model_copy(
                update={"derivado_departamento_tienda": derivados.publicado(self._repo.db, version)}
            ),
            nota=(
                "Catálogo real importado del conjunto M5. Las tiendas son las del archivo "
                "(CA, TX, WI) con su estado; no hay ciudades mexicanas ni conversión de moneda."
            ),
        )

    # -- pulso ---------------------------------------------------------------
    def pulso(
        self,
        *,
        desde: date | None,
        hasta: date | None,
        agregacion: Agregacion,
        tiendas: list[str],
        categorias: list[str],
        departamentos: list[str],
        productos: list[str],
        a_desde: date | None,
        a_hasta: date | None,
        b_desde: date | None,
        b_hasta: date | None,
        nivel: NivelExploracion | None = None,
        tienda: str | None = None,
        estado: str | None = None,
        departamento: str | None = None,
    ) -> PulsoRespuesta:
        version = self._version()
        filtros = consultas.resolver_filtros(
            self._repo.db,
            version,
            {
                "desde": desde,
                "hasta": hasta,
                "tiendas": tiendas,
                "categorias": categorias,
                "departamentos": departamentos,
                "productos": productos,
                "a_desde": a_desde,
                "a_hasta": a_hasta,
                "b_desde": b_desde,
                "b_hasta": b_hasta,
                "nivel": nivel.value if nivel else None,
                "tienda": tienda,
                "estado": estado,
                "departamento": departamento,
            },
        )
        datos = self._repo.pulso(version, filtros, agregacion.value)
        self.ultimo_calculo_ms = self._repo.ultimo_calculo_ms
        self.ultimo_desde_cache = self._repo.ultimo_desde_cache
        ventana: Ventana = filtros["ventana"]
        series = datos["series"]
        nivel_activo = NivelExploracion(filtros["nivel"])
        derivado = _derivado(datos.get("derivado"), version) if nivel_activo != NivelExploracion.tienda else None
        cobertura = consultas.cobertura_publicada(self._repo.db, version)
        return PulsoRespuesta(
            version=version,
            version_derivado=derivado.huella if derivado else None,
            generado_en=datetime.now(timezone.utc),
            nivel=nivel_activo,
            parametros=ParametrosPulso(
                desde=ventana.desde,
                hasta=ventana.hasta,
                agregacion=agregacion,
                tiendas=filtros["tiendas"],
                categorias=filtros["categorias"],
                departamentos=filtros["departamentos"],
                productos=filtros["productos"],
                nivel=nivel_activo,
                tienda=filtros.get("tienda"),
                estado=filtros.get("estado"),
                departamento=filtros.get("departamento"),
                a=_cobertura(filtros["a"]),
                b=_cobertura(filtros["b"]),
                ab_por_omision=bool(filtros.get("ab_por_omision")),
            ),
            contexto=_contexto(filtros, datos["resumen"]),
            migas=_migas(filtros),
            derivado=derivado,
            cobertura=Cobertura(
                desde=cobertura["desde"], hasta=cobertura["hasta"], dias=cobertura["dias"]
            ),
            resumen=ResumenPulso(
                total=datos["resumen"]["total"],
                nivel=nivel_activo,
                series=datos["resumen"]["series"],
                tiendas=datos["resumen"]["tiendas"],
                total_contexto=datos["resumen"]["total_contexto"],
                total_seleccion=datos["resumen"]["total_seleccion"],
                participacion_pct=datos["resumen"]["participacion_pct"],
                dias=datos["resumen"]["dias"],
                promedio_diario=datos["resumen"]["promedio_diario"],
                unidad=datos["resumen"]["unidad"],
                concentracion_total=datos["resumen"]["concentracion_total"],
                reconcilia_con_concentracion=datos["resumen"]["reconcilia_con_concentracion"],
            ),
            dominio=Dominio(
                desde=ventana.desde,
                hasta=ventana.hasta,
                dias=ventana.dias,
                agregacion=agregacion,
            ),
            series=[SerieTienda(**serie) for serie in series["series"]],
            comparacion=ComparacionAB(
                **{
                    **datos["comparacion"],
                    "no_causal": datos["comparacion"]["no_causal"]
                    + (
                        " A y B se proponen como las dos mitades del intervalo porque no se "
                        "enviaron fechas explícitas."
                        if filtros.get("ab_por_omision")
                        else ""
                    ),
                }
            ),
            concentracion=Concentracion(**datos["concentracion"]),
            panorama=Panorama(**datos["panorama"]) if datos.get("panorama") else None,
            composicion=Composicion(**datos["composicion"]) if datos.get("composicion") else None,
            nota=NOTA_PULSO,
        )

    # -- comparación histórica ------------------------------------------------
    def historico(
        self,
        *,
        anio: int,
        modo: ModoHistorico,
        tiendas: list[str],
        categorias: list[str],
        departamentos: list[str],
        estado: str | None,
    ) -> HistoricoRespuesta:
        """Comparación descriptiva del año contra años anteriores completos.

        No toca el periodo global, ni A/B, ni la concentración: usa la cobertura
        publicada completa y lo declara en `filtros.alcance_temporal`.
        """
        version = self._version()
        filtros = consultas.resolver_filtros(
            self._repo.db,
            version,
            {
                "tiendas": tiendas,
                "categorias": categorias,
                "departamentos": departamentos,
                "estado": estado,
            },
        )
        datos = self._repo.historico(version, filtros, anio, modo.value)
        self.ultimo_calculo_ms = self._repo.ultimo_calculo_ms
        self.ultimo_desde_cache = self._repo.ultimo_desde_cache
        cobertura = consultas.cobertura_publicada(self._repo.db, version)
        referencias = datos["referencias"]
        return HistoricoRespuesta(
            version=version,
            generado_en=datetime.now(timezone.utc),
            anio=datos["anio"],
            anios_disponibles=datos["anios_disponibles"],
            modo=modo,
            etiqueta_modo=datos["etiqueta_modo"],
            regla_alineacion=datos["regla_alineacion"],
            cobertura_anio=CoberturaAnio(**datos["cobertura_anio"]),
            referencias=ReferenciasHistoricas(**referencias),
            sin_referencia=datos["sin_referencia"],
            una_referencia=datos["una_referencia"],
            unidad=datos["unidad"],
            filtros=FiltrosHistoricos(
                tiendas=filtros["tiendas"],
                categorias=filtros["categorias"],
                departamentos=filtros["departamentos"],
                estado=filtros.get("estado"),
                tienda=filtros.get("tienda"),
                descripcion=_descripcion_contexto(filtros),
                periodo_global=Cobertura(
                    desde=cobertura["desde"], hasta=cobertura["hasta"], dias=cobertura["dias"]
                ),
                alcance_temporal=(
                    f"Año analizado {datos['anio']}"
                    + (
                        f" · referencia {', '.join(str(a) for a in referencias['anios_incluidos'])}"
                        if referencias["anios_incluidos"]
                        else " · sin referencia histórica comparable"
                    )
                    + ". El detalle anual usa la cobertura completa del conjunto y no modifica el "
                    "periodo global, la comparación A/B ni la concentración."
                ),
            ),
            puntos=[PuntoHistorico(**punto) for punto in datos["puntos"]],
            puntos_con_referencia=datos["puntos_con_referencia"],
            anotaciones=[AnotacionHistorica(**anotacion) for anotacion in datos["anotaciones"]],
            ultimo_con_dato=(
                PuntoHistorico(**datos["ultimo_con_dato"]) if datos.get("ultimo_con_dato") else None
            ),
            acumulado=AcumuladoInfo(**datos["acumulado"]) if datos.get("acumulado") else None,
            nota_banda=datos["nota_banda"],
            nota_alcance=datos["nota_alcance"],
        )

    # -- búsqueda de productos del explorador --------------------------------
    def productos_contexto(
        self,
        *,
        tienda: str | None,
        estado: str | None,
        tiendas: list[str],
        departamento: str | None,
        categorias: list[str],
        desde: date | None,
        hasta: date | None,
        texto: str | None,
        orden: str,
        limite: int,
        desplazamiento: int,
    ) -> ProductosRespuesta:
        version = self._version()
        filtros = consultas.resolver_filtros(
            self._repo.db,
            version,
            {
                "tiendas": tiendas or ([tienda] if tienda else []),
                "categorias": categorias,
                "desde": desde,
                "hasta": hasta,
                "estado": estado,
            },
        )
        if tienda and tienda not in filtros["tiendas"]:
            raise FiltroInvalido(
                f"La tienda {tienda} no pertenece al contexto territorial vigente "
                f"({', '.join(filtros['tiendas'])})."
            )
        catalogo = self._repo.catalogo(version)
        categorias_validas = {
            d["dept_id"]: d["cat_id"] for d in catalogo["departamentos"]
        }
        if departamento:
            if departamento not in categorias_validas:
                raise FiltroInvalido(f"Departamento inexistente: {departamento}")
            if categorias_validas[departamento] not in filtros["categorias"]:
                raise FiltroInvalido(
                    f"El departamento {departamento} pertenece a "
                    f"{categorias_validas[departamento]}, excluida por el filtro de categorías."
                )
        ventana = filtros["ventana"]
        datos = self._repo.productos_contexto(
            version,
            tiendas=filtros["tiendas"],
            departamento=departamento,
            categorias=filtros["categorias"],
            desde=ventana.desde,
            hasta=ventana.hasta,
            texto=texto,
            orden=orden,
            limite=limite,
            desplazamiento=desplazamiento,
        )
        self.ultimo_calculo_ms = self._repo.ultimo_calculo_ms
        self.ultimo_desde_cache = self._repo.ultimo_desde_cache
        cobertura = consultas.cobertura_publicada(self._repo.db, version)
        return ProductosRespuesta(
            version=version,
            contexto=datos["contexto"],
            orden=datos["orden"],
            total=datos["total"],
            productos=[ProductoConUnidades(**producto) for producto in datos["productos"]],
            universo=datos["universo"],
            seleccion_inicial=datos["seleccion_inicial"],
            seleccion_inicial_participacion_pct=datos["seleccion_inicial_participacion_pct"],
            paginacion={
                "limite": limite,
                "desplazamiento": desplazamiento,
                "total": datos["total"],
                "hay_mas": desplazamiento + len(datos["productos"]) < datos["total"],
            },
            cobertura=Cobertura(
                desde=cobertura["desde"], hasta=cobertura["hasta"], dias=cobertura["dias"]
            ),
            nota=(
                "Identificadores reales de M5 (item_id); no se inventan nombres comerciales. "
                "Las unidades corresponden al contexto territorial y al intervalo vigentes; la "
                "participación se calcula sobre el universo del departamento en ese contexto."
            ),
        )

    # -- pronósticos ---------------------------------------------------------
    def pronosticos(
        self,
        nivel: NivelPronostico,
        *,
        dias_historico: int = 90,
        run_id: str | None = None,
        origen: str | None = None,
        tienda: str | None = None,
        estado: str | None = None,
        categoria: str | None = None,
        departamento: str | None = None,
        item: str | None = None,
        items: list[str] | None = None,
    ) -> PronosticosRespuesta:
        """Pronóstico del modelo publicado (o del baseline si no hay ejecución).

        Con una ejecución publicada sirve los 28 días del candidato **y** del baseline,
        con el observado solo cuando ya existe (cortes de evaluación).
        """
        version = self._version()
        modelo_doc = (
            self._repo.ejecucion(version, run_id) if run_id else self._repo.ejecucion_recomendada(version)
        )
        if not modelo_doc:
            return self._pronosticos_base(version, nivel, dias_historico)

        seleccion = [i for i in (items or []) if i]
        if item and item not in seleccion:
            seleccion.append(item)
        if len(seleccion) > 5:
            raise FiltroInvalido("Como máximo se piden cinco artículos a la vez.")
        segmento = _resolver_segmento(
            nivel,
            tienda=tienda,
            estado=estado,
            categoria=categoria,
            departamento=departamento,
            item=seleccion[0] if seleccion else item,
            items=seleccion,
        )
        origen_d = _resolver_origen(self._repo, version, modelo_doc["run_id"], origen)
        if nivel == NivelPronostico.producto:
            return self._pronosticos_producto(
                version, modelo_doc, segmento, origen_d, dias_historico
            )

        predicciones = self._repo.predictores(
            version, modelo_doc["run_id"], origen=origen_d, dimension=segmento["dimension"],
            claves=segmento["claves"],
        )
        por_clave = {documento["clave"]: documento for documento in predicciones}
        claves = segmento["claves"] or sorted(por_clave)
        if not claves:
            raise FiltroInvalido(
                f"No hay predicciones publicadas para el nivel {nivel.value} en esta ejecución."
            )
        # Una clave pedida sin predicción publicada no se rellena con ceros: se avisa.
        faltantes = [clave for clave in claves if clave not in por_clave]
        if faltantes:
            raise FiltroInvalido(
                "Sin predicciones publicadas en esta ejecución para "
                + ", ".join(faltantes)
                + f" (nivel {nivel.value}, origen d_{origen_d})."
            )
        dias = predicciones[0]["dias"]
        # ¿Hay observación para las fechas del horizonte? Se decide con la cobertura real del
        # archivo (último día con ventas) y no con «el origen posterior al entrenamiento»: en
        # los cortes finales d_1885 y d_1913 también existen observaciones posteriores, y
        # declararlas ausentes ocultaba justo el tramo que la leyenda anunciaba.
        objetivo = [int(dia["d"]) for dia in dias]
        es_futuro = bool(objetivo) and max(objetivo) > _ultimo_dia_observado(self._repo, version)
        evaluacion = None
        if not es_futuro:
            evaluaciones = self._repo.evaluaciones_modelo(
                version, modelo_doc["run_id"], etapa="final", origen=origen_d
            )
            evaluacion = evaluaciones[0] if evaluaciones else None
        # ``historico_antes_de`` traduce la clave de la predicción a la del agregado
        # (``total`` → ``todas``, ``FOODS_1`` → ``FOODS|FOODS_1``) y devuelve las series
        # indexadas por la clave pedida.
        historico = consultas.historico_antes_de(
            self._repo.db,
            version,
            nivel=segmento["dimension"],
            claves=claves,
            origen_d=origen_d,
            dias=dias_historico,
        )
        historia_por_clave = {clave: historico.get(clave, []) for clave in claves}
        origen_fecha = consultas._fecha(self._repo.db["m5_calendario"].find_one({"d": origen_d})["fecha"])
        # ``observado`` = historia hasta el corte + los 28 días del horizonte: en un
        # corte de evaluación el observado posterior existe; en el pronóstico final
        # esas 28 posiciones son ``None`` (no hay observación y no se imputa).
        series = []
        for clave in claves:
            documento = por_clave.get(clave, {})
            posteriores = documento.get("observado")
            if posteriores is None:
                posteriores = [None] * int(modelo_doc["horizonte"])
            series.append(
                SeriePronostico(
                    clave=clave,
                    observado=list(historia_por_clave.get(clave, [])) + list(posteriores),
                    pronostico=list(documento.get("pronostico") or [0] * modelo_doc["horizonte"]),
                    baseline=list(documento.get("baseline") or [0] * modelo_doc["horizonte"]),
                    metodo="modelo",
                )
            )
        observado_futuro = None
        if evaluacion:
            observado_futuro = [
                documento.get("observado") for documento in predicciones if documento.get("observado")
            ]
        return PronosticosRespuesta(
            version=version,
            revision=(self._repo.publicacion() or {}).get("_id"),
            politica_precision=self._politica_precision(),
            generado_en=datetime.now(timezone.utc),
            nivel=nivel,
            metodo=modelo_doc["metodo"],
            unidad="unidades vendidas",
            origen_fecha=origen_fecha,
            origen_d=origen_d,
            horizonte=int(modelo_doc.get("horizonte", 28)),
            corte=f"Último día observado: {origen_fecha.isoformat()}",
            es_futuro=es_futuro,
            nota=(
                "Pronóstico del modelo publicado con información disponible hasta el corte. "
                "El baseline estacional se muestra para comparar; no se usan ventas futuras."
            ),
            dias=[{"d": int(dia["d"]), "fecha": dia["fecha"]} for dia in dias],
            historico_dias=consultas.dias_de_historico(origen_d, dias_historico),
            series=series,
            run_id=modelo_doc["run_id"],
            modelo=_modelo_resumen(modelo_doc),
            rango_objetivo=(
                Cobertura(
                    desde=date.fromisoformat(dias[0]["fecha"]),
                    hasta=date.fromisoformat(dias[-1]["fecha"]),
                    dias=len(dias),
                )
                if dias
                else None
            ),
            cobertura=CoberturaModelo(**(modelo_doc.get("cobertura") or {})),
            supuestos=supuestos_modelo(modelo_doc),
            estado_evaluacion="con_observaciones" if evaluacion else "sin_observaciones",
            metricas=_metricas(evaluacion["candidato"]) if evaluacion else None,
            baseline_metricas=_metricas(evaluacion["baseline"]) if evaluacion else None,
            filtros={
                "nivel": nivel.value,
                "dimension": segmento["dimension"],
                "claves": claves,
                "origen": origen or "final",
                "tienda": tienda,
                "estado": estado,
                "categoria": categoria,
                "departamento": departamento,
            },
        )

    def _politica_precision(self) -> str:
        """Política de precisión de la revisión publicada (texto único del proyecto)."""
        return planificacion.texto_politica(self._repo.publicacion())

    def _pronosticos_base(
        self, version: str, nivel: NivelPronostico, dias_historico: int
    ) -> PronosticosRespuesta:
        """Respaldo: baseline guardado por el generador anterior (sin ejecución de modelo)."""
        documento = self._repo.pronosticos(version, nivel.value)
        if not documento:
            raise DatosPendientes()
        origen = documento["origen_fecha"]
        origen_fecha = origen.date() if isinstance(origen, datetime) else origen
        claves = [detalle["clave"] for detalle in documento.get("detalle", [])]
        historico = consultas.historico_antes_de(
            self._repo.db,
            version,
            nivel=nivel.value,
            claves=claves,
            origen_d=int(documento["origen_d"]),
            dias=dias_historico,
        )
        dias_hist = consultas.dias_de_historico(int(documento["origen_d"]), dias_historico)
        return PronosticosRespuesta(
            version=version,
            revision=(self._repo.publicacion() or {}).get("_id"),
            politica_precision=self._politica_precision(),
            generado_en=datetime.now(timezone.utc),
            nivel=nivel,
            metodo=documento.get("metodo", "ingenuo estacional semanal"),
            unidad=documento.get("unidad", "unidades vendidas"),
            origen_fecha=origen_fecha,
            origen_d=int(documento["origen_d"]),
            horizonte=int(documento["horizonte"]),
            corte=f"Último día observado: {origen_fecha.isoformat()}",
            es_futuro=documento.get("tipo") == "futuro",
            nota=documento.get("nota", ""),
            dias=[{"d": int(dia["d"]), "fecha": dia["fecha"]} for dia in documento.get("dias", [])],
            historico_dias=dias_hist,
            series=[
                SeriePronostico(
                    clave=detalle["clave"],
                    observado=historico.get(detalle["clave"], []),
                    pronostico=detalle["pronostico"],
                    baseline=detalle["pronostico"],
                    metodo="estacional",
                )
                for detalle in documento.get("detalle", [])
            ],
        )

    def _pronosticos_producto(
        self,
        version: str,
        modelo_doc: dict,
        segmento: dict,
        origen_d: int,
        dias_historico: int,
    ) -> PronosticosRespuesta:
        """Series artículo × tienda de la ejecución publicada."""
        tienda = segmento["tienda"]
        items = segmento["items"]
        documentos = self._repo.predictores_series(
            version, modelo_doc["run_id"], tienda=tienda, items=items
        )
        if not documentos:
            raise FiltroInvalido(
                "El pronóstico artículo × tienda solo está publicado para el origen final de la "
                "ejecución recomendada; elige otro nivel para los cortes de evaluación."
            )
        dias = [
            {"d": int(documentos[0]["origen_d"]) + paso,
             "fecha": (consultas._fecha(self._repo.db["m5_calendario"].find_one({"d": int(documentos[0]["origen_d"])})["fecha"]) + timedelta(days=paso)).isoformat()}
            for paso in range(1, modelo_doc["horizonte"] + 1)
        ]
        dias_hist = consultas.dias_de_historico(origen_d, dias_historico)
        series = []
        for documento in documentos:
            vector = consultas._vector_bloques_contexto(
                self._repo.db, version, tiendas=[tienda], item=documento["item_id"], dias=dias_hist
            )
            series.append(
                SeriePronostico(
                    clave=documento["item_id"],
                    observado=[None if valor is None else int(valor) for valor in vector],
                    pronostico=documento["pronostico"],
                    baseline=documento["baseline"],
                    metodo=documento.get("metodo", "modelo"),
                )
            )
        origen_fecha = consultas._fecha(self._repo.db["m5_calendario"].find_one({"d": origen_d})["fecha"])
        return PronosticosRespuesta(
            version=version,
            revision=(self._repo.publicacion() or {}).get("_id"),
            politica_precision=self._politica_precision(),
            generado_en=datetime.now(timezone.utc),
            nivel=NivelPronostico.producto,
            metodo=modelo_doc["metodo"],
            unidad="unidades vendidas",
            origen_fecha=origen_fecha,
            origen_d=origen_d,
            horizonte=int(modelo_doc.get("horizonte", 28)),
            corte=f"Último día observado: {origen_fecha.isoformat()}",
            es_futuro=True,
            nota=(
                "Pronóstico artículo × tienda de la ejecución publicada. Son unidades vendidas "
                "del conjunto histórico de M5, no una previsión de ventas actuales."
            ),
            dias=dias,
            historico_dias=dias_hist,
            series=series,
            run_id=modelo_doc["run_id"],
            modelo=_modelo_resumen(modelo_doc),
            rango_objetivo=Cobertura(
                desde=date.fromisoformat(dias[0]["fecha"]),
                hasta=date.fromisoformat(dias[-1]["fecha"]),
                dias=len(dias),
            ),
            cobertura=CoberturaModelo(**(modelo_doc.get("cobertura") or {})),
            supuestos=supuestos_modelo(modelo_doc),
            estado_evaluacion="sin_observaciones",
            filtros={"nivel": "producto", "tienda": tienda, "items": items},
        )

    def evaluacion(
        self,
        nivel: NivelPronostico,
        *,
        run_id: str | None = None,
        etapa: str = "final",
        clave: str | None = None,
    ) -> EvaluacionRespuesta:
        """Evaluación del modelo publicado frente al baseline, con segmentos y horizontes."""
        version = self._version()
        modelo_doc = (
            self._repo.ejecucion(version, run_id) if run_id else self._repo.ejecucion_recomendada(version)
        )
        if not modelo_doc:
            return self._evaluacion_base(version, nivel)

        if etapa not in ("desarrollo", "final"):
            raise FiltroInvalido("La etapa debe ser «desarrollo» o «final».")
        if nivel == NivelPronostico.producto:
            raise FiltroInvalido(
                "La evaluación no se publica por artículo: se mide a grano artículo × tienda "
                "en el total y por estado, tienda, categoría y departamento."
            )
        evaluaciones = self._repo.evaluaciones_modelo(
            version, modelo_doc["run_id"], etapa=etapa
        )
        if not evaluaciones and etapa == "desarrollo":
            # La ejecución final no repite los cortes de desarrollo: se usan los de la
            # ejecución de desarrollo de la misma variante (misma configuración,
            # entrenada antes de los cortes reservados).
            for candidata in self._repo.ejecuciones(version):
                if candidata.get("variante") != modelo_doc.get("variante"):
                    continue
                if candidata.get("estado") != "desarrollo":
                    continue
                previas = self._repo.evaluaciones_modelo(
                    version, candidata["run_id"], etapa="desarrollo"
                )
                if previas:
                    evaluaciones = previas
                    modelo_doc = {**modelo_doc, "run_id": candidata["run_id"]}
                    break
        if not evaluaciones:
            raise DatosPendientes()

        segmentos = modelo.agrupar_segmentos(evaluaciones)
        if nivel == NivelPronostico.total:
            candidato = modelo.agregar_metricas_guardadas([e["candidato"] for e in evaluaciones])
            baseline = modelo.agregar_metricas_guardadas([e["baseline"] for e in evaluaciones])
            seleccionados = segmentos
        else:
            del_nivel = [s for s in segmentos if s["nivel"] == nivel.value]
            if clave:
                del_nivel = [s for s in del_nivel if s["clave"] == clave]
            if not del_nivel:
                raise FiltroInvalido(
                    f"Sin evaluaciones para el nivel {nivel.value}"
                    + (f" y la clave {clave}" if clave else "")
                )
            candidato = modelo.agregar_metricas_guardadas([s["candidato"] for s in del_nivel])
            baseline = modelo.agregar_metricas_guardadas([s["baseline"] for s in del_nivel])
            seleccionados = del_nivel
        mejoran = sum(1 for s in seleccionados if (s["mejora_pct"] or 0) > 0)
        empeoran = sum(1 for s in seleccionados if (s["mejora_pct"] or 0) < 0)
        sin_calculo = sum(1 for s in seleccionados if s["mejora_pct"] is None)
        por_horizonte = modelo.agrupar_horizontes(evaluaciones)
        ventanas = [
            VentanaEvaluacion(
                nivel=nivel,
                origen_d=int(evaluacion["origen_d"]),
                origen_fecha=evaluacion["origen_fecha"],
                fin_fecha=evaluacion["fin_fecha"],
                horizonte=int(modelo_doc.get("horizonte", 28)),
                series=int(evaluacion["series"]),
                metricas=_metricas(evaluacion["candidato"]),
                baseline=_metricas(evaluacion["baseline"]),
                mejora_pct=evaluacion.get("mejora_pct"),
            )
            for evaluacion in evaluaciones
        ]
        cobertura = modelo_doc.get("cobertura") or {}
        return EvaluacionRespuesta(
            version=version,
            nivel=nivel,
            metodo=modelo_doc["metodo"],
            unidad="unidades vendidas",
            horizonte=int(modelo_doc.get("horizonte", 28)),
            resumen=_metricas(candidato),
            definiciones=DEFINICIONES_EVALUACION,
            ventanas=ventanas,
            calculada_en=modelo_doc.get("creado_en"),
            limitaciones=LIMITACIONES_EVALUACION,
            run_id=modelo_doc["run_id"],
            variante=modelo_doc.get("variante"),
            etapa=etapa,
            baseline_resumen=_metricas(baseline),
            mejora_pct=modelo._mejora(candidato["wape"], baseline["wape"]),
            por_horizonte=[PuntoHorizonte(**punto) for punto in por_horizonte],
            segmentos=[SegmentoEvaluacion(**segmento) for segmento in seleccionados],
            mejoras={
                "segmentos_mejoran": mejoran,
                "segmentos_empeoran": empeoran,
                "segmentos_sin_mejora_calculable": sin_calculo,
                "nivel": nivel.value,
                "criterio": "Mejora del WAPE frente al baseline; positivo = menos error.",
            },
            cobertura=CoberturaModelo(**cobertura) if cobertura else None,
            modelo=_modelo_resumen(modelo_doc),
            supuestos=supuestos_modelo(modelo_doc),
        )

    def _evaluacion_base(self, version: str, nivel: NivelPronostico) -> EvaluacionRespuesta:
        documento = self._repo.evaluacion(version, nivel.value)
        if not documento:
            raise DatosPendientes()
        resumen = dict(documento["resumen"])
        resumen.setdefault("denominador", 0)
        return EvaluacionRespuesta(
            version=version,
            nivel=nivel,
            metodo=documento.get("metodo", "ingenuo estacional semanal"),
            unidad=documento.get("unidad", "unidades vendidas"),
            horizonte=int(documento.get("horizonte", 28)),
            resumen=_metricas(resumen),
            definiciones=documento.get("definiciones", {}),
            ventanas=[_ventana(v) for v in documento.get("ventanas", [])],
            calculada_en=documento.get("calculada_en"),
            limitaciones=LIMITACIONES_EVALUACION,
        )

    def modelos(self) -> ModelosRespuesta:
        """Ejecuciones del modelo guardadas (transparencia y trazabilidad)."""
        version = self._version()
        documentos = self._repo.ejecuciones(version)
        manifiesto = self._repo.manifiesto_precios()
        recomendado = next(
            (documento["run_id"] for documento in documentos if documento.get("recomendado")), None
        )
        return ModelosRespuesta(
            version=version,
            version_precios=(manifiesto or {}).get("version_precios"),
            recomendado=recomendado,
            ejecuciones=[
                EjecucionModelo(
                    run_id=documento["run_id"],
                    variante=documento.get("variante", "baseline"),
                    etiqueta=documento.get("etiqueta", ""),
                    metodo=documento.get("metodo", ""),
                    estado=documento.get("estado", ""),
                    recomendado=bool(documento.get("recomendado")),
                    version_precios=documento.get("version_precios"),
                    horizonte=int(documento.get("horizonte", 28)),
                    semilla=documento.get("semilla"),
                    origenes_entrenamiento=list(documento.get("origenes_entrenamiento") or []),
                    ventana_entrenamiento=documento.get("ventana_entrenamiento") or {},
                    variables=list(documento.get("variables") or []),
                    importancias=documento.get("importancias") or [],
                    supuestos=supuestos_modelo(documento),
                    cobertura=documento.get("cobertura") or {},
                    metricas_resumen=documento.get("metricas_resumen") or {},
                    duracion_s=documento.get("duracion_s"),
                    memoria=documento.get("memoria") or {},
                    entorno=documento.get("entorno") or {},
                    # El paquete de publicación guarda solo el nombre del archivo
                    # (`artefacto_nombre`): la ruta del equipo de entrenamiento no se publica.
                    artefacto=documento.get("artefacto") or documento.get("artefacto_nombre"),
                    creado_en=documento.get("creado_en"),
                )
                for documento in documentos
            ],
            nota=(
                "Ejecuciones guardadas en MongoDB local. Los modelos y las matrices pesadas "
                "viven en disco (backend/data/artefactos) y no se versionan. Una ejecución se "
                "marca «recomendada» solo después de evaluarse en los cortes reservados."
            ),
        )

    # -- inventario ----------------------------------------------------------
    def inventario(self) -> dict:
        """Documentos, datos e índices por colección (medición previa a Atlas)."""
        return self._repo.inventario()

    # -- exploración descriptiva ---------------------------------------------
    def _marcar_cache(self) -> None:
        """Copia el estado de caché del repositorio a esta respuesta."""
        self.ultimo_calculo_ms = self._repo.ultimo_calculo_ms
        self.ultimo_desde_cache = self._repo.ultimo_desde_cache

    def _ventana_descriptiva(
        self, version: str, desde: str | None, hasta: str | None
    ) -> tuple[date, date]:
        """Periodo de una vista descriptiva, con un dominio más amplio que la cobertura.

        Los análisis descriptivos (estacionalidad y matriz temporal) necesitan poder pedir
        «enero de 2011» aunque las ventas empiecen el 29: las fechas sin cobertura se marcan
        como «sin dato». El dominio va del 1 de enero del primer año con datos al 31 de
        diciembre del último año del calendario; fuera de ahí es un error explícito.
        """
        cobertura = consultas.cobertura_publicada(self._repo.db, version)
        calendario = self._repo.fechas(version)
        ultimo_calendario = max(calendario.values()) if calendario else cobertura["hasta"]
        dominio_desde = date(cobertura["desde"].year, 1, 1)
        dominio_hasta = date(ultimo_calendario.year, 12, 31)
        inicio = date.fromisoformat(desde) if desde else cobertura["desde"]
        fin = date.fromisoformat(hasta) if hasta else cobertura["hasta"]
        if inicio > fin:
            raise FiltroInvalido(
                f"El periodo es inválido: {inicio.isoformat()} es posterior a {fin.isoformat()}."
            )
        if inicio < dominio_desde or fin > dominio_hasta:
            raise FiltroInvalido(
                f"El periodo {inicio.isoformat()}–{fin.isoformat()} sale del dominio "
                f"descriptivo {dominio_desde.isoformat()}–{dominio_hasta.isoformat()}."
            )
        return inicio, fin

    def _filtros_contexto(
        self,
        *,
        desde: str | None,
        hasta: str | None,
        tiendas: list[str] | None,
        estado: str | None,
        tienda: str | None,
        categorias: list[str] | None,
        departamentos: list[str] | None,
        productos: list[str] | None = None,
        a_desde: str | None = None,
        a_hasta: str | None = None,
        b_desde: str | None = None,
        b_hasta: str | None = None,
        descriptivo: bool = False,
    ) -> tuple[str, dict]:
        """Valida el contexto territorial y comercial de una vista descriptiva.

        El nivel de exploración es siempre «tienda»: estas vistas miran la intersección
        real tienda × categoría (o tienda × departamento) desde los agregados, no una
        serie agregada por departamento. Una tienda pedida acota el contexto; una tienda
        que no pertenece al estado seleccionado es un error, nunca un contexto vacío.
        """
        version = self._version()
        cal = consultas.catalogo(self._repo.db, version)
        tienda_limpia = (tienda or "").strip() or None
        estado_limpio = (estado or "").strip() or None
        if tienda_limpia:
            if tienda_limpia not in {t["store_id"] for t in cal["tiendas"]}:
                raise FiltroInvalido(f"Tienda inexistente: {tienda_limpia}")
            if estado_limpio:
                del_estado = {
                    t["store_id"] for t in cal["tiendas"] if t["state_id"] == estado_limpio
                }
                if tienda_limpia not in del_estado:
                    raise FiltroInvalido(
                        f"La tienda {tienda_limpia} no pertenece al estado {estado_limpio}."
                    )
        solicitadas = list(tiendas or [])
        if tienda_limpia:
            solicitadas = [tienda_limpia]
        # La validación del contexto reutiliza el resolvedor de siempre (con la ventana
        # recortada a la cobertura) y después la vista descriptiva recupera su ventana.
        ventana_descriptiva = (
            self._ventana_descriptiva(version, desde, hasta) if descriptivo else None
        )
        cobertura = consultas.cobertura_publicada(self._repo.db, version)
        desde_validacion = desde
        hasta_validacion = hasta
        if ventana_descriptiva is not None:
            desde_validacion = max(ventana_descriptiva[0], cobertura["desde"]).isoformat()
            hasta_validacion = min(ventana_descriptiva[1], cobertura["hasta"]).isoformat()
        filtros = consultas.resolver_filtros(
            self._repo.db,
            version,
            {
                "desde": desde_validacion,
                "hasta": hasta_validacion,
                "tiendas": solicitadas,
                "categorias": categorias,
                "departamentos": departamentos,
                "productos": productos,
                "a_desde": a_desde,
                "a_hasta": a_hasta,
                "b_desde": b_desde,
                "b_hasta": b_hasta,
                "nivel": "tienda",
                "estado": estado_limpio,
            },
        )
        if ventana_descriptiva is not None:
            inicio, fin = ventana_descriptiva
            filtros["ventana"] = Ventana(inicio, fin)
            filtros["ventana_cobertura"] = filtros["ventana"]
            filtros["cobertura"] = cobertura
        return version, filtros

    def comparacion(
        self,
        *,
        desde: str | None = None,
        hasta: str | None = None,
        a_desde: str | None = None,
        a_hasta: str | None = None,
        b_desde: str | None = None,
        b_hasta: str | None = None,
        tiendas: list[str] | None = None,
        estado: str | None = None,
        tienda: str | None = None,
        categorias: list[str] | None = None,
        departamentos: list[str] | None = None,
        productos: list[str] | None = None,
    ) -> ComparacionTiendaCategoriaRespuesta:
        """Comparación entre los periodos A y B por tienda × categoría.

        Cada fila es una combinación real del catálogo importado. El promedio diario usa
        días con observación y la cobertura se declara; la suma de las filas se contrasta
        con el total del contexto en la propia respuesta.
        """
        version, filtros = self._filtros_contexto(
            desde=desde,
            hasta=hasta,
            tiendas=tiendas,
            estado=estado,
            tienda=tienda,
            categorias=categorias,
            departamentos=departamentos,
            productos=productos,
            a_desde=a_desde,
            a_hasta=a_hasta,
            b_desde=b_desde,
            b_hasta=b_hasta,
        )
        datos = self._repo.comparacion_tienda_categoria(version, filtros)
        conteos = self._repo.conteos_contexto(version, filtros)
        self._marcar_cache()
        return ComparacionTiendaCategoriaRespuesta(
            version=version,
            unidad=datos["unidad"],
            metrica_inicial=MetricaAB(datos["metrica_inicial"]),
            metricas=[MetricaAB(metrica) for metrica in datos["metricas"]],
            regla_promedio=datos["regla_promedio"],
            periodo=PeriodoInclusivo(**datos["periodo"]),
            a=PeriodoInclusivo(**datos["a"]),
            b=PeriodoInclusivo(**datos["b"]),
            por_omision=datos["por_omision"],
            filas=[FilaComparacionTiendaCategoria(**fila) for fila in datos["filas"]],
            tiendas=[GrupoTiendaAB(**grupo) for grupo in datos["tiendas"]],
            conteos=ConteosContexto(**conteos),
            resumen_filas=ResumenFilasAB(**datos["conteos"]),
            reconciliacion=ReconciliacionAB(**datos["reconciliacion"]),
            filtros={
                "tiendas": filtros["tiendas"],
                "categorias": filtros["categorias"],
                "departamentos": filtros["departamentos"],
                "productos": filtros["productos"],
                "estado": filtros.get("estado"),
                "tienda": tienda,
                "periodo": datos["periodo"],
                "a": datos["a"],
                "b": datos["b"],
            },
            calculo_ms=datos.get("calculo_ms"),
        )

    def estacionalidad(
        self,
        *,
        dimension: DimensionEstacionalidad = DimensionEstacionalidad.estado,
        desde: str | None = None,
        hasta: str | None = None,
        tiendas: list[str] | None = None,
        estado: str | None = None,
        tienda: str | None = None,
        categorias: list[str] | None = None,
        departamentos: list[str] | None = None,
    ) -> EstacionalidadRespuesta:
        """Perfil semanal (lunes a domingo) y mensual (enero a diciembre) del contexto."""
        version, filtros = self._filtros_contexto(
            desde=desde,
            hasta=hasta,
            tiendas=tiendas,
            estado=estado,
            tienda=tienda,
            categorias=categorias,
            departamentos=departamentos,
            descriptivo=True,
        )
        datos = self._repo.estacionalidad(version, filtros, dimension.value)
        self._marcar_cache()
        self.ultimo_calculo_ms = max(self._repo.ultimo_calculo_ms, datos.get("calculo_ms") or 0)
        return EstacionalidadRespuesta(
            version=version,
            unidad=datos["unidad"],
            dimension=dimension,
            dimensiones=[DimensionEstacionalidad(valor) for valor in datos["dimensiones"]],
            periodo=PeriodoInclusivo(**datos["periodo"]),
            dominio=datos.get("dominio") or {},
            cobertura_ventas=datos.get("cobertura_ventas") or {},
            semanal=SemanalInfo(**datos["semanal"]),
            mensual=MensualInfo(**datos["mensual"]),
            filtros={
                "tiendas": filtros["tiendas"],
                "categorias": filtros["categorias"],
                "departamentos": filtros["departamentos"],
                "estado": filtros.get("estado"),
                "tienda": tienda,
            },
            calculo_ms=datos.get("calculo_ms"),
        )

    def matriz_temporal(
        self,
        *,
        anio: int,
        desde: str | None = None,
        hasta: str | None = None,
        tiendas: list[str] | None = None,
        estado: str | None = None,
        tienda: str | None = None,
        categorias: list[str] | None = None,
        departamentos: list[str] | None = None,
    ) -> MatrizTemporalRespuesta:
        """Matriz tienda × día del año pedido, con cero, positivo y sin dato."""
        version, filtros = self._filtros_contexto(
            desde=desde,
            hasta=hasta,
            tiendas=tiendas,
            estado=estado,
            tienda=tienda,
            categorias=categorias,
            departamentos=departamentos,
            descriptivo=True,
        )
        datos = self._repo.matriz_temporal(version, filtros, anio)
        self._marcar_cache()
        self.ultimo_calculo_ms = max(self._repo.ultimo_calculo_ms, datos.get("calculo_ms") or 0)
        return MatrizTemporalRespuesta(
            version=version,
            anio=datos["anio"],
            unidad=datos["unidad"],
            dias=[DiaMatriz(**dia) for dia in datos["dias"]],
            meses=[MarcaMes(**mes) for mes in datos["meses"]],
            tiendas=[FilaMatrizTemporal(**fila) for fila in datos["tiendas"]],
            escala=EscalaMatriz(**datos["escala"]),
            cobertura=CoberturaMatriz(**datos["cobertura"]),
            calculo_ms=datos.get("calculo_ms"),
        )

    def articulo(
        self,
        *,
        item: str,
        tienda: str,
        desde: str | None = None,
        hasta: str | None = None,
        categorias: list[str] | None = None,
        departamentos: list[str] | None = None,
    ) -> ArticuloRespuesta:
        """Detalle de artículo × tienda con ventas diarias, precio semanal y calendario."""
        version, filtros = self._filtros_contexto(
            desde=desde,
            hasta=hasta,
            tiendas=[tienda],
            estado=None,
            tienda=tienda,
            categorias=categorias,
            departamentos=departamentos,
        )
        datos = self._repo.articulo(version, filtros, item_id=item, tienda=tienda)
        self._marcar_cache()
        return ArticuloRespuesta(
            version=version,
            articulo=ArticuloDetalle(**datos["articulo"]),
            periodo=PeriodoInclusivo(**datos["periodo"]),
            unidad=datos["unidad"],
            ventas=[PuntoVentaDiaria(**punto) for punto in datos["ventas"]],
            precios=InfoPrecios(**datos["precios"]),
            capas=CapasCalendario(**datos["capas"]),
            intermitencia=IntermitenciaSerie(**datos["intermitencia"]),
            nota=datos["nota"],
            filtros={
                "tiendas": filtros["tiendas"],
                "categorias": filtros["categorias"],
                "departamentos": filtros["departamentos"],
                "tienda": tienda,
                "item": item,
            },
        )

    def intermitencia(
        self,
        *,
        tienda: str,
        desde: str | None = None,
        hasta: str | None = None,
        categorias: list[str] | None = None,
        departamentos: list[str] | None = None,
        pagina: int = 1,
        tamano: int = 25,
        orden: str = "ceros",
    ) -> IntermitenciaRespuesta:
        """Intermitencia (ceros observados) de las series de una tienda."""
        version, filtros = self._filtros_contexto(
            desde=desde,
            hasta=hasta,
            tiendas=[tienda],
            estado=None,
            tienda=tienda,
            categorias=categorias,
            departamentos=departamentos,
        )
        ventana: Ventana = filtros["ventana"]
        datos = self._repo.intermitencia(
            version,
            tienda=tienda,
            desde=ventana.desde,
            hasta=ventana.hasta,
            categorias=list(filtros["categorias"]),
            departamentos=list(filtros["departamentos"]),
            pagina=pagina,
            tamano=tamano,
            orden=orden,
        )
        self._marcar_cache()
        return IntermitenciaRespuesta(
            version=version,
            tienda=datos["tienda"],
            unidad=datos["unidad"],
            periodo=PeriodoInclusivo(**datos["periodo"]),
            resumen=ResumenIntermitencia(**datos["resumen"]),
            filas=[FilaIntermitencia(**fila) for fila in datos["filas"]],
            paginacion=Paginacion(**datos["paginacion"]),
            reglas=datos["reglas"],
            calculo_ms=datos.get("calculo_ms"),
        )

    def auditoria_temporal(self) -> AuditoriaTemporalRespuesta:
        """Evidencia de separación temporal por ejecución y corte.

        No se calcula aquí: se lee el documento que produce
        ``scripts.auditar_separacion_temporal``. Si no existe, la respuesta lo declara en lugar de
        inventar una conclusión.
        """
        version = self._version()
        documento = self._repo.auditoria_temporal(version)
        if not documento:
            return AuditoriaTemporalRespuesta(
                version=version,
                esquema="sin-auditoria",
                generado_en="",
                disponible=False,
                motivo=(
                    "No hay auditoría temporal guardada para esta versión. Se genera con "
                    "«python -m scripts.auditar_separacion_temporal»."
                ),
            )
        return AuditoriaTemporalRespuesta(**documento)

    def evaluacion_ventanas(
        self,
        *,
        etapa: str = "final",
        run_id: str | None = None,
    ) -> VentanasEvaluacionRespuesta:
        """Tabla completa de evaluación por ventana de 28 días, en dos granos.

        - **Detalle artículo × tienda**: el error se calcula antes de agregar series.
        - **Total del segmento**: se suman predicción y observado del día y después se mide,
          usando las predicciones agregadas publicadas (que son la suma de las series).

        Las métricas se consolidan desde numeradores y denominadores guardados, nunca
        promediando porcentajes. El WAPE se publica como razón y como porcentaje; si el
        denominador es cero se marca como no calculable.
        """
        inicio = time.perf_counter()
        version = self._version()
        modelo_doc = (
            self._repo.ejecucion(version, run_id)
            if run_id
            else self._repo.ejecucion_recomendada(version)
        )
        if not modelo_doc:
            raise DatosPendientes()
        if etapa not in ("desarrollo", "final"):
            raise FiltroInvalido("La etapa debe ser «desarrollo» o «final».")
        evaluaciones = self._repo.evaluaciones_modelo(version, modelo_doc["run_id"], etapa=etapa)
        if not evaluaciones and etapa == "desarrollo":
            for candidata in self._repo.ejecuciones(version):
                if candidata.get("variante") != modelo_doc.get("variante"):
                    continue
                if candidata.get("estado") != "desarrollo":
                    continue
                previas = self._repo.evaluaciones_modelo(
                    version, candidata["run_id"], etapa="desarrollo"
                )
                if previas:
                    evaluaciones = previas
                    modelo_doc = {**modelo_doc, "run_id": candidata["run_id"]}
                    break
        if not evaluaciones:
            raise DatosPendientes()

        ventanas: list[VentanaEvaluada] = []
        for evaluacion in evaluaciones:
            origen = int(evaluacion["origen_d"])
            detalle = _grano_detalle(evaluacion)
            agregado = _grano_agregado(self._repo, version, modelo_doc["run_id"], origen)
            fallback = int((modelo_doc.get("cobertura") or {}).get("fallback") or 0)
            ventanas.append(
                VentanaEvaluada(
                    origen_d=origen,
                    origen_fecha=date.fromisoformat(str(evaluacion["origen_fecha"])),
                    fin_fecha=date.fromisoformat(str(evaluacion["fin_fecha"])),
                    etapa=str(evaluacion.get("etapa") or etapa),
                    series=int(evaluacion.get("series") or 0),
                    dias=int(modelo_doc.get("horizonte", 28)),
                    detalle=detalle,
                    agregado=agregado,
                    cobertura=modelo_doc.get("cobertura") or {},
                    fallback=fallback,
                )
            )

        consolidado = {
            "detalle": _consolidar_grano([ventana.detalle for ventana in ventanas]),
            "agregado": _consolidar_grano([ventana.agregado for ventana in ventanas]),
            "ventanas": len(ventanas),
            "nota": (
                "Consolidado desde numeradores y denominadores guardados. Con horizontes "
                "solapados se cuentan pares origen × fecha objetivo, no días independientes; "
                "dos ventanas no permiten afirmar significancia estadística."
            ),
        }

        por_estado = _segmentos_por_nivel(evaluaciones, "estado")
        por_categoria = _segmentos_por_nivel(evaluaciones, "categoria")
        reconciliacion = _reconciliar_predicciones(
            self._repo,
            version,
            modelo_doc["run_id"],
            evaluaciones,
            revision=(self._repo.publicacion() or {}).get("_id"),
        )

        advertencias = [
            "El WAPE se publica como razón (Σ|error| ÷ Σobservado) y como porcentaje; no se "
            "compara con el WAPE del detalle cuando cambia el grano.",
            "Dos ventanas de evaluación no bastan para afirmar significancia estadística; se "
            "reportan diferencias observadas, no un contraste de hipótesis.",
            "No se declara WRMSSE oficial: esa métrica exige pesos por serie, nivel y periodo "
            "que aquí no están implementados.",
        ]
        cobertura = modelo_doc.get("cobertura") or {}
        if int(cobertura.get("fallback") or 0) > 0:
            advertencias.append(
                f"La ejecución declara {cobertura['fallback']} series con fallback al baseline: "
                "el candidato puro no cubre todas las series."
            )

        return VentanasEvaluacionRespuesta(
            version=version,
            revision=(self._repo.publicacion() or {}).get("_id"),
            politica_precision=self._politica_precision(),
            run_id=modelo_doc["run_id"],
            variante=modelo_doc.get("variante"),
            etapa=etapa,
            horizonte=int(modelo_doc.get("horizonte", 28)),
            granos=["detalle artículo × tienda", "total del segmento"],
            formulas=FORMULAS_EVALUACION_VENTANAS,
            advertencias=advertencias,
            ventanas=ventanas,
            origenes_graficables=self._repo.origenes_publicados(
                version, modelo_doc["run_id"]
            ),
            consolidado=consolidado,
            por_estado=por_estado,
            por_categoria=por_categoria,
            reconciliacion=reconciliacion,
            cobertura_modelo=(
                CoberturaModelo(**cobertura) if cobertura else None
            ),
            modelo=_modelo_resumen(modelo_doc),
            calculo_ms=round((time.perf_counter() - inicio) * 1000, 1),
        )

    # -- planificación de ventas ---------------------------------------------
    def planificacion(
        self,
        *,
        tiendas: list[str] | None = None,
        categorias: list[str] | None = None,
        departamentos: list[str] | None = None,
        texto: str | None = None,
        orden: str = "volumen_previsto",
        minimo_observado: float | None = None,
        pagina: int = 1,
        tamano: int = 25,
        run_id: str | None = None,
    ) -> PlanificacionRespuesta:
        """Plan de ventas a cuatro semanas, paginado en el servidor.

        Los totales del resumen corresponden al conjunto filtrado completo, no a la página.
        """
        version = self._version()
        documento = (
            self._repo.ejecucion(version, run_id) if run_id else self._repo.ejecucion_recomendada(version)
        )
        if not documento:
            raise DatosPendientes("Sin ejecución publicada: no hay plan de ventas que mostrar.")
        revision = (self._repo.publicacion() or {}).get("_id")
        datos = self._repo.planificacion(
            version,
            documento["run_id"],
            revision=revision,
            tiendas=tiendas,
            categorias=categorias,
            departamentos=departamentos,
            texto=texto,
            orden=orden,
            minimo_observado=minimo_observado,
            pagina=pagina,
            tamano=tamano,
        )
        return PlanificacionRespuesta(**datos)

    def planificacion_completa(
        self,
        *,
        tiendas: list[str] | None = None,
        categorias: list[str] | None = None,
        departamentos: list[str] | None = None,
        texto: str | None = None,
        orden: str = "volumen_previsto",
        minimo_observado: float | None = None,
        run_id: str | None = None,
    ) -> dict:
        """Filas del filtro completo para la descarga (sin paginar)."""
        version = self._version()
        documento = (
            self._repo.ejecucion(version, run_id) if run_id else self._repo.ejecucion_recomendada(version)
        )
        if not documento:
            raise DatosPendientes("Sin ejecución publicada: no hay plan que descargar.")
        revision = (self._repo.publicacion() or {}).get("_id")
        return self._repo.planificacion_completa(
            version,
            documento["run_id"],
            revision=revision,
            tiendas=tiendas,
            categorias=categorias,
            departamentos=departamentos,
            texto=texto,
            orden=orden,
            minimo_observado=minimo_observado,
        )

    def detalle_plan(self, *, tienda: str, item: str, run_id: str | None = None) -> DetallePlanRespuesta:
        """Detalle de una serie artículo × tienda del plan publicado."""
        version = self._version()
        documento = (
            self._repo.ejecucion(version, run_id) if run_id else self._repo.ejecucion_recomendada(version)
        )
        if not documento:
            raise DatosPendientes("Sin ejecución publicada: no hay detalle que mostrar.")
        revision = (self._repo.publicacion() or {}).get("_id")
        datos = self._repo.detalle_plan(
            version, documento["run_id"], tienda=tienda, item=item, revision=revision
        )
        if not datos:
            raise FiltroInvalido(
                f"La serie {tienda} · {item} no está en el plan publicado: revisa tienda y artículo."
            )
        return DetallePlanRespuesta(**datos)

    def evaluacion_horizonte(
        self,
        *,
        tienda: str | None = None,
        categoria: str | None = None,
        departamento: str | None = None,
        run_id: str | None = None,
    ) -> EvaluacionHorizonteRespuesta:
        """Evaluación del total de 28 días por serie y corte."""
        version = self._version()
        documento = (
            self._repo.ejecucion(version, run_id) if run_id else self._repo.ejecucion_recomendada(version)
        )
        if not documento:
            raise DatosPendientes("Sin ejecución publicada: no hay evaluación a 28 días.")
        revision = (self._repo.publicacion() or {}).get("_id")
        datos = self._repo.evaluacion_horizonte(
            version,
            documento["run_id"],
            revision=revision,
            tienda=tienda,
            categoria=categoria,
            departamento=departamento,
        )
        return EvaluacionHorizonteRespuesta(**datos)

    def matriz_desempeno(
        self,
        *,
        nivel: NivelPronostico = NivelPronostico.total,
        etapa: str = "final",
        run_id: str | None = None,
    ) -> MatrizDesempenoRespuesta:
        """Desempeño por corte y horizonte: ventaja del candidato y sesgo.

        Cada celda se calcula sobre las mismas series y fechas para los dos métodos, al
        grano artículo × tienda (el error se mide antes de agregar). Una celda sin
        observaciones se devuelve con ``motivo``, nunca como cero.
        """
        inicio = time.perf_counter()
        version = self._version()
        modelo_doc = (
            self._repo.ejecucion(version, run_id)
            if run_id
            else self._repo.ejecucion_recomendada(version)
        )
        if not modelo_doc:
            raise DatosPendientes()
        if etapa not in ("desarrollo", "final"):
            raise FiltroInvalido("La etapa debe ser «desarrollo» o «final».")
        if nivel == NivelPronostico.producto:
            raise FiltroInvalido(
                "El desempeño por horizonte se mide al grano artículo × tienda; no se "
                "publica por artículo individual."
            )
        evaluaciones = self._repo.evaluaciones_modelo(
            version, modelo_doc["run_id"], etapa=etapa
        )
        if not evaluaciones and etapa == "desarrollo":
            for candidata in self._repo.ejecuciones(version):
                if candidata.get("variante") != modelo_doc.get("variante"):
                    continue
                if candidata.get("estado") != "desarrollo":
                    continue
                previas = self._repo.evaluaciones_modelo(
                    version, candidata["run_id"], etapa="desarrollo"
                )
                if previas:
                    evaluaciones = previas
                    modelo_doc = {**modelo_doc, "run_id": candidata["run_id"]}
                    break
        if not evaluaciones:
            raise DatosPendientes()
        return _matriz_desempeno(
            version=version,
            modelo_doc=modelo_doc,
            evaluaciones=evaluaciones,
            etapa=etapa,
            nivel=nivel,
            duracion_ms=(time.perf_counter() - inicio) * 1000,
        )

    # -- precios (métrica separada de las unidades) --------------------------
    def _version_precios(self) -> str:
        """Versión de precios vigente; sin ella no hay análisis de precios."""
        manifiesto = self._repo.manifiesto_precios()
        if not manifiesto:
            raise DatosPendientes()
        return str(manifiesto["version_precios"])

    def precios_distribucion(
        self,
        *,
        tienda: str,
        desde: str | None = None,
        hasta: str | None = None,
        categorias: list[str] | None = None,
        departamentos: list[str] | None = None,
        items: list[str] | None = None,
    ) -> DistribucionPreciosRespuesta:
        """Distribución del precio medio por artículo × tienda en una tienda."""
        version_precios = self._version_precios()
        version, filtros = self._filtros_contexto(
            desde=desde,
            hasta=hasta,
            tiendas=[tienda],
            estado=None,
            tienda=tienda,
            categorias=categorias,
            departamentos=departamentos,
        )
        ventana: Ventana = filtros["ventana"]
        datos = self._repo.precios_distribucion(
            version_precios,
            {
                "tienda": tienda,
                "desde": ventana.desde,
                "hasta": ventana.hasta,
                "categorias": list(filtros["categorias"]),
                "departamentos": list(filtros["departamentos"]),
                "items_permitidos": [i for i in (items or []) if i] or None,
                "version_ventas": version,
            },
        )
        self._marcar_cache()
        valores = [ValorPrecio(**valor) for valor in datos.get("valores") or []]
        # Cobertura completa del catálogo del contexto: un artículo sin ninguna semana con
        # precio también cuenta, como ausencia (nunca como precio cero).
        existencias = self._repo.existencias(version)
        catalogo = existencias["productos"]
        permitidos = {item for item in (items or []) if item}
        esperados = {
            item: catalogo[item]
            for item in existencias["items_por_tienda"].get(tienda, [])
            if item in catalogo
            and catalogo[item]["cat_id"] in filtros["categorias"]
            and (
                not filtros["departamentos"]
                or catalogo[item]["dept_id"] in filtros["departamentos"]
            )
            and (not permitidos or item in permitidos)
        }
        con_precio = {valor.item_id for valor in valores}
        faltantes = [
            ValorPrecio(
                item_id=item,
                cat_id=datos_item["cat_id"],
                dept_id=datos_item["dept_id"],
                precio_medio=None,
                semanas_con_precio=0,
                dias_cubiertos=0,
            )
            for item, datos_item in sorted(esperados.items())
            if item not in con_precio
        ]
        valores = sorted(valores + faltantes, key=lambda valor: valor.item_id)
        # La cobertura la calcula el módulo sobre el universo del archivo de precios; aquí
        # solo se añaden los artículos de la tienda que no tienen ni una fila de precio, para
        # que completa + parcial + sin precio siga sumando el universo publicado.
        cobertura = dict(datos["cobertura"])
        if faltantes:
            cobertura["sin_precio"] = int(cobertura.get("sin_precio") or 0) + len(faltantes)
            total = len(valores)
            cobertura["pct_completa"] = (
                round(100 * cobertura["completa"] / total, 2) if total else None
            )
            cobertura["pct_con_algun_precio"] = (
                round(100 * (total - cobertura["sin_precio"]) / total, 2) if total else None
            )
        detalle_sin_precio = dict(datos.get("sin_precio") or {})
        if faltantes:
            detalle_sin_precio["combinaciones"] = int(
                detalle_sin_precio.get("combinaciones") or 0
            ) + len(faltantes)
            detalle_sin_precio["sin_precio_en_toda_la_tienda"] = int(
                detalle_sin_precio.get("sin_precio_en_toda_la_tienda") or 0
            ) + len(faltantes)
        con_precio_n = sum(1 for valor in valores if valor.precio_medio is not None)
        return DistribucionPreciosRespuesta(
            unidad=datos["unidad"],
            tienda=datos["tienda"],
            periodo=PeriodoPrecios(**datos["periodo"]),
            regla_ponderacion=datos["regla_ponderacion"],
            combinaciones_incluidas=len(valores),
            articulos=con_precio_n,
            cobertura=CoberturaPrecios(**cobertura),
            cuartiles=(
                CuartilesPrecio(**datos["cuartiles"]) if datos.get("cuartiles") else None
            ),
            media=datos.get("media"),
            cajas=[CajaPrecio(**caja) for caja in datos.get("cajas") or []],
            valores=valores,
            sin_precio=detalle_sin_precio,
            motivo=datos.get("motivo"),
        )

    def precios_cambios(
        self,
        *,
        tienda: str,
        desde: str | None = None,
        hasta: str | None = None,
        categorias: list[str] | None = None,
        departamentos: list[str] | None = None,
    ) -> CambiosPreciosRespuesta:
        """Frecuencia y dirección de los cambios de precio entre semanas con precio."""
        version_precios = self._version_precios()
        version, filtros = self._filtros_contexto(
            desde=desde,
            hasta=hasta,
            tiendas=[tienda],
            estado=None,
            tienda=tienda,
            categorias=categorias,
            departamentos=departamentos,
        )
        ventana: Ventana = filtros["ventana"]
        datos = self._repo.precios_cambios(
            version_precios,
            {
                "tienda": tienda,
                "desde": ventana.desde,
                "hasta": ventana.hasta,
                "categorias": list(filtros["categorias"]),
                "departamentos": list(filtros["departamentos"]),
                "version_ventas": version,
            },
        )
        self._marcar_cache()
        return CambiosPreciosRespuesta(
            unidad=datos["unidad"],
            tienda=datos["tienda"],
            periodo=PeriodoPrecios(**datos["periodo"]),
            umbral=UmbralPrecio(**datos["umbral"]),
            semanas=datos["semanas"],
            semanas_del_intervalo=datos.get("semanas_del_intervalo", 0),
            precision_comparacion=datos.get("precision_comparacion", "centavos"),
            series_con_precio=datos["series_con_precio"],
            totales=TotalesCambiosPrecio(**datos["totales"]),
            por_categoria=[
                CategoriaCambiosPrecio(**categoria) for categoria in datos.get("por_categoria") or []
            ],
            series=[SerieCambiosPrecio(**serie) for serie in datos.get("series") or []],
            nota=datos["nota"],
        )

    def precios_evolucion(
        self,
        *,
        tienda: str,
        item_id: str,
        desde: str | None = None,
        hasta: str | None = None,
    ) -> EvolucionPrecioRespuesta:
        """Serie semanal del precio de un artículo en una tienda."""
        version_precios = self._version_precios()
        version, filtros = self._filtros_contexto(
            desde=desde,
            hasta=hasta,
            tiendas=[tienda],
            estado=None,
            tienda=tienda,
            categorias=None,
            departamentos=None,
        )
        ventana: Ventana = filtros["ventana"]
        datos = self._repo.precios_evolucion(
            version_precios,
            {
                "tienda": tienda,
                "item_id": item_id,
                "desde": ventana.desde,
                "hasta": ventana.hasta,
            },
        )
        self._marcar_cache()
        return EvolucionPrecioRespuesta(
            unidad=datos["unidad"],
            tienda=datos["tienda"],
            item_id=datos["item_id"],
            periodo=PeriodoPrecios(**datos["periodo"]),
            puntos=[PuntoPrecio(**punto) for punto in datos.get("puntos") or []],
            semanas_con_precio=datos["semanas_con_precio"],
            semanas_sin_precio=datos["semanas_sin_precio"],
            cambios=CambiosSeriePrecio(**datos["cambios"]),
            umbral=UmbralPrecio(**datos["umbral"]),
            motivo=datos.get("motivo"),
        )


def _cobertura(ventana: Ventana | None) -> Cobertura | None:
    if not ventana:
        return None
    return Cobertura(desde=ventana.desde, hasta=ventana.hasta, dias=ventana.dias)


def _derivado(datos: dict | None, version: str) -> DerivadoInfo | None:
    """Traduce el estado del derivado local al contrato público."""
    if not datos:
        return None
    publicado = bool(datos.get("publicado"))
    return DerivadoInfo(
        nombre=str(datos.get("nombre") or derivados.NOMBRE_DERIVADO),
        estado=str(datos.get("estado") or "ausente"),
        publicado=publicado,
        origen=(
            "agregado derivado publicado en m5_agregados"
            if publicado
            else "bloques locales (el derivado no está publicado)"
        ),
        version_datos=str(datos.get("version_datos") or version),
        huella=datos.get("huella"),
        documentos=datos.get("documentos"),
        dias=datos.get("dias"),
        duracion_s=datos.get("duracion_s"),
        construido_en=datos.get("construido_en"),
        reutilizado=bool(datos.get("reutilizado")),
        reconciliacion=datos.get("reconciliacion"),
    )


def _descripcion_contexto(filtros: dict) -> str:
    """Texto corto del contexto: territorio y catálogo efectivos."""
    estado = filtros.get("estado")
    tienda = filtros.get("tienda")
    tiendas = list(filtros.get("tiendas_contexto") or filtros.get("tiendas") or [])
    if tienda:
        territorio = tienda
    elif estado:
        territorio = f"{consultas.ESTADOS_NOMBRE.get(estado, estado)} ({estado}) · {len(tiendas)} tiendas"
    else:
        territorio = f"{len(tiendas)} tiendas"
    departamentos = list(filtros.get("departamentos") or [])
    categorias = list(filtros.get("categorias") or [])
    todos = set(consultas.CATEGORIAS_NOMBRE)
    if departamentos:
        comercio = ", ".join(departamentos)
    elif categorias and set(categorias) != todos:
        comercio = ", ".join(
            consultas.CATEGORIAS_NOMBRE.get(categoria, categoria) for categoria in categorias
        )
    else:
        comercio = "todas las categorías"
    return f"{territorio} · {comercio}"


def _contexto(filtros: dict, resumen: dict) -> ContextoPulso:
    """Ámbito efectivo: nivel, territorio, catálogo, selección y totales."""
    nivel = NivelExploracion(filtros["nivel"])
    tienda = filtros.get("tienda")
    estado = filtros.get("estado")
    departamento = filtros.get("departamento")
    departamentos_panel = list(filtros.get("departamentos_panel") or [])
    productos = list(filtros["productos"])
    disponibles = len(filtros.get("productos_contexto") or [])
    tiendas_contexto = list(filtros.get("tiendas_contexto") or filtros["tiendas"])
    if nivel == NivelExploracion.tienda:
        descripcion = f"{len(tiendas_contexto)} tiendas del contexto territorial"
        if estado:
            descripcion = (
                f"{consultas.ESTADOS_NOMBRE.get(estado, estado)} · {len(tiendas_contexto)} tiendas"
            )
    elif nivel == NivelExploracion.departamento:
        descripcion = (
            f"{tienda or consultas.ESTADOS_NOMBRE.get(estado or '', estado or 'contexto')} · "
            f"{len(departamentos_panel)} departamentos permitidos por los filtros"
        )
    else:
        descripcion = (
            f"{tienda or estado} · {departamento} · {len(productos)} de {disponibles} productos"
        )
    return ContextoPulso(
        nivel=nivel,
        estado=estado,
        tienda=tienda,
        tiendas_contexto=tiendas_contexto,
        departamento=departamento,
        departamentos=departamentos_panel,
        productos=productos,
        productos_disponibles=disponibles,
        seleccion_automatica=bool(filtros.get("seleccion_automatica")),
        total_contexto=resumen["total_contexto"],
        total_seleccion=resumen["total_seleccion"],
        participacion_pct=resumen["participacion_pct"],
        descripcion=descripcion,
    )


def _migas(filtros: dict) -> list[MigaNavegacion]:
    """Ruta de exploración: cada miga describe el estado al que vuelve."""
    nivel = NivelExploracion(filtros["nivel"])
    tienda = filtros.get("tienda")
    estado = filtros.get("estado")
    departamento = filtros.get("departamento")
    migas = [MigaNavegacion(nivel=NivelExploracion.tienda, etiqueta="Estados Unidos")]
    if estado:
        migas.append(
            MigaNavegacion(
                nivel=NivelExploracion.tienda,
                etiqueta=consultas.ESTADOS_NOMBRE.get(estado, estado),
                tienda=None,
            )
        )
    if nivel in (NivelExploracion.departamento, NivelExploracion.producto) and tienda:
        migas.append(
            MigaNavegacion(nivel=NivelExploracion.departamento, etiqueta=tienda, tienda=tienda)
        )
    if nivel == NivelExploracion.producto and (tienda or estado) and departamento:
        migas.append(
            MigaNavegacion(
                nivel=NivelExploracion.producto,
                etiqueta=departamento,
                tienda=tienda,
                departamento=departamento,
            )
        )
    return migas


def _metricas(datos: dict) -> dict:
    limpio = {clave: valor for clave, valor in datos.items() if clave.startswith(("n", "mae", "sesgo", "wape", "denominador", "ventanas"))}
    limpio.setdefault("n", 0)
    limpio.setdefault("mae", None)
    limpio.setdefault("sesgo", None)
    limpio.setdefault("wape", None)
    limpio.setdefault("denominador", 0)
    if "nota" in datos:
        limpio["nota"] = datos["nota"]
    return limpio


def _ventana(datos: dict) -> dict:
    metrica = _metricas(dict(datos.get("metricas") or {}))
    return {
        "nivel": datos.get("nivel"),
        "origen_d": datos.get("origen_d"),
        "origen_fecha": datos.get("origen_fecha"),
        "fin_fecha": datos.get("fin_fecha"),
        "horizonte": datos.get("horizonte"),
        "series": datos.get("series", 0),
        "metricas": metrica,
    }



# --------------------------------------------------------------------------- #
# Utilidades del modelo predictivo
# --------------------------------------------------------------------------- #

FORMULAS_EVALUACION_VENTANAS = {
    "mae": "Σ|predicción − observado| ÷ número de observaciones válidas",
    "sesgo": "Σ(predicción − observado) ÷ número de observaciones válidas",
    "wape": "Σ|predicción − observado| ÷ Σobservado (razón); se publica también ×100",
    "diferencia_absoluta": (
        "MAE_baseline − MAE_candidato en unidades por serie; positivo = el candidato tiene "
        "menos error"
    ),
    "mejora_relativa_pct": "100 × (MAE_baseline − MAE_candidato) ÷ MAE_baseline",
    "granos": (
        "«detalle artículo × tienda» mide el error antes de agregar series; «total del "
        "segmento» suma predicción y observado del día y después mide"
    ),
    "consolidado": (
        "se suman numeradores y denominadores de cada ventana; nunca se promedian MAE ni "
        "WAPE entre ventanas o segmentos"
    ),
    "no_calculable": "si Σobservado = 0, el WAPE se devuelve como no calculable (null)",
    "comparabilidad": (
        "candidato y baseline se miden sobre exactamente las mismas series, fechas y filtros"
    ),
    "unidades": (
        "detalle artículo × tienda: unidades por serie y día; total del segmento: unidades por "
        "día del total del segmento. Las dos cifras no son comparables entre sí"
    ),
    "compensacion": (
        "al sumar series antes de medir, los errores de distinto signo se compensan: una "
        "mejora del total no implica que mejoren las series una por una"
    ),
}


def _metrica_ventana(datos: dict, *, observaciones: int = 0) -> MetricasVentana:
    """Convierte una métrica guardada en el contrato, con WAPE en razón y en porcentaje."""
    wape_pct = datos.get("wape")
    return MetricasVentana(
        n=int(datos.get("n") or 0),
        observaciones=int(observaciones or datos.get("n") or 0),
        mae=datos.get("mae"),
        sesgo=datos.get("sesgo"),
        wape_pct=wape_pct,
        wape_razon=(round(float(wape_pct) / 100, 6) if wape_pct is not None else None),
        denominador=int(datos.get("denominador") or 0),
        wape_no_calculable=bool(datos.get("denominador_cero")) or wape_pct is None,
    )


def _comparar_grano(
    grano: str, candidato: dict, baseline: dict, *, motivo: str | None = None
) -> GranoVentana:
    """Construye un grano con sus dos métodos y la diferencia de MAE."""
    cand = _metrica_ventana(candidato)
    base = _metrica_ventana(baseline)
    diferencia = None
    mejora = None
    if cand.mae is not None and base.mae is not None:
        diferencia = round(float(base.mae) - float(cand.mae), 4)
        if float(base.mae) > 0:
            mejora = round(100 * diferencia / float(base.mae), 3)
    if motivo is None and cand.n == 0:
        motivo = "sin observaciones en esta ventana"
    return GranoVentana(
        grano=grano,
        candidato=cand,
        baseline=base,
        diferencia_absoluta=diferencia,
        mejora_relativa_pct=mejora,
        motivo=motivo,
    )


def _grano_detalle(evaluacion: dict) -> GranoVentana:
    """Grano artículo × tienda de una ventana evaluada."""
    return _comparar_grano(
        "detalle artículo × tienda",
        evaluacion.get("candidato") or {},
        evaluacion.get("baseline") or {},
    )


def _grano_agregado(repositorio, version: str, run_id: str, origen: int) -> GranoVentana:
    """Grano «total del segmento»: se suman predicción y observado del día y luego se mide."""
    documentos = repositorio.predictores(
        version, run_id, origen=origen, dimension="total", claves=["total"]
    )
    documento = documentos[0] if documentos else None
    if not documento or not documento.get("pronostico"):
        return GranoVentana(
            grano="total del segmento",
            candidato=MetricasVentana(n=0),
            baseline=MetricasVentana(n=0),
            motivo="sin predicciones agregadas publicadas para este origen",
        )
    pronostico = np.asarray(documento.get("pronostico") or [], dtype=float)
    baseline = np.asarray(documento.get("baseline") or [], dtype=float)
    observado_crudo = documento.get("observado")
    if observado_crudo is None:
        return GranoVentana(
            grano="total del segmento",
            candidato=MetricasVentana(n=0),
            baseline=MetricasVentana(n=0),
            motivo="este origen no tiene observaciones: es pronóstico, no evaluación",
        )
    observado = np.asarray(
        [np.nan if valor is None else float(valor) for valor in observado_crudo], dtype=float
    )
    valido = ~np.isnan(observado)
    cand = modelo.metricas(np.where(valido, pronostico - observado, np.nan), observado)
    base = modelo.metricas(np.where(valido, baseline - observado, np.nan), observado)
    return _comparar_grano("total del segmento", cand, base)


def _consolidar_grano(granos: list[GranoVentana]) -> dict:
    """Consolida ventanas sumando numeradores y denominadores (no promedia porcentajes)."""
    candidatos = [
        {
            "n": grano.candidato.n,
            "mae": grano.candidato.mae,
            "sesgo": grano.candidato.sesgo,
            "wape": grano.candidato.wape_pct,
            "denominador": grano.candidato.denominador,
        }
        for grano in granos
    ]
    baselines = [
        {
            "n": grano.baseline.n,
            "mae": grano.baseline.mae,
            "sesgo": grano.baseline.sesgo,
            "wape": grano.baseline.wape_pct,
            "denominador": grano.baseline.denominador,
        }
        for grano in granos
    ]
    cand = modelo.agregar_metricas_guardadas(candidatos)
    base = modelo.agregar_metricas_guardadas(baselines)
    diferencia = None
    mejora = None
    if cand["mae"] is not None and base["mae"] is not None:
        diferencia = round(float(base["mae"]) - float(cand["mae"]), 4)
        if float(base["mae"]) > 0:
            mejora = round(100 * diferencia / float(base["mae"]), 3)
    return {
        "grano": granos[0].grano if granos else "sin datos",
        "candidato": _metrica_ventana(cand).model_dump(),
        "baseline": _metrica_ventana(base).model_dump(),
        "diferencia_absoluta": diferencia,
        "mejora_relativa_pct": mejora,
        "ventanas": len(granos),
    }


def _segmentos_por_nivel(evaluaciones: list[dict], nivel: str) -> list[SegmentoVentana]:
    """Consolida los segmentos de un nivel (estado o categoría) sobre todas las ventanas."""
    agrupados: dict[str, dict] = {}
    for evaluacion in evaluaciones:
        for segmento in evaluacion.get("segmentos") or []:
            if str(segmento.get("nivel")) != nivel:
                continue
            registro = agrupados.setdefault(
                str(segmento["clave"]),
                {
                    "series": 0,
                    "detalle_c": [],
                    "detalle_b": [],
                    "agregado_c": [],
                    "agregado_b": [],
                },
            )
            registro["series"] = max(registro["series"], int(segmento.get("series") or 0))
            registro["detalle_c"].append(segmento.get("candidato") or {})
            registro["detalle_b"].append(segmento.get("baseline") or {})
            agregado = segmento.get("agregado") or {}
            if agregado:
                registro["agregado_c"].append(agregado.get("candidato") or {})
                registro["agregado_b"].append(agregado.get("baseline") or {})
    salida: list[SegmentoVentana] = []
    for clave in sorted(agrupados):
        registro = agrupados[clave]
        detalle = _comparar_grano(
            "detalle artículo × tienda",
            modelo.agregar_metricas_guardadas(registro["detalle_c"]),
            modelo.agregar_metricas_guardadas(registro["detalle_b"]),
        )
        agregado = _comparar_grano(
            "total del segmento",
            modelo.agregar_metricas_guardadas(registro["agregado_c"]),
            modelo.agregar_metricas_guardadas(registro["agregado_b"]),
            motivo=None if registro["agregado_c"] else "sin métricas de la serie agregada",
        )
        salida.append(
            SegmentoVentana(
                nivel=nivel,
                clave=clave,
                series=registro["series"],
                detalle=detalle,
                agregado=agregado,
            )
        )
    return salida


def _reconciliar_predicciones(
    repositorio, version: str, run_id: str, evaluaciones: list[dict], revision: str | None = None
) -> dict:
    """Comprueba que el agregado publicado sea la suma del detalle artículo × tienda.

    Se hace sobre un origen que tenga ambos conjuntos persistidos: la suma por día de las
    predicciones de las series debe coincidir con la predicción agregada del total. La consulta
    se limita a la revisión publicada: sin ese filtro conviven la publicación decimal y la
    anterior en enteros, y el recuento de series se duplica.
    """
    # Los vectores por serie solo se publican en algunos orígenes (el corte final); la
    # comprobación se hace sobre los orígenes que sí tienen ambos conjuntos.
    filtro_revision = (
        {"revision": revision} if revision else {"revision": {"$exists": False}}
    )
    con_series = sorted(
        {
            int(documento["origen_d"])
            for documento in repositorio._db["m5_predicciones_series"].find(
                {"run_id": run_id, "version": version, **filtro_revision}, {"origen_d": 1}
            )
        }
    )
    origenes = sorted({int(evaluacion["origen_d"]) for evaluacion in evaluaciones} | set(con_series))
    agregados = {
        int(documento["origen_d"]): documento
        for documento in repositorio._db["m5_predicciones"].find(
            {
                "run_id": run_id,
                "version": version,
                "dimension": "total",
                "clave": "total",
                **filtro_revision,
            },
            {"origen_d": 1, "pronostico": 1, "baseline": 1},
        )
    }
    candidatos = [origen for origen in origenes if origen in agregados and origen in con_series]
    if not candidatos:
        return {
            "cuadra": None,
            "motivo": "no hay predicción agregada del total para los orígenes evaluados",
        }
    origen = candidatos[-1]
    series = list(
        repositorio._db["m5_predicciones_series"].find(
            {"run_id": run_id, "version": version, "origen_d": origen, **filtro_revision},
            {"pronostico": 1, "baseline": 1},
        )
    )
    if not series:
        return {
            "cuadra": None,
            "origen_d": origen,
            "motivo": (
                "las predicciones artículo × tienda solo se publican para el corte final; "
                "no hay vectores para este origen"
            ),
        }
    suma = np.zeros(len(agregados[origen]["pronostico"]), dtype=float)
    for documento in series:
        suma += np.asarray(documento["pronostico"], dtype=float)
    esperado = np.asarray(agregados[origen]["pronostico"], dtype=float)
    diferencia = float(np.max(np.abs(suma - esperado)))
    decimal = _es_politica_decimal(repositorio)
    tolerancia = 1e-6 if decimal else 0.51
    return {
        "cuadra": bool(diferencia < tolerancia),
        "origen_d": origen,
        "revision": revision,
        "series": len(series),
        "max_diferencia_absoluta": round(diferencia, 6 if decimal else 4),
        "tolerancia": tolerancia,
        "nota": (
            "El agregado se publica como la suma del detalle de la misma revisión. Con la "
            "política decimal la tolerancia es de una millonésima por día; la publicación "
            "anterior en enteros toleraba medio punto por día de redondeo."
        ),
    }


def _es_politica_decimal(repositorio) -> bool:
    """Indica si la revisión publicada conserva decimales en la serie canónica."""
    try:
        publicacion = repositorio.publicacion() or {}
    except Exception:  # noqa: BLE001 - la traza nunca debe romper la respuesta
        return True
    return (publicacion.get("politica") or "decimal-v1") == "decimal-v1"


FORMULAS_MATRIZ_DESEMPENO = {
    "mae_candidato": "media(|candidato − observado|) sobre las series válidas del horizonte",
    "mae_baseline": "media(|baseline − observado|) sobre esas mismas series y fechas",
    "ventaja": (
        "MAE_baseline − MAE_candidato, en unidades por serie: positivo = el candidato tiene "
        "menos error; negativo = tiene más; cero = el mismo error"
    ),
    "sesgo": (
        "media(predicción − observado), en unidades por serie: positivo = sobreestimación, "
        "negativo = subestimación"
    ),
    "grano": (
        "artículo × tienda: el error se calcula antes de agregar series, así que un error "
        "positivo y otro negativo no se compensan"
    ),
    "comparabilidad": (
        "candidato y baseline se miden sobre exactamente las mismas series, fechas y filtros; "
        "el MAE se recalcula desde los errores guardados"
    ),
    "no_calculable": (
        "una celda sin observaciones se devuelve sin valor y con su motivo, nunca como cero"
    ),
    "etapas": (
        "«desarrollo» eligió la variante; «final» son cortes reservados que no participaron "
        "en la selección de parámetros"
    ),
}

NOTA_ESCALA_MATRIZ = (
    "Escala divergente centrada en cero y común a todas las celdas de la vista. La lectura "
    "de mejora y la de sesgo tienen escalas distintas: no comparten leyenda ni significado."
)


def _matriz_desempeno(
    *,
    version: str,
    modelo_doc: dict,
    evaluaciones: list[dict],
    etapa: str,
    nivel: NivelPronostico,
    duracion_ms: float,
) -> MatrizDesempenoRespuesta:
    """Matriz origen × horizonte con la ventaja del candidato y el sesgo de ambos."""
    filas: list[FilaOrigenHorizontes] = []
    max_ventaja = 0.0
    max_sesgo = 0.0
    horizonte = int(modelo_doc.get("horizonte", 28))
    for evaluacion in evaluaciones:
        celdas: list[CeldaHorizonte] = []
        for punto in evaluacion.get("por_horizonte") or []:
            candidato = punto.get("candidato") or {}
            baseline = punto.get("baseline") or {}
            n = int(candidato.get("n") or baseline.get("n") or 0)
            mae_candidato = candidato.get("mae")
            mae_baseline = baseline.get("mae")
            ventaja = None
            if n and mae_candidato is not None and mae_baseline is not None:
                ventaja = round(float(mae_baseline) - float(mae_candidato), 4)
                max_ventaja = max(max_ventaja, abs(ventaja))
            sesgo_candidato = candidato.get("sesgo")
            sesgo_baseline = baseline.get("sesgo")
            for valor in (sesgo_candidato, sesgo_baseline):
                if valor is not None:
                    max_sesgo = max(max_sesgo, abs(float(valor)))
            motivo = None
            if not n:
                motivo = "sin observaciones en este horizonte"
            celdas.append(
                CeldaHorizonte(
                    horizonte=int(punto["horizonte"]),
                    fecha=date.fromisoformat(str(punto["fecha"])) if punto.get("fecha") else None,
                    n=n,
                    denominador=int(candidato.get("denominador") or baseline.get("denominador") or 0),
                    mae_candidato=mae_candidato,
                    mae_baseline=mae_baseline,
                    ventaja=ventaja,
                    sesgo_candidato=sesgo_candidato,
                    sesgo_baseline=sesgo_baseline,
                    motivo=motivo,
                )
            )
        filas.append(
            FilaOrigenHorizontes(
                origen_d=int(evaluacion["origen_d"]),
                origen_fecha=date.fromisoformat(str(evaluacion["origen_fecha"])),
                fin_fecha=(
                    date.fromisoformat(str(evaluacion["fin_fecha"]))
                    if evaluacion.get("fin_fecha")
                    else None
                ),
                etapa=str(evaluacion.get("etapa") or etapa),
                series=int(evaluacion.get("series") or 0),
                celdas=celdas,
            )
        )
    return MatrizDesempenoRespuesta(
        version=version,
        run_id=modelo_doc["run_id"],
        variante=modelo_doc.get("variante"),
        horizonte=horizonte,
        etapa=etapa,
        nivel=nivel.value,
        origenes=filas,
        escala=EscalaMatrizDesempeno(
            max_abs_ventaja=round(max_ventaja, 4),
            max_abs_sesgo=round(max_sesgo, 4),
            nota=NOTA_ESCALA_MATRIZ,
        ),
        formulas=FORMULAS_MATRIZ_DESEMPENO,
        cobertura=(
            CoberturaModelo(**(modelo_doc.get("cobertura") or {}))
            if modelo_doc.get("cobertura")
            else None
        ),
        modelo=_modelo_resumen(modelo_doc),
        calculo_ms=round(duracion_ms, 1),
    )


def _modelo_resumen(documento: dict) -> ModeloResumen:
    """Identidad y estado de la ejecución para las respuestas."""
    return ModeloResumen(
        run_id=documento["run_id"],
        variante=documento.get("variante", "baseline"),
        etiqueta=documento.get("etiqueta", ""),
        metodo=documento.get("metodo", ""),
        estado=documento.get("estado", ""),
        recomendado=bool(documento.get("recomendado")),
        horizonte=int(documento.get("horizonte", 28)),
        semilla=documento.get("semilla"),
        version_precios=documento.get("version_precios"),
        supuestos=supuestos_modelo(documento),
        importancias=documento.get("importancias") or [],
        variables=list(documento.get("variables") or []),
    )


def _resolver_segmento(
    nivel: NivelPronostico,
    *,
    tienda: str | None,
    estado: str | None,
    categoria: str | None,
    departamento: str | None,
    item: str | None,
    items: list[str] | None = None,
) -> dict:
    """Traduce el nivel y los filtros al documento de predicción que hay que leer."""
    if nivel == NivelPronostico.total:
        return {"dimension": "total", "claves": ["total"]}
    if nivel == NivelPronostico.estado:
        return {"dimension": "estado", "claves": [estado] if estado else None}
    if nivel == NivelPronostico.tienda:
        return {"dimension": "tienda", "claves": [tienda] if tienda else None}
    if nivel == NivelPronostico.categoria:
        return {"dimension": "categoria", "claves": [categoria] if categoria else None}
    if nivel == NivelPronostico.departamento:
        return {"dimension": "departamento", "claves": [departamento] if departamento else None}
    if nivel == NivelPronostico.categoria_tienda:
        if not (tienda and categoria):
            raise FiltroInvalido("El nivel categoría × tienda necesita «tienda» y «categoria».")
        return {"dimension": "categoria_tienda", "claves": [f"{categoria}|{tienda}"]}
    if nivel == NivelPronostico.departamento_tienda:
        if not (tienda and departamento):
            raise FiltroInvalido("El nivel departamento × tienda necesita «tienda» y «departamento».")
        return {"dimension": "departamento_tienda", "claves": [f"{departamento}|{tienda}"]}
    if nivel == NivelPronostico.producto:
        elegidos = [i for i in (items or ([item] if item else [])) if i]
        if not (tienda and elegidos):
            raise FiltroInvalido("El nivel producto necesita «tienda» y «item» (o «items»).")
        return {"dimension": "producto", "claves": elegidos, "tienda": tienda, "items": elegidos}
    raise FiltroInvalido(f"Nivel no soportado: {nivel.value}")


def supuestos_modelo(documento: dict) -> dict[str, str]:
    """Supuestos del manifiesto depurados contra las variables que el modelo usa de verdad.

    Los manifiestos antiguos declaraban una política de precios aunque la variante no tuviera
    variables de precio, y afirmaban que el vector publicado era entero. La descripción se
    alinea con la ejecución real y con la política de precisión vigente en lugar de repetir
    texto estático que la contradice.
    """
    supuestos = dict(documento.get("supuestos") or {})
    variables = [str(nombre) for nombre in (documento.get("variables") or [])]
    usa_precios = any(nombre.startswith("precio") or nombre == "tiene_precio" for nombre in variables)
    if not usa_precios:
        supuestos["precio_futuro"] = (
            "No usa precios: esta variante no incluye variables de precio, así que no congela ni "
            "supone precios futuros. El precio se congelaba solo en la variante con precios, que "
            "está identificada por separado."
        )
    supuestos["redondeo"] = (
        "No se redondea antes de calcular las métricas ni al publicar: la serie canónica conserva "
        "decimales (política decimal-v1) y el redondeo se aplica solo al presentar."
    )
    return supuestos


def _ultimo_dia_observado(repositorio, version: str) -> int:
    """Último día con ventas observadas en la versión publicada."""
    documento = repositorio._db["m5_agregados"].find_one(
        {"version": version}, {"d_fin": 1}, sort=[("d_fin", -1)]
    )
    return int((documento or {}).get("d_fin") or 0)


def _resolver_origen(repositorio, version: str, run_id: str, origen: str | None) -> int:
    """Origen solicitado (``final`` o un ``d_N`` con predicciones publicadas)."""
    disponibles = sorted(
        {
            int(documento["origen_d"])
            for documento in repositorio._db["m5_predicciones"].find(
                {"run_id": run_id, "version": version}, {"origen_d": 1}
            )
        }
    )
    if not disponibles:
        raise DatosPendientes()
    if origen in (None, "", "final"):
        return disponibles[-1]
    try:
        solicitado = int(origen)
    except ValueError as exc:
        raise FiltroInvalido(f"Origen no válido: «{origen}». Usa «final» o un d_N.") from exc
    if solicitado not in disponibles:
        raise FiltroInvalido(
            f"El origen d_{solicitado} no tiene predicciones publicadas. "
            f"Disponibles: {', '.join(f'd_{d}' for d in disponibles)}."
        )
    return solicitado
