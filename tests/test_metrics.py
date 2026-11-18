"""Pruebas de las reglas de negocio de las métricas (sin base de datos)."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.metrics import (
    Periodo,
    line_amounts,
    margin_pct,
    previous_period,
    ticket_medio,
    variation_pct,
    variation_points,
)


def test_line_amounts_calcula_los_cinco_importes():
    importes = line_amounts(
        cantidad=5,
        precio_unitario_cent=10_000,
        descuento_cent=2_500,
        coste_unitario_cent=6_000,
    )
    assert importes == {
        "venta_bruta_cent": 50_000,
        "descuento_cent": 2_500,
        "venta_neta_cent": 47_500,
        "coste_cent": 30_000,
        "margen_cent": 17_500,
    }


def test_line_amounts_sin_descuento():
    importes = line_amounts(2, 10_000, 0, 6_000)
    assert importes["venta_neta_cent"] == importes["venta_bruta_cent"] == 20_000
    assert importes["margen_cent"] == 8_000


def test_line_amounts_rechaza_descuento_mayor_que_la_venta_bruta():
    with pytest.raises(ValueError):
        line_amounts(1, 1_000, 1_001, 500)


@pytest.mark.parametrize(
    ("cantidad", "precio", "descuento", "coste"),
    [
        (-1, 1_000, 0, 500),
        (1, -1_000, 0, 500),
        (1, 1_000, -1, 500),
        (1, 1_000, 0, -500),
    ],
)
def test_line_amounts_rechaza_negativos(cantidad, precio, descuento, coste):
    with pytest.raises(ValueError):
        line_amounts(cantidad, precio, descuento, coste)


def test_margen_porcentaje_desde_sumas():
    # 50.500 / 202.500 · 100 = 24,9382… -> 24,94
    assert margin_pct(202_500, 50_500) == 24.94
    # 108.500 / 312.500 · 100 = 34,72 justo
    assert margin_pct(312_500, 108_500) == 34.72


def test_margen_porcentaje_es_nulo_si_la_venta_neta_es_cero():
    assert margin_pct(0, 0) is None
    assert margin_pct(0, 12_345) is None


def test_margen_porcentaje_redondea_mitad_hacia_arriba():
    # 1 / 800 · 100 = 0,125 -> 0,13 (el redondeo bancario daría 0,12)
    assert margin_pct(800, 1) == 0.13


def test_margen_porcentaje_admite_margen_negativo():
    assert margin_pct(1_000, -250) == -25.0


def test_periodo_anterior_de_dos_dias():
    anterior = previous_period(date(2024, 2, 1), date(2024, 2, 2))
    assert anterior == Periodo(desde=date(2024, 1, 30), hasta=date(2024, 1, 31))
    assert anterior.dias == 2


def test_periodo_anterior_de_un_mes_completo():
    anterior = previous_period(date(2024, 3, 1), date(2024, 3, 31))
    assert anterior.dias == 31
    assert anterior.desde == date(2024, 1, 30)
    assert anterior.hasta == date(2024, 2, 29)  # 2024 es bisiesto


def test_periodo_anterior_de_un_solo_dia():
    anterior = previous_period(date(2024, 5, 10), date(2024, 5, 10))
    assert anterior == Periodo(desde=date(2024, 5, 9), hasta=date(2024, 5, 9))


def test_periodo_anterior_conserva_la_duracion():
    actual = Periodo(desde=date(2025, 1, 1), hasta=date(2025, 12, 31))
    anterior = previous_period(actual.desde, actual.hasta)
    assert anterior.dias == actual.dias == 365
    assert anterior.hasta == date(2024, 12, 31)


def test_variacion_porcentual():
    assert variation_pct(202_500, 110_000) == 84.09
    assert variation_pct(50_500, 58_000) == -12.93


def test_variacion_porcentual_es_nula_sin_base():
    assert variation_pct(100, 0) is None
    assert variation_pct(None, 100) is None
    assert variation_pct(100, None) is None


def test_variacion_en_puntos_porcentuales():
    assert variation_points(24.94, 52.73) == -27.79
    assert variation_points(None, 52.73) is None
    assert variation_points(24.94, None) is None


def test_ticket_medio():
    assert ticket_medio(202_500, 2) == 101_250
    assert ticket_medio(202_500, 0) is None
    assert ticket_medio(0, 0) is None


def test_ticket_medio_redondea_mitad_hacia_arriba():
    assert ticket_medio(10, 4) == 3  # 2,5 -> 3
    assert ticket_medio(10, 3) == 3  # 3,33 -> 3
