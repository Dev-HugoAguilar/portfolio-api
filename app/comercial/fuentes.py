"""Archivos, columnas y aritmética de días del conjunto Walmart M5.

Procedencia
-----------
«M5 Forecasting - Accuracy», competición de Kaggle:
https://www.kaggle.com/competitions/m5-forecasting-accuracy/data

Los archivos **no se versionan en el repositorio**: son datos de la competición,
sujetos a sus condiciones de uso. Se descargan aparte y se importan en local.

Qué asume este módulo
---------------------
Nada sobre el contenido: el importador lee los archivos reales y **deriva** de
ellos la cobertura, los identificadores y los rangos. Aquí solo se declaran los
nombres de archivo y de columna, la regla de comparación de hashes y la
aritmética de días, que en M5 es 1-based (``d_1`` es el primer día observado).

Los datos son históricos de **Estados Unidos** (tres estados: CA, TX, WI). No se
renombran tiendas como ciudades mexicanas ni se convierten monedas.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ARCHIVO_VENTAS = "sales_train_evaluation.csv"
ARCHIVO_VENTAS_ALT = "sales_train_validation.csv"
"""Si solo está el archivo de validación, se importa con su cobertura real
(1913 días) y el manifiesto lo declara. Nunca se mezclan los dos: se solapan."""

ARCHIVO_CALENDARIO = "calendar.csv"
ARCHIVO_PRECIOS = "sell_prices.csv"
"""Opcional en esta entrega: el baseline no usa precios y no hay métrica de
ingresos, así que no se importa salvo análisis documentados."""

COLUMNAS_VENTAS_FIJAS = ("id", "item_id", "dept_id", "cat_id", "store_id", "state_id")
COLUMNAS_CALENDARIO = (
    "date",
    "wm_yr_wk",
    "weekday",
    "wday",
    "month",
    "year",
    "d",
)

PATRON_DIA = re.compile(r"^d_(\d+)$")

DIAS_HORIZONTE = 28
"""Horizonte de la competición y del baseline: 28 días."""

DIAS_SEMANA_PATRON = 7
"""El baseline repite el patrón de los últimos siete días observados."""

TAMANO_BLOQUE = 196
"""Días por bloque de la serie producto × tienda. Acota el tamaño de documento
(196 enteros pequeños) y permite leer un intervalo con una o dos lecturas."""


@dataclass(frozen=True)
class ArchivoEsperado:
    """Archivo requerido y cómo se reconoce."""

    nombre: str
    obligatorio: bool
    descripcion: str


def archivos_esperados(solo_validacion: bool = False) -> tuple[ArchivoEsperado, ...]:
    """Lista de archivos que el importador acepta."""
    ventas = ARCHIVO_VENTAS_ALT if solo_validacion else ARCHIVO_VENTAS
    return (
        ArchivoEsperado(ventas, True, "Ventas diarias por producto y tienda (columnas d_1…d_N)."),
        ArchivoEsperado(ARCHIVO_CALENDARIO, True, "Calendario: fecha real de cada día d_N."),
        ArchivoEsperado(ARCHIVO_PRECIOS, False, "Precios (no usado por el baseline de esta entrega)."),
    )


def hash_archivo(ruta: Path) -> str:
    """SHA-256 del archivo, para el manifiesto de ingesta."""
    digest = hashlib.sha256()
    with ruta.open("rb") as manejador:
        for bloque in iter(lambda: manejador.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


def cuenta_dias_ventas(cabecera: list[str]) -> int:
    """Número de días observados según las columnas ``d_N`` de la cabecera."""
    dias = [int(m.group(1)) for c in cabecera if (m := PATRON_DIA.match(c))]
    if not dias:
        raise ValueError("El archivo de ventas no tiene columnas d_N.")
    if dias != list(range(1, len(dias) + 1)):
        raise ValueError("Las columnas d_N no son consecutivas desde d_1.")
    return len(dias)


def crear_version(archivo_ventas: str, dias: int, hash_ventas: str, hash_calendario: str) -> str:
    """Identificador estable de versión: mismo contenido ⇒ misma versión.

    Así la importación es idempotente: reejecutar con los mismos archivos no crea
    una versión nueva ni duplica documentos.
    """
    firma = f"{archivo_ventas}|{dias}|{hash_ventas}|{hash_calendario}"
    return "m5-" + hashlib.sha256(firma.encode("utf-8")).hexdigest()[:12]


def dia_a_fecha(fechas: dict[int, date], dia: int) -> date:
    """Fecha real de un día ``d_N`` según el calendario importado."""
    try:
        return fechas[dia]
    except KeyError as exc:  # pragma: no cover - depende de los datos
        raise ValueError(f"El calendario no cubre d_{dia}.") from exc


def fecha_a_dia(fechas: dict[int, date], valor: date) -> int | None:
    """Día ``d_N`` de una fecha, o ``None`` si cae fuera de la cobertura."""
    for dia, fecha in fechas.items():
        if fecha == valor:
            return dia
    return None


def inicio_semana(valor: date) -> date:
    """Lunes de la semana comercial que contiene ``valor``.

    Las semanas del tablero van de **lunes a domingo** y sus extremos son
    inclusivos. En M5 la semana de la competición empieza en sábado (``wm_yr_wk``),
    así que se documenta esta diferencia: aquí manda la regla del tablero.
    """
    return valor - timedelta(days=valor.weekday())


def fin_semana(valor: date) -> date:
    """Domingo de la semana comercial que contiene ``valor``."""
    return inicio_semana(valor) + timedelta(days=6)
