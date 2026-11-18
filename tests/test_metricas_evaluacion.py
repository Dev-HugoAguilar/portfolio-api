"""Pruebas de las métricas de evaluación y de su presentación.

Cubren los defectos corregidos en este incremento:

* el agregado diario se calculaba con vectores de distinta longitud (28 días de pronóstico frente a
  118 de observado, que incluye 90 de historia), lo que producía un agregado imposible;
* la consolidación entre cortes podía promediar porcentajes en lugar de sumar numeradores y
  denominadores;
* la mejora se presentaba con signo negativo («−23,71 % menos error») en vez de «% más error»;
* la política de precisión decía cosas distintas según el endpoint;
* el recálculo independiente debe partir de sumas por serie, no del resumen publicado.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.comercial import planificacion
from app.models.comercial import MetricasVentana
from app.services.comercial_service import _consolidar_grano, _grano_agregado
from app.models.comercial import GranoVentana
from scripts.auditar_evaluacion import _consolidar as consolidar_auditoria
from scripts.auditar_evaluacion import _mejora, _metricas


class RepositorioFalso:
    """Repositorio mínimo: devuelve los documentos de predicción indicados."""

    def __init__(self, documentos, publicacion=None):
        self._documentos = documentos
        self._publicacion = publicacion or {}
        self._db = {}

    def predictores(self, version, run_id, *, origen, dimension, claves):
        return [dict(documento) for documento in self._documentos]

    def publicacion(self):
        return self._publicacion


def _documento_total(pronostico, observado, baseline=None):
    return {
        "clave": "total",
        "dimension": "total",
        "origen_d": 1885,
        "dias": [{"d": 1886 + paso, "fecha": f"2016-04-{paso + 1:02d}"} for paso in range(28)],
        "pronostico": list(pronostico),
        "baseline": list(baseline if baseline is not None else pronostico),
        "observado": list(observado) if observado is not None else None,
    }


def test_agregado_diario_usa_el_horizonte_de_cada_vector():
    """El agregado mide 28 días: `pronostico` trae 28 valores y `observado` 28 (ya recortado).

    Antes se indexaba el observado con el desplazamiento de la historia (90 días) sobre un vector
    de 28 posiciones; el resultado era un agregado de error absurdo (42 272 unidades por día).
    """
    pronostico = np.full(28, 100.0)
    observado = np.full(28, 90.0)
    repositorio = RepositorioFalso([_documento_total(pronostico, observado)])
    grano = _grano_agregado(repositorio, "m5-prueba", "run-prueba", 1885)
    assert grano.grano == "total del segmento"
    assert grano.candidato.n == 28, "el grano agregado debe medir 28 días, no 28 series"
    assert grano.candidato.mae == pytest.approx(10.0)
    assert grano.candidato.sesgo == pytest.approx(10.0)
    assert grano.candidato.denominador == pytest.approx(90.0 * 28)
    assert grano.candidato.wape_pct == pytest.approx(100 * 10 / 90)


def test_agregado_sin_observaciones_no_inventa_metricas():
    repositorio = RepositorioFalso([_documento_total(np.full(28, 100.0), None)])
    grano = _grano_agregado(repositorio, "m5-prueba", "run-prueba", 1941)
    assert grano.candidato.n == 0
    assert "no tiene observaciones" in (grano.motivo or "")


def _grano(n, mae, sesgo, denominador, wape_pct):
    return GranoVentana(
        grano="total del segmento",
        candidato=MetricasVentana(
            n=n, mae=mae, sesgo=sesgo, denominador=denominador, wape_pct=wape_pct
        ),
        baseline=MetricasVentana(
            n=n, mae=mae * 2, sesgo=0.0, denominador=denominador, wape_pct=wape_pct * 2
        ),
    )


def test_consolidado_suma_numeradores_y_denominadores():
    """Dos cortes con distinto número de días y error: el consolidado no promedia porcentajes."""
    ventanas = [
        _grano(n=28, mae=10.0, sesgo=2.0, denominador=280.0, wape_pct=100 * 10 / 280),
        _grano(n=56, mae=20.0, sesgo=-1.0, denominador=560.0, wape_pct=100 * 20 / 560),
    ]
    consolidado = _consolidar_grano(ventanas)
    # MAE = (28·10 + 56·20) / 84; el promedio simple de MAE habría dado 15.
    assert consolidado["candidato"]["mae"] == pytest.approx((28 * 10 + 56 * 20) / 84, abs=5e-5)
    assert consolidado["candidato"]["sesgo"] == pytest.approx((28 * 2 + 56 * -1) / 84, abs=5e-5)
    assert consolidado["candidato"]["wape_pct"] == pytest.approx(
        100 * (28 * 10 + 56 * 20) / 840, abs=5e-3
    )
    assert consolidado["candidato"]["n"] == 84
    assert consolidado["ventanas"] == 2
    # El promedio simple de los WAPE sería 1,79 % y no coincide con la razón consolidada.
    promedio_simple = (100 * 10 / 280 + 100 * 20 / 560) / 2
    assert consolidado["candidato"]["wape_pct"] != pytest.approx(promedio_simple)


def test_mejora_tiene_signo_y_se_lee_en_palabras():
    """Positivo = menos error, negativo = más error; sin signos delante del porcentaje."""
    mejor = _mejora({"mae": 1.07}, {"mae": 1.2382})
    assert mejor["tipo"] == "mejor"
    assert mejor["pct"] == pytest.approx(13.584, abs=1e-3)

    peor = _mejora({"mae": 3997.3064}, {"mae": 3231.1429})
    assert peor["tipo"] == "peor"
    assert peor["pct"] == pytest.approx(-23.712, abs=1e-3)

    empate = _mejora({"mae": 1.24}, {"mae": 1.24})
    assert empate["tipo"] == "sin_diferencia_numerica" and empate["pct"] == 0

    # Diferencia relativa pequeña: no es igualdad numérica, pero no alcanza el umbral de 0,05 %.
    pequena = _mejora({"mae": 199.95}, {"mae": 200.0})
    assert pequena["tipo"] == "diferencia_pequena"
    assert abs(pequena["pct"]) < 0.05

    # En el límite exacto (0,05 % de 200 = 0,1) ya es diferencia apreciable.
    limite = _mejora({"mae": 199.9}, {"mae": 200.0})
    assert limite["tipo"] == "mejor"

    cero = _mejora({"mae": 0.0}, {"mae": 0.0})
    assert cero["tipo"] == "no_calculable"
    assert "cero" in cero["motivo"]


def test_recalculo_independiente_suma_antes_de_medir():
    """El horizonte suma los 28 días por serie y luego mide; el diario mide serie × día."""
    prediccion = np.array([[1.0, 3.0], [2.0, 2.0]])
    observado = np.array([[2.0, 2.0], [2.0, 2.0]])
    diario = _metricas(prediccion - observado, observado)
    assert diario["n"] == 4
    assert diario["mae"] == pytest.approx(0.5)
    assert diario["sesgo"] == pytest.approx(0.0)

    horizonte = _metricas(prediccion.sum(axis=1) - observado.sum(axis=1), observado.sum(axis=1))
    assert horizonte["n"] == 2
    assert horizonte["mae"] == pytest.approx(0.0), "los errores con signo opuesto se compensan"
    assert horizonte["wape"] == pytest.approx(0.0)


def test_consolidacion_de_la_auditoria_suma_numeradores():
    """La consolidación del script de auditoría reconstruye MAE y WAPE desde numeradores."""
    cortes = [
        _metricas(np.array([2.0, -2.0]), np.array([10.0, 10.0])),
        _metricas(np.array([6.0]), np.array([10.0])),
    ]
    consolidado = consolidar_auditoria(cortes)
    assert consolidado["n"] == 3
    assert consolidado["mae"] == pytest.approx((4 + 6) / 3)
    assert consolidado["wape"] == pytest.approx(100 * 10 / 30)
    assert consolidado["sesgo"] == pytest.approx((0 + 6) / 3)


def test_politica_de_precision_es_el_mismo_texto_en_todas_las_respuestas():
    """La política la define un solo lugar: planificación y servicio devuelven la misma frase."""
    activa = {"politica": "decimal-v1"}
    texto = planificacion.texto_politica(activa)
    assert "decimal-v1" in texto and "serie decimal" in texto
    assert planificacion.texto_politica({"politica": "enteros-por-dia"}) != texto
    assert planificacion.texto_politica(None) == texto, "sin publicación se asume la decimal"

    from app.services.comercial_service import ComercialServicio

    servicio = object.__new__(ComercialServicio)
    servicio._repo = RepositorioFalso([], publicacion=activa)
    assert servicio._politica_precision() == texto
