"""Modelos y filtros del tablero «Pulso comercial · Walmex».

Los filtros se normalizan en un solo sitio para que todas las vistas pidan el
mismo subconjunto y para rechazar combinaciones que la versión publicada no
soporta (por ejemplo, un mercado o un indicador que no existe en el catálogo).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Mercados publicados, en el orden con el que se presentan.
MERCADOS = ("consolidado", "mexico", "centroamerica")

#: Indicadores de importe (millones de pesos) y de variación (porcentaje).
INDICADORES_IMPORTE = ("total_ingresos", "total_ingresos_anio_anterior", "ventas_netas")
INDICADORES_VARIACION = ("crecimiento_anual", "mismas_tiendas", "trafico", "ticket")

#: Bases de comparación admitidas.
BASES = (
    "nominal",
    "nominal_publicado",
    "nominal_calculado",
    "moneda_constante_publicado",
    "unidades_iguales_publicado",
    "publicado_comunicado",
)


def normalizar_lista(valores: list[str] | None) -> list[str]:
    """Lista sin repetidos, sin espacios y ordenada."""
    if not valores:
        return []
    limpios = {valor.strip() for valor in valores if valor and valor.strip()}
    return sorted(limpios)


@dataclass
class WalmexFiltros:
    """Filtros compartidos por las vistas del tablero."""

    mercados: list[str] = field(default_factory=list)
    desde: str | None = None
    hasta: str | None = None
    indicador: str | None = None
    base: str | None = None
    #: Varios indicadores en una sola lectura (matriz de desempeño y series de motores).
    indicadores: list[str] = field(default_factory=list)

    def condiciones(self) -> dict[str, Any]:
        consulta: dict[str, Any] = {}
        if self.mercados:
            consulta["mercado"] = {"$in": self.mercados}
        if self.desde or self.hasta:
            orden: dict[str, int] = {}
            if self.desde:
                orden["$gte"] = _orden(self.desde)
            if self.hasta:
                orden["$lte"] = _orden(self.hasta)
            consulta["orden"] = orden
        if self.indicadores:
            consulta["indicador"] = {"$in": list(self.indicadores)}
        elif self.indicador:
            consulta["indicador"] = self.indicador
        if self.base:
            consulta["base"] = self.base
        return consulta

    def activos(self) -> list[dict[str, Any]]:
        activos: list[dict[str, Any]] = []
        if self.mercados:
            activos.append(
                {"clave": "mercado", "etiqueta": "Mercado", "valores": list(self.mercados)}
            )
        if self.desde or self.hasta:
            activos.append(
                {
                    "clave": "periodo",
                    "etiqueta": "Periodo",
                    "valores": [f"{self.desde or 'inicio'} → {self.hasta or 'último'}"],
                }
            )
        if self.indicador:
            activos.append(
                {"clave": "indicador", "etiqueta": "Indicador", "valores": [self.indicador]}
            )
        if self.indicadores:
            activos.append(
                {
                    "clave": "indicadores",
                    "etiqueta": "Indicadores",
                    "valores": list(self.indicadores),
                }
            )
        if self.base:
            activos.append({"clave": "base", "etiqueta": "Base", "valores": [self.base]})
        return activos


def _orden(periodo: str) -> int:
    """Orden comparable de un periodo «3T26» (2026 × 10 + 3)."""
    return int(f"20{periodo[2:]}") * 10 + int(periodo[0])


def orden_periodo(periodo: str) -> int:
    return _orden(periodo)
