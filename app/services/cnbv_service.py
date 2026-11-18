"""Servicio del comparativo bancario con datos reales de la CNBV.

Reglas de negocio (todas verificables en los datos importados)

1. La unidad es la del boletín (millones de pesos); no se convierte.
2. El agregado del sector es la columna **Sistema** publicada por la CNBV; las
   participaciones se calculan contra ese valor y nunca sumando instituciones
   para el total.
3. Los conceptos de ``naturaleza = flujo_acumulado`` (estado de resultado
   integral) se comparan solo contra **el mismo mes del año anterior**, porque el
   boletín los publica acumulados del ejercicio.
4. Los conceptos de ``naturaleza = saldo`` se comparan contra el periodo
   importado inmediatamente anterior.
5. No se suman saldos a través del tiempo ni se promedian porcentajes. La
   mediana que se devuelve es de **valores**, no de porcentajes.
6. Ausente ≠ cero: si una institución no tiene el concepto en el periodo, se
   informa como ausente y no entra en los cálculos.
"""

from __future__ import annotations

import logging
import statistics
from datetime import date, datetime, timezone

from fastapi import HTTPException, status

from app.cnbv import conceptos as catalogo_conceptos
from app.cnbv import sources
from app.config import Settings
from app.models.cnbv import (
    CnbvBoletin,
    CnbvCatalogo,
    CnbvComparacion,
    CnbvConcepto,
    CnbvConsulta,
    CnbvDetalle,
    CnbvDetalleFila,
    CnbvFilaInstitucion,
    CnbvFiltros,
    CnbvFuente,
    CnbvIndicador,
    CnbvIndicadores,
    CnbvInstitucion,
    CnbvKpis,
    CnbvPuntoSerie,
    CnbvResumen,
    CnbvSerieInstitucion,
    CnbvSeries,
)
from app.repositories.cnbv_repository import CnbvRepository

logger = logging.getLogger(__name__)

NO_ENCONTRADO = status.HTTP_404_NOT_FOUND
NO_PROCESABLE = status.HTTP_422_UNPROCESSABLE_CONTENT


def _redondear(valor: float | None, decimales: int = 2) -> float | None:
    return None if valor is None else round(valor, decimales)


def _a_fecha(valor):
    """MongoDB devuelve ``datetime`` a medianoche; la API expone ``date``."""
    return valor.date() if isinstance(valor, datetime) else valor


def _variacion_pct(actual: float | None, anterior: float | None) -> float | None:
    if actual is None or anterior is None or anterior == 0:
        return None
    return round(100.0 * (actual - anterior) / abs(anterior), 2)


