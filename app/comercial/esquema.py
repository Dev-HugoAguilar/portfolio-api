"""Modelo compacto de documentos para «Atlas comercial · Demanda y ventas».

Por qué este esquema
--------------------
M5 tiene 30 490 series (3 049 productos × 10 tiendas) y hasta 1 941 días: un
documento por serie y día serían ~59 millones de documentos, imposible de servir
desde un MongoDB local pequeño y absurdo para las tres gráficas del tablero.

Se eligen **cuatro** formas de documento, cada una con un motivo medible:

1. ``m5_catalogo`` — un documento por tienda, estado, categoría, departamento y
   producto. Es el catálogo que alimenta filtros y búsqueda.
2. ``m5_calendario`` — un documento por día comercial (fecha, semana, evento,
   ``snap``). Permite traducir ``d_N`` a fecha sin abrir el CSV.
3. ``m5_agregados`` — **un documento por clave de dimensión** con el vector de
   unidades alineado a ``d_inicio``; la dimensión se declara en el propio
   documento (``total``, ``tienda``, ``categoria``, ``estado``, ``departamento``,
   ``categoria_tienda``). Unas decenas de documentos sirven las series y la
   comparación A/B.
4. ``m5_bloques`` — serie **producto × tienda** en bloques de ``TAMANO_BLOQUE``
   días. Acota el tamaño de documento (196 enteros pequeños) y permite sumar un
   intervalo leyendo uno o dos bloques por serie.

Los pronósticos y las métricas van en colecciones propias (``m5_pronosticos`` y
``m5_evaluacion``) porque son resultados versionados: se recalculan y se publican
en bloque, sin tocar los datos de ventas.

Reglas de validación que impone este módulo
------------------------------------------
- Cantidades: enteras y **no negativas**; un ``None`` significa «no observado» y
  nunca se convierte en cero.
- Fechas: días comerciales (``date``), sin convertir a medianoche UTC.
- Identificadores: cadenas no vacías, tal como llegan del archivo.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from app.comercial import ESQUEMA_VERSION

#: Dimensiones agregadas que se guardan en ``m5_agregados``.
#:
#: ``departamento_tienda`` es un **derivado local**: no viene de los CSV de M5, se
#: calcula desde ``m5_bloques`` × catálogo y solo se publica cuando reconcilia
#: (ver ``app.comercial.derivados``).
DIMENSIONES = (
    "total",
    "tienda",
    "estado",
    "categoria",
    "departamento",
    "categoria_tienda",
    "departamento_tienda",
)


class ErrorEsquema(ValueError):
    """Error de validación de documentos del modelo compacto."""


def unidades_validas(valor: object, contexto: str = "") -> int:
    """Convierte una cantidad a entero no negativo.

    Rechaza negativos, ``NaN``, infinitos y textos no numéricos: el archivo M5
    solo trae enteros ≥ 0 y cualquier otra cosa indica un problema de lectura.
    """
    if isinstance(valor, bool) or valor is None:
        raise ErrorEsquema(f"Cantidad no válida{' en ' + contexto if contexto else ''}: {valor!r}")
    if isinstance(valor, str):
        texto = valor.strip()
        if texto == "":
            raise ErrorEsquema(f"Cantidad vacía{' en ' + contexto if contexto else ''}")
        try:
            valor = float(texto)
        except ValueError as exc:
            raise ErrorEsquema(f"Cantidad no numérica: {valor!r}") from exc
    numero = float(valor)
    if not math.isfinite(numero):
        raise ErrorEsquema(f"Cantidad no finita: {valor!r}")
    if numero < 0:
        raise ErrorEsquema(f"Cantidad negativa{' en ' + contexto if contexto else ''}: {numero}")
    if numero != int(numero):
        raise ErrorEsquema(f"Cantidad no entera: {numero}")
    return int(numero)


def fecha_comercial(valor: object) -> date:
    """Fecha de calendario (AAAA-MM-DD) sin zona horaria."""
    if isinstance(valor, date):
        return valor
    texto = str(valor).strip()
    try:
        anio, mes, dia = (int(parte) for parte in texto[:10].split("-"))
    except (ValueError, AttributeError) as exc:
        raise ErrorEsquema(f"Fecha no válida: {valor!r}") from exc
    return date(anio, mes, dia)


def texto_requerido(valor: object, campo: str) -> str:
    """Cadena no vacía, sin recortar su contenido original."""
    texto = "" if valor is None else str(valor).strip()
    if not texto:
        raise ErrorEsquema(f"«{campo}» no puede estar vacío")
    return texto


def documento_agregado(
    *,
    version: str,
    dimension: str,
    clave: str,
    d_inicio: int,
    unidades: list[int | None],
    extra: dict | None = None,
) -> dict:
    """Documento de ``m5_agregados``: vector de días alineado a ``d_inicio``."""
    if dimension not in DIMENSIONES:
        raise ErrorEsquema(f"Dimensión desconocida: {dimension}")
    if d_inicio < 1:
        raise ErrorEsquema("d_inicio debe ser ≥ 1")
    limpio = [None if valor is None else unidades_validas(valor, f"{dimension}/{clave}") for valor in unidades]
    documento = {
        "_id": f"{version}:{dimension}:{clave}",
        "esquema": ESQUEMA_VERSION,
        "version": version,
        "dimension": dimension,
        "clave": clave,
        "d_inicio": d_inicio,
        "d_fin": d_inicio + len(limpio) - 1,
        "unidades": limpio,
        "total": sum(valor for valor in limpio if valor is not None),
        "dias_observados": sum(1 for valor in limpio if valor is not None),
    }
    if extra:
        documento.update(extra)
    return documento


@dataclass(frozen=True)
class Ventana:
    """Intervalo inclusivo de días comerciales."""

    desde: date
    hasta: date

    def __post_init__(self) -> None:
        if self.desde > self.hasta:
            raise ErrorEsquema("El intervalo termina antes de empezar")

    @property
    def dias(self) -> int:
        return (self.hasta - self.desde).days + 1

    def contiene(self, otra: "Ventana") -> bool:
        return self.desde <= otra.desde and otra.hasta <= self.hasta
