"""Pruebas del parser del boletín CNBV con un archivo construido a mano.

El archivo de prueba reproduce la estructura real del boletín (fila de
encabezado variable, columna ``Sistema``, conceptos repetidos, notas al pie y
celdas vacías) para fijar las reglas del parser sin depender de la red.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest

from app.cnbv import sources
from app.cnbv.parser import (
    ErrorFormatoFuente,
    normalizar_concepto,
    parsear_boletin,
    slug_concepto,
)

PERIODO = "202410"


def _libro(tmp_path: Path, *, fila_cabecera: int = 5, titulo: str | None = None, fila_fechas=()) -> Path:
    """Construye un xlsx con la forma del boletín real."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    libro = openpyxl.Workbook()
    hoja = libro.active
    hoja.title = "EF Resumen"

    cabecera = fila_cabecera
    hoja.cell(row=cabecera, column=2, value=titulo or "Octubre de 2024\nMillones de pesos")
    hoja.cell(row=cabecera, column=3, value="Sistema")
    hoja.cell(row=cabecera, column=4, value="Banco Uno")
    hoja.cell(row=cabecera, column=5, value="Banco Dos")

    filas = [
        (cabecera + 2, "Estado de Situación Financiera", None, None, None),
        (cabecera + 3, "ACTIVO", 1000.5, 400.25, 200.50),
        (cabecera + 4, "Efectivo y Equivalentes de Efectivo", 100.0, 40.0, 20.0),
        (cabecera + 5, "Créditos Comerciales", 300.0, 120.0, 60.0),
        (cabecera + 6, "Créditos Comerciales", 30.0, 12.0, 6.0),
        (cabecera + 7, "Capital Social*", 50.0, 20.0, None),  # celda vacía
        (cabecera + 8, "Estado de Resultado Integral", None, None, None),
        (cabecera + 9, "Resultado Neto", 90.0, 30.0, 10.0),
        (cabecera + 10, "(1) Nota al pie que no es concepto", None, None, None),
    ]
    for fila, concepto, sistema, uno, dos in filas:
        hoja.cell(row=fila, column=2, value=concepto)
        for columna, valor in ((3, sistema), (4, uno), (5, dos)):
            if valor is not None:
                hoja.cell(row=fila, column=columna, value=valor)

    indicadores = libro.create_sheet("Indicadores")
    indicadores.cell(row=2, column=2, value="Indicadores financieros")
    indicadores.cell(row=3, column=2, value="Porcentajes")
    indicadores.cell(row=fila_cabecera, column=3, value="ROA  (Flujo 12 meses)  3/")
    indicadores.cell(row=fila_cabecera, column=6, value="ROE  (Flujo 12 meses)  4/")
    fechas = fila_fechas or (
        datetime(2023, 10, 1),
        datetime(2024, 9, 1),
        datetime(2024, 10, 1),
    )
    for desplazamiento, fecha in enumerate(fechas):
        indicadores.cell(row=fila_cabecera + 1, column=3 + desplazamiento, value=fecha)
        indicadores.cell(row=fila_cabecera + 1, column=6 + desplazamiento, value=fecha)
    indicadores.cell(row=fila_cabecera + 2, column=2, value="Sistema  */")
    for columna, valor in ((3, 2.08), (4, 2.09), (5, 2.10), (6, 18.6), (7, 18.5), (8, 18.4)):
        indicadores.cell(row=fila_cabecera + 2, column=columna, value=valor)
    indicadores.cell(row=fila_cabecera + 3, column=2, value="Banco Uno")
    for columna, valor in ((3, 3.11), (4, 3.00), (5, 2.97), (6, 27.6), (7, 26.3), (8, 26.2)):
        indicadores.cell(row=fila_cabecera + 3, column=columna, value=valor)
    indicadores.cell(row=fila_cabecera + 4, column=2, value="Banco Dos")

    ruta = tmp_path / f"BE_BM_{PERIODO}.xlsx"
    libro.save(ruta)
    return ruta


def test_parsea_el_boletin_y_separa_secciones(tmp_path):
    resultado = parsear_boletin(_libro(tmp_path), PERIODO)

    assert resultado.periodo == PERIODO
    assert resultado.fecha == date(2024, 10, 1)
    assert "Millones de pesos" in resultado.unidad_declarada
    assert len(resultado.instituciones) == 3
    assert resultado.instituciones[0]["es_sistema"] is True

    conceptos = {concepto["concepto_id"]: concepto for concepto in resultado.conceptos}
    assert conceptos["activo#1"]["seccion"] == "balance"
    assert conceptos["activo#1"]["naturaleza"] == "saldo"
    assert conceptos["resultado_neto#1"]["seccion"] == "resultados"
    assert conceptos["resultado_neto#1"]["naturaleza"] == "flujo_acumulado"
    assert "creditos_comerciales#1" in conceptos
    assert "creditos_comerciales#2" in conceptos  # concepto repetido, distinta etapa
    assert conceptos["capital_social#1"]["nombre_original"] == "Capital Social*"


