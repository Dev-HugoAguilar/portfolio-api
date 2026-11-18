"""Atlas comercial · Demanda y ventas (datos históricos Walmart M5).

Este paquete contiene la **preparación** del conjunto M5 y el cálculo del baseline
de pronóstico. La API solo lee lo que estos procesos dejan en MongoDB: dentro de
una petición HTTP no se importa ni se entrena nada.

Módulos:

- ``fuentes``   archivos esperados, nombres de columna y aritmética de días.
- ``esquema``   versión del esquema, colecciones y validaciones del modelo compacto.
- ``ingesta``   importación por lotes, idempotente por versión, con manifiesto.
- ``forecast``  baseline estacional semanal, backtesting y métricas.
- ``consultas`` lectura de agregados para las vistas públicas.
- ``inventario`` medición de almacenamiento y preparación de la exportación compacta.
"""

from __future__ import annotations

ESQUEMA_VERSION = "m5-comercial-1"
"""Versión del esquema de documentos. Se guarda en cada manifiesto y en cada
documento de datos para poder convivir con importaciones antiguas."""

PROYECTO_ID = "atlas-comercial"
"""Identificador del proyecto dentro del portafolio."""

__all__ = ["ESQUEMA_VERSION", "PROYECTO_ID"]