class CnbvService:
    def __init__(self, repo: CnbvRepository, settings: Settings) -> None:
        self._repo = repo
        self._settings = settings

    # -------------------------------------------------------------- catálogo
    def fuente(self) -> CnbvFuente:
        return CnbvFuente(**sources.detalle_fuente())

    def _boletin_modelo(self, documento: dict) -> CnbvBoletin:
        return CnbvBoletin(
            periodo=documento["periodo"],
            fecha=_a_fecha(documento["fecha"]),
            archivo=documento.get("archivo", ""),
            url=documento.get("url", ""),
            sha256=documento.get("sha256", ""),
            revision=int(documento.get("revision", 1)),
            bytes=int(documento.get("bytes", 0)),
            titulo_hoja=documento.get("titulo_hoja"),
            unidad_declarada=documento.get("unidad_declarada"),
            importado_en=documento.get("importado_en"),
            instituciones=int(documento.get("instituciones", 0)),
            conceptos=int(documento.get("conceptos", 0)),
            hechos=int(documento.get("hechos", 0)),
            indicadores=int(documento.get("indicadores", 0)),
            celdas_ausentes=int(documento.get("celdas_ausentes", 0)),
            rechazados=int(documento.get("rechazados", 0)),
        )

    def catalogo(self, dataset_id: str = sources.DATASET_ID) -> CnbvCatalogo:
        boletines = self._repo.boletines(dataset_id)
        if not boletines:
            raise HTTPException(
                status_code=NO_ENCONTRADO,
                detail=(
                    "No hay boletines de la CNBV importados. Ejecuta "
                    "`python -m scripts.importar_cnbv --descargar`."
                ),
            )
        conceptos = self._repo.conceptos(dataset_id)
        instituciones = self._repo.instituciones(dataset_id)
        disponibles = {concepto["concepto_id"] for concepto in conceptos}

        return CnbvCatalogo(
            fuente=self.fuente(),
            boletines=[self._boletin_modelo(documento) for documento in boletines],
            periodos=sorted(documento["periodo"] for documento in boletines),
            conceptos=[
                CnbvConcepto(
                    concepto_id=concepto["concepto_id"],
                    nombre=catalogo_conceptos.nombre_visible(
                        concepto["concepto_id"], concepto["nombre"]
                    ),
                    seccion=concepto["seccion"],
                    naturaleza=concepto["naturaleza"],
                    unidad=concepto["unidad"],
                    orden=concepto["orden"],
                    periodos_con_dato=concepto["periodos_con_dato"],
                    destacado=catalogo_conceptos.es_destacado(concepto["concepto_id"]),
                )
                for concepto in conceptos
            ],
            instituciones=[
                CnbvInstitucion(
                    institucion_id=institucion["institucion_id"],
                    nombre=institucion["nombre"],
                    es_sistema=institucion["es_sistema"],
                    periodos_con_dato=institucion["periodos_con_dato"],
                )
                for institucion in instituciones
            ],
            indicadores=self._repo.indicadores_catalogo(dataset_id),
            cobertura={
                **self._repo.resumen_cobertura(dataset_id),
                "tipos_cartera": catalogo_conceptos.grupos_de_cartera(disponibles),
                "concepto_por_omision": catalogo_conceptos.CONCEPTO_POR_OMISION,
                "conceptos_destacados": list(catalogo_conceptos.CONCEPTOS_DESTACADOS.values()),
            },
            advertencias=list(sources.ADVERTENCIAS_METRICA),
        )

    # ----------------------------------------------------------- utilidades
    def _concepto_modelo(self, dataset_id: str, concepto_id: str) -> tuple[dict, CnbvConcepto]:
        documento = self._repo.concepto(dataset_id, concepto_id)
        if documento is None:
            raise HTTPException(
                status_code=NO_PROCESABLE,
                detail=(
                    f"El concepto {concepto_id!r} no existe en los boletines importados. "
                    "Consulta GET /api/v1/cnbv/banca-multiple/catalogo."
                ),
            )
        return documento, CnbvConcepto(
            concepto_id=documento["concepto_id"],
            nombre=catalogo_conceptos.nombre_visible(
                documento["concepto_id"], documento["nombre"]
            ),
            seccion=documento["seccion"],
            naturaleza=documento["naturaleza"],
            unidad=documento["unidad"],
            orden=documento["orden"],
            periodos_con_dato=documento["periodos_con_dato"],
            destacado=catalogo_conceptos.es_destacado(documento["concepto_id"]),
        )

    def _boletin(self, dataset_id: str, periodo: str) -> tuple[dict, CnbvBoletin]:
        documento = self._repo.boletin(dataset_id, periodo)
        if documento is None:
            periodos = self._repo.periodos(dataset_id)
            raise HTTPException(
                status_code=NO_PROCESABLE,
                detail=(
                    f"El periodo {periodo!r} no está importado. Periodos disponibles: "
                    f"{', '.join(periodos) or 'ninguno'}."
                ),
            )
        return documento, self._boletin_modelo(documento)

    @staticmethod
    def _periodo_comparable(
        concepto: CnbvConcepto, periodo: str, periodos_con_dato: list[str]
    ) -> tuple[str | None, str, str | None]:
        """Periodo con el que sí se puede comparar, y por qué."""
        if concepto.naturaleza == "flujo_acumulado":
            anio, mes = periodo.split("-")
            objetivo = f"{int(anio) - 1:04d}-{mes}"
            if objetivo in periodos_con_dato:
                return objetivo, "mismo_mes_anio_anterior", None
            return (
                None,
                "mismo_mes_anio_anterior",
                "No hay boletín importado del mismo mes del año anterior: el concepto es "
                "un flujo acumulado y no se compara contra el mes inmediato anterior.",
            )

        anteriores = [p for p in periodos_con_dato if p < periodo]
        if anteriores:
            return max(anteriores), "periodo_anterior", None
        return (
            None,
            "periodo_anterior",
            "No hay un periodo anterior importado para este concepto.",
        )

    # -------------------------------------------------------------- resumen
    def resumen(self, filtros: CnbvFiltros) -> CnbvResumen:
        dataset_id = filtros.dataset_id
        documento_concepto, concepto = self._concepto_modelo(dataset_id, filtros.concepto)
        documento_boletin, boletin = self._boletin(dataset_id, filtros.periodo)

        hechos = self._repo.valores_periodo(dataset_id, filtros.periodo, filtros.concepto)
        if not hechos:
            raise HTTPException(
                status_code=NO_ENCONTRADO,
                detail=(
                    "El boletín del periodo existe pero no trae valores para ese concepto."
                ),
            )

        instituciones_periodo = {
            fila["institucion_id"]: fila
            for fila in self._repo.instituciones(dataset_id, filtros.periodo)
        }
        valores = {fila["institucion_id"]: fila for fila in hechos}

        valor_sistema = None
        for institucion_id, fila in valores.items():
            if fila["es_sistema"]:
                valor_sistema = float(fila["valor"])
                break

        # El bloque de indicadores describe SIEMPRE al sector completo; el filtro
        # de instituciones solo limita las filas que se muestran.
        todas = [fila for fila in hechos if not fila["es_sistema"]]
        todas.sort(key=lambda fila: float(fila["valor"]), reverse=True)

        instituciones = [
            fila
            for fila in todas
            if not filtros.instituciones or fila["institucion_id"] in filtros.instituciones
        ]

        periodos_con_dato = self._repo.periodos_con_dato(dataset_id, filtros.concepto)
        periodo_comparable, tipo_comparacion, motivo = self._periodo_comparable(
            concepto, filtros.periodo, periodos_con_dato
        )

        comparados: dict[str, float] = {}
        valor_sistema_comparable = None
        if periodo_comparable:
            for fila in self._repo.valores_periodo(dataset_id, periodo_comparable, filtros.concepto):
                comparados[fila["institucion_id"]] = float(fila["valor"])
                if fila["es_sistema"]:
                    valor_sistema_comparable = float(fila["valor"])

        filas: list[CnbvFilaInstitucion] = []
        if filtros.incluir_sistema and valor_sistema is not None:
            filas.append(
                CnbvFilaInstitucion(
                    institucion_id="sistema",
                    institucion="Sistema (agregado publicado por la CNBV)",
                    es_sistema=True,
                    valor=valor_sistema,
                    participacion_pct=100.0,
                    valor_periodo_comparado=valor_sistema_comparable,
                    variacion=(
                        None
                        if valor_sistema_comparable is None
                        else round(valor_sistema - valor_sistema_comparable, 6)
                    ),
                    variacion_pct=_variacion_pct(valor_sistema, valor_sistema_comparable),
                    posicion=None,
                )
            )

        for posicion, fila in enumerate(todas, start=1):
            if filtros.instituciones and fila["institucion_id"] not in filtros.instituciones:
                continue
            valor = float(fila["valor"])
            anterior = comparados.get(fila["institucion_id"])
            filas.append(
                CnbvFilaInstitucion(
                    institucion_id=fila["institucion_id"],
                    institucion=fila["institucion_nombre"],
                    es_sistema=False,
                    valor=valor,
                    participacion_pct=_redondear(
                        100.0 * valor / valor_sistema if valor_sistema else None
                    ),
                    valor_periodo_comparado=anterior,
                    variacion=None if anterior is None else round(valor - anterior, 6),
                    variacion_pct=_variacion_pct(valor, anterior),
                    posicion=posicion,
                )
            )

        valores_instituciones = [float(fila["valor"]) for fila in todas]
        top5 = sum(valores_instituciones[:5])
        mayor = todas[0] if todas else None

        kpis = CnbvKpis(
            valor_sistema=valor_sistema,
            unidad=concepto.unidad,
            naturaleza=concepto.naturaleza,
            instituciones_con_dato=len(todas),
            instituciones_sin_dato=max(
                0,
                len(
                    [
                        institucion_id
                        for institucion_id, fila in instituciones_periodo.items()
                        if not fila["es_sistema"]
                        and institucion_id not in valores
                    ]
                ),
            ),
            participacion_mayor_pct=_redondear(
                100.0 * float(mayor["valor"]) / valor_sistema
                if mayor is not None and valor_sistema
                else None
            ),
            institucion_mayor=mayor["institucion_nombre"] if mayor else None,
            concentracion_top5_pct=_redondear(
                100.0 * top5 / valor_sistema if valor_sistema else None
            ),
            mediana_instituciones=(
                _redondear(statistics.median(valores_instituciones), 6)
                if valores_instituciones
                else None
            ),
            maximo_institucion=max(valores_instituciones) if valores_instituciones else None,
            minimo_institucion=min(valores_instituciones) if valores_instituciones else None,
        )

        comparacion = CnbvComparacion(
            periodo_comparado=periodo_comparable,
            tipo=tipo_comparacion,
            valor_sistema_periodo_comparado=valor_sistema_comparable,
            variacion=(
                None
                if valor_sistema is None or valor_sistema_comparable is None
                else round(valor_sistema - valor_sistema_comparable, 6)
            ),
            variacion_sistema_pct=_variacion_pct(valor_sistema, valor_sistema_comparable),
            motivo=motivo,
        )

        advertencias = list(sources.ADVERTENCIAS_METRICA)
        if filtros.instituciones:
            advertencias.append(
                "El filtro de instituciones solo limita las filas de la tabla: los "
                "indicadores del bloque superior describen al sector completo del boletín."
            )
        if concepto.naturaleza == "flujo_acumulado":
            advertencias.append(
                "El concepto seleccionado es un flujo acumulado del ejercicio: la "
                "comparación válida es contra el mismo mes del año anterior."
            )
        if kpis.instituciones_sin_dato:
            advertencias.append(
                f"{kpis.instituciones_sin_dato} institución(es) del periodo no publican "
                "este concepto; no se imputan valores."
            )
        if valor_sistema is None:
            advertencias.append(
                "El boletín no publica el agregado del sistema para este concepto."
            )

        return CnbvResumen(
            fuente=self.fuente(),
            boletin=boletin,
            filtros=filtros.normalized(),
            concepto=concepto,
            periodo=filtros.periodo,
            fecha=documento_boletin["fecha"],
            kpis=kpis,
            comparacion=comparacion,
            por_institucion=filas,
            serie=[
                CnbvPuntoSerie(
                    periodo=punto["periodo"],
                    fecha=_a_fecha(punto["fecha"]),
                    valor=punto["valor"],
                    valor_sistema=punto["valor"],
                    instituciones_con_dato=punto["instituciones_con_dato"],
                )
                for punto in self._serie_sistema(dataset_id, filtros.concepto)
            ],
            valores_ausentes={
                "celdas_ausentes_boletin": self._repo.celda_ausentes(dataset_id, filtros.periodo),
                "instituciones_sin_dato": kpis.instituciones_sin_dato,
                "nota": (
                    "Celdas vacías en el archivo original. No se convierten en cero ni se "
                    "imputan."
                ),
            },
            cobertura=self._repo.resumen_cobertura(dataset_id),
            advertencias=advertencias,
            generado_en=datetime.now(timezone.utc),
        )

    # ---------------------------------------------------------------- series
    @staticmethod
    def _conteos_por_periodo(hechos: list[dict]) -> dict[str, int]:
        """Instituciones con dato por periodo, en una sola pasada."""
        conteos: dict[str, int] = {}
        for hecho in hechos:
            if hecho["es_sistema"]:
                continue
            conteos[hecho["periodo"]] = conteos.get(hecho["periodo"], 0) + 1
        return conteos

    def _serie_sistema(self, dataset_id: str, concepto_id: str) -> list[dict]:
        """Serie del agregado del sector en una sola consulta."""
        hechos = self._repo.serie(dataset_id, concepto_id)
        conteos = self._conteos_por_periodo(hechos)
        puntos: dict[str, dict] = {}
        for hecho in hechos:
            if not hecho["es_sistema"]:
                continue
            puntos[hecho["periodo"]] = {
                "periodo": hecho["periodo"],
                "fecha": hecho["fecha"],
                "valor": float(hecho["valor"]),
                "instituciones_con_dato": conteos.get(hecho["periodo"], 0),
            }
        return [puntos[periodo] for periodo in sorted(puntos)]

    def _contar_instituciones(self, dataset_id: str, concepto_id: str, periodo: str) -> int:
        return sum(
            1
            for fila in self._repo.valores_periodo(dataset_id, periodo, concepto_id)
            if not fila["es_sistema"]
        )

    def series(
        self,
        concepto_id: str,
        *,
        instituciones: tuple[str, ...] = (),
        desde: str | None = None,
        hasta: str | None = None,
        dataset_id: str = sources.DATASET_ID,
    ) -> CnbvSeries:
        _, concepto = self._concepto_modelo(dataset_id, concepto_id)
        hechos = self._repo.serie(dataset_id, concepto_id, instituciones, desde, hasta)
        if not hechos:
            raise HTTPException(
                status_code=NO_ENCONTRADO,
                detail="No hay datos para ese concepto con los filtros indicados.",
            )

        sistemas = {h["periodo"]: float(h["valor"]) for h in hechos if h["es_sistema"]}
        conteos = self._conteos_por_periodo(hechos)
        if instituciones:
            # Si hay selección, el conteo de instituciones sigue siendo del sector.
            conteos = self._conteos_por_periodo(
                self._repo.serie(dataset_id, concepto_id, (), desde, hasta)
            )
        por_institucion: dict[str, list[dict]] = {}
        for hecho in hechos:
            por_institucion.setdefault(hecho["institucion_id"], []).append(hecho)

        series: list[CnbvSerieInstitucion] = []
        for institucion_id, lista in sorted(
            por_institucion.items(),
            key=lambda par: (not par[1][0]["es_sistema"], par[1][0]["institucion_nombre"]),
        ):
            puntos: dict[str, dict] = {hecho["periodo"]: hecho for hecho in lista}
            series.append(
                CnbvSerieInstitucion(
                    institucion_id=institucion_id,
                    institucion=lista[0]["institucion_nombre"],
                    es_sistema=bool(lista[0]["es_sistema"]),
                    puntos=[
                        CnbvPuntoSerie(
                            periodo=periodo,
                            fecha=_a_fecha(puntos[periodo]["fecha"]),
                            valor=float(puntos[periodo]["valor"]),
                            valor_sistema=sistemas.get(periodo),
                            instituciones_con_dato=conteos.get(periodo, 0),
                        )
                        for periodo in sorted(puntos)
                    ],
                )
            )

        advertencias = list(sources.ADVERTENCIAS_METRICA)
        if concepto.naturaleza == "flujo_acumulado":
            advertencias.append(
                "Serie de un flujo acumulado del ejercicio: cada punto acumula de enero al "
                "mes indicado; no se suman entre sí."
            )

        return CnbvSeries(
            fuente=self.fuente(),
            filtros={
                "dataset_id": dataset_id,
                "concepto": concepto_id,
                "instituciones": list(instituciones),
                "periodo_desde": desde,
                "periodo_hasta": hasta,
            },
            concepto=concepto,
            periodos=sorted({hecho["periodo"] for hecho in hechos}),
            series=series,
            advertencias=advertencias,
            generado_en=datetime.now(timezone.utc),
        )

    # --------------------------------------------------------------- detalle
    def detalle(
        self,
        *,
        periodo: str | None,
        concepto: str | None,
        instituciones: tuple[str, ...],
        seccion: str | None,
        consulta: CnbvConsulta,
        dataset_id: str = sources.DATASET_ID,
    ) -> CnbvDetalle:
        filtro: dict = {"dataset_id": dataset_id, "vigente": True}
        if periodo:
            self._boletin(dataset_id, periodo)
            filtro["periodo"] = periodo
        if concepto:
            self._concepto_modelo(dataset_id, concepto)
            filtro["concepto_id"] = concepto
        if instituciones:
            filtro["institucion_id"] = {"$in": list(instituciones)}
        if seccion:
            filtro["seccion"] = seccion

        total = self._repo.contar(filtro)
        paginas = (total + consulta.page_size - 1) // consulta.page_size if total else 0
        skip = (consulta.page - 1) * consulta.page_size
        items: list[CnbvDetalleFila] = []
        if total and skip < total:
            documentos = self._repo.detalle(
                filtro, orden=consulta.orden, skip=skip, limit=consulta.page_size
            )
            items = [
                CnbvDetalleFila(
                    periodo=documento["periodo"],
                    fecha=_a_fecha(documento["fecha"]),
                    concepto_id=documento["concepto_id"],
                    concepto=catalogo_conceptos.nombre_visible(
                        documento["concepto_id"], documento["concepto_nombre"]
                    ),
                    seccion=documento["seccion"],
                    naturaleza=documento["naturaleza"],
                    institucion_id=documento["institucion_id"],
                    institucion=documento["institucion_nombre"],
                    es_sistema=documento["es_sistema"],
                    valor=float(documento["valor"]),
                    unidad=documento["unidad"],
                    moneda=documento.get("moneda", sources.MONEDA),
                    archivo=documento.get("archivo", ""),
                    fila_origen=int(documento.get("fila", 0)),
                )
                for documento in documentos
            ]

        advertencias = [sources.ADVERTENCIAS_METRICA[0], sources.ADVERTENCIAS_METRICA[1]]
        if not total:
            advertencias.append("Ningún registro coincide con los filtros indicados.")

        return CnbvDetalle(
            items=items,
            total=total,
            page=consulta.page,
            page_size=consulta.page_size,
            pages=paginas,
            orden=consulta.orden,
            filtros={
                "dataset_id": dataset_id,
                "periodo": periodo,
                "concepto": concepto,
                "instituciones": list(instituciones),
                "seccion": seccion,
            },
            unidad=sources.UNIDAD_SALDOS,
            advertencias=advertencias,
            generado_en=datetime.now(timezone.utc),
        )

    # ----------------------------------------------------------- indicadores
    def indicadores(
        self, *, periodo_boletin: str | None = None, dataset_id: str = sources.DATASET_ID
    ) -> CnbvIndicadores:
        periodos = self._repo.periodos(dataset_id)
        if not periodos:
            raise HTTPException(status_code=NO_ENCONTRADO, detail="No hay boletines importados.")
        objetivo = periodo_boletin or periodos[-1]
        self._boletin(dataset_id, objetivo)

        documentos = self._repo.indicadores(dataset_id, objetivo)
        agrupados: dict[str, list[dict]] = {}
        for documento in documentos:
            agrupados.setdefault(documento["indicador_id"], []).append(documento)

        indicadores = [
            CnbvIndicador(
                indicador_id=indicador_id,
                nombre=lista[0]["indicador_nombre"],
                nota=lista[0].get("nota"),
                unidad=lista[0]["unidad"],
                periodo=objetivo,
                valores=[
                    {
                        "periodo": documento["periodo"],
                        "fecha": documento["fecha"],
                        "institucion_id": documento["institucion_id"],
                        "institucion": documento["institucion_nombre"],
                        "es_sistema": documento["es_sistema"],
                        "valor": documento["valor"],
                        "unidad": documento["unidad"],
                    }
                    for documento in lista
                ],
            )
            for indicador_id, lista in sorted(agrupados.items())
        ]

        return CnbvIndicadores(
            fuente=self.fuente(),
            periodo_boletin=objetivo,
            indicadores=indicadores,
            advertencias=[
                "Indicadores publicados por la CNBV; no se recalculan ni se promedian.",
                "ROA y ROE del boletín se calculan sobre flujo de 12 meses, según su nota.",
                *[a for a in sources.ADVERTENCIAS_METRICA if "Porcentajes" in a],
            ],
            generado_en=datetime.now(timezone.utc),
        )