def test_los_valores_se_guardan_tal_cual(tmp_path):
    resultado = parsear_boletin(_libro(tmp_path), PERIODO)
    activo = {
        hecho["institucion_id"]: hecho["valor"]
        for hecho in resultado.hechos
        if hecho["concepto_id"] == "activo#1"
    }
    assert activo == {"sistema": 1000.5, "banco_uno": 400.25, "banco_dos": 200.5}
    for hecho in resultado.hechos:
        assert hecho["unidad"] == sources.UNIDAD_SALDOS
        assert hecho["periodo"] == "2024-10"


def test_las_celdas_vacias_no_se_convierten_en_cero(tmp_path):
    resultado = parsear_boletin(_libro(tmp_path), PERIODO)
    capital = [
        hecho for hecho in resultado.hechos if hecho["concepto_id"] == "capital_social#1"
    ]
    # Banco Dos no publica la celda: no hay hecho para esa institución.
    assert {hecho["institucion_id"] for hecho in capital} == {"sistema", "banco_uno"}
    assert resultado.ausentes >= 1


def test_las_notas_al_pie_se_rechazan_e_informan(tmp_path):
    resultado = parsear_boletin(_libro(tmp_path), PERIODO)
    motivos = {rechazo["motivo"] for rechazo in resultado.rechazados}
    assert "nota_al_pie" in motivos
    conceptos = {concepto["concepto_id"] for concepto in resultado.conceptos}
    assert not any("nota_al_pie" in concepto for concepto in conceptos)


def test_detecta_la_fila_de_encabezado_sea_cual_sea_su_posicion(tmp_path):
    alto = parsear_boletin(_libro(tmp_path / "a", fila_cabecera=8), PERIODO)
    bajo = parsear_boletin(_libro(tmp_path / "b", fila_cabecera=5), PERIODO)
    valores_alto = sorted(
        (hecho["concepto_id"], hecho["institucion_id"], hecho["valor"])
        for hecho in alto.hechos
    )
    valores_bajo = sorted(
        (hecho["concepto_id"], hecho["institucion_id"], hecho["valor"])
        for hecho in bajo.hechos
    )
    assert valores_alto == valores_bajo


def test_rechaza_un_archivo_sin_unidad_declarada(tmp_path):
    ruta = _libro(tmp_path, titulo="Octubre de 2024")
    with pytest.raises(ErrorFormatoFuente, match="Millones de pesos"):
        parsear_boletin(ruta, PERIODO)


def test_rechaza_un_titulo_que_contradice_al_archivo(tmp_path):
    ruta = _libro(tmp_path, titulo="Enero de 2024\nMillones de pesos")
    with pytest.raises(ErrorFormatoFuente, match="título"):
        parsear_boletin(ruta, PERIODO)


def test_avisa_si_el_titulo_tiene_erratas(tmp_path):
    # La fuente real publica "Septimebre de 2024" en el boletín de septiembre.
    ruta = _libro(tmp_path, titulo="Septimebre de 2024\nMillones de pesos")
    resultado = parsear_boletin(ruta, "202409")
    assert any("título" in aviso for aviso in resultado.avisos)


def test_importa_los_indicadores_publicados(tmp_path):
    resultado = parsear_boletin(_libro(tmp_path), PERIODO)
    indicadores = {i["indicador_id"] for i in resultado.indicadores}
    assert indicadores == {"roa_flujo_12_meses", "roe_flujo_12_meses"}
    sistema = [i for i in resultado.indicadores if i["institucion_id"] == "sistema"]
    assert {i["periodo"] for i in sistema} == {"2023-10", "2024-09", "2024-10"}
    for indicador in resultado.indicadores:
        assert indicador["unidad"] == sources.UNIDAD_INDICADORES
        assert indicador["nota"] == "Flujo 12 meses"


def test_normalizacion_de_conceptos():
    assert normalizar_concepto("Capital Social*") == "capital social"
    assert normalizar_concepto("  Créditos  Comerciales ") == "creditos comerciales"
    assert slug_concepto("Partidas Diferidas (2)") == "partidas_diferidas"
    assert slug_concepto("Instrumentos Financieros para Cobrar Principal e Interés (valores)(neto)") == (
        "instrumentos_financieros_para_cobrar_principal_e_interes_valores_neto"
    )


def test_no_se_inventan_urls_de_periodos_no_verificados():
    with pytest.raises(ValueError, match="verificada"):
        sources.archivo_fuente("199901")
    archivo = sources.archivo_fuente("202410")
    assert archivo.nombre == "BE_BM_202410.xlsx"
    assert archivo.url == (
        "https://portafolioinfo.cnbv.gob.mx/PortafolioInformacion/BE_BM_202410.xlsx"
    )
