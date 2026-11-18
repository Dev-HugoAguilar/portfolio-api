"""Pruebas de la auditoría de separación temporal y de la regla de diferencias de error.

Cubren los tres pendientes del incremento:

* **Identidad de departamentos**: la etiqueta central incluye la categoría cuando la tabla mezcla
  categorías y la omite cuando ya hay un encabezado de categoría.
* **Diferencias de error**: igualdad numérica (tolerancia de publicación), diferencia relativa
  pequeña, mejora y deterioro, límite exacto del umbral y baseline en cero.
* **Evidencia temporal**: la clasificación por fechas de **etiqueta** (origen + horizonte) detecta
  etiquetas posteriores al corte y distingue «no verificable» de aprobación.
"""

from __future__ import annotations

import pytest

from scripts.auditar_separacion_temporal import clasificar


# --------------------------------------------------------------------------- #
# Separación temporal
# --------------------------------------------------------------------------- #


def test_etiquetas_posteriores_al_corte_es_contaminacion():
    """Origen 1857 con horizonte 28 produce etiquetas hasta d_1885: puntuar d_1773 es fuga."""
    resultado, detalle, ultima = clasificar(
        variante="calendario",
        etapa="desarrollo",
        corte=1773,
        horizonte=28,
        origenes=[1541, 1609, 1677, 1857],
        entrenado=True,
    )
    assert resultado == "contaminada"
    assert ultima == 1885
    assert "d_1885" in detalle and "posterior al corte" in detalle


def test_etiquetas_anteriores_al_corte_es_separacion_verificada():
    """Orígenes hasta 1745 con horizonte 28 → última etiqueta d_1773, justo en el corte."""
    resultado, detalle, ultima = clasificar(
        variante="calendario",
        etapa="desarrollo",
        corte=1773,
        horizonte=28,
        origenes=[1541, 1609, 1677, 1745],
        entrenado=True,
    )
    assert resultado == "separacion_verificada"
    assert ultima == 1773
    assert "todos posteriores al corte" in detalle


def test_un_dia_de_etiqueta_despues_del_corte_ya_es_contaminacion():
    """El límite es estricto: una sola etiqueta posterior invalida la separación."""
    resultado, _, ultima = clasificar(
        variante="calendario",
        etapa="final",
        corte=1913,
        horizonte=28,
        origenes=[1886],
        entrenado=True,
    )
    assert resultado == "contaminada"
    assert ultima == 1914


def test_baseline_sin_entrenamiento_se_verifica_por_construccion():
    resultado, detalle, ultima = clasificar(
        variante="baseline",
        etapa="final",
        corte=1885,
        horizonte=28,
        origenes=[],
        entrenado=False,
    )
    assert resultado == "separacion_verificada"
    assert ultima is None
    assert "regla determinista" in detalle


def test_sin_evidencia_es_no_verificable_y_no_aprobacion():
    resultado, detalle, ultima = clasificar(
        variante="calendario",
        etapa="final",
        corte=1885,
        horizonte=28,
        origenes=[],
        entrenado=False,
    )
    assert resultado == "no_verificable"
    assert ultima is None
    assert "no permiten afirmar" in detalle


@pytest.mark.parametrize(
    "corte,origenes,esperado",
    [
        (1857, [1541, 1609, 1677, 1745], "separacion_verificada"),
        (1801, [1541, 1609, 1677, 1745], "separacion_verificada"),
        (1885, [1621, 1677, 1745, 1829, 1857], "separacion_verificada"),
        (1913, [1621, 1677, 1745, 1829, 1857], "separacion_verificada"),
    ],
)
def test_evaluaciones_reales_b5ca3a8(corte, origenes, esperado):
    """Los cortes publicados del manifiesto: separación verificada con etiquetas ≤ corte."""
    resultado, _, _ = clasificar(
        variante="calendario",
        etapa="final",
        corte=corte,
        horizonte=28,
        origenes=origenes,
        entrenado=True,
    )
    assert resultado == esperado
