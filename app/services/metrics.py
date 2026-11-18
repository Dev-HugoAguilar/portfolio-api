"""Reglas de negocio de las métricas.

Se aplican **una sola vez**, aquí, y tanto la generación de datos como los
servicios usan estas funciones. MongoDB solo suma importes ya calculados; los
porcentajes se derivan de esas sumas.

Definiciones (moneda única: MXN, importes en centavos enteros):

    venta_bruta   = cantidad × precio_unitario
    venta_neta    = venta_bruta − descuento
    coste_ventas  = cantidad × coste_unitario
    margen_bruto  = venta_neta − coste_ventas
    margen_pct    = 100 × margen_bruto / venta_neta      (null si venta_neta = 0)

No se modelan gastos operativos: no se devuelve utilidad operativa.
Los impuestos no forman parte de la venta neta.
Las devoluciones quedan fuera de esta primera versión.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

#: Decimales de los porcentajes devueltos.
PERCENT_DECIMALS = 2


def _round(value: float, decimals: int = PERCENT_DECIMALS) -> float:
    """Redondeo comercial (mitad hacia arriba) y estable entre plataformas."""
    quant = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def line_amounts(
    cantidad: int,
    precio_unitario_cent: int,
    descuento_cent: int,
    coste_unitario_cent: int,
) -> dict[str, int]:
    """Importes de una línea de venta, en centavos enteros.

    El descuento se expresa en importe monetario y no puede superar la venta
    bruta; si la superara, la venta neta quedaría negativa y el dato sería
    inválido, así que se rechaza.
    """
    if cantidad < 0:
        raise ValueError("cantidad no puede ser negativa")
    if precio_unitario_cent < 0 or coste_unitario_cent < 0 or descuento_cent < 0:
        raise ValueError("los importes no pueden ser negativos")

    venta_bruta = cantidad * precio_unitario_cent
    if descuento_cent > venta_bruta:
        raise ValueError("el descuento no puede superar la venta bruta")

    venta_neta = venta_bruta - descuento_cent
    coste = cantidad * coste_unitario_cent

    return {
        "venta_bruta_cent": venta_bruta,
        "descuento_cent": descuento_cent,
        "venta_neta_cent": venta_neta,
        "coste_cent": coste,
        "margen_cent": venta_neta - coste,
    }


def margin_pct(venta_neta_cent: int, margen_cent: int) -> float | None:
    """Porcentaje de margen sobre la venta neta; ``None`` si la base es 0."""
    if venta_neta_cent == 0:
        return None
    return _round(100 * margen_cent / venta_neta_cent)


def variation_pct(current: float | int | None, previous: float | int | None) -> float | None:
    """Variación relativa en %; ``None`` si la base no permite calcularla."""
    if current is None or previous is None or previous == 0:
        return None
    return _round(100 * (current - previous) / abs(previous))


def variation_points(current: float | None, previous: float | None) -> float | None:
    """Diferencia en puntos porcentuales entre dos porcentajes."""
    if current is None or previous is None:
        return None
    return _round(current - previous)


def round_pct(value: float | None) -> float | None:
    return None if value is None else _round(value)


@dataclass(frozen=True)
class Periodo:
    desde: date
    hasta: date

    @property
    def dias(self) -> int:
        return (self.hasta - self.desde).days + 1


def previous_period(desde: date, hasta: date) -> Periodo:
    """Periodo anterior **equivalente**, de igual número de días.

    Se toma el bloque inmediatamente anterior y de la misma duración:

        días      = hasta − desde + 1
        hasta_ant = desde − 1 día
        desde_ant = hasta_ant − (días − 1)

    Así, un mes de 31 días se compara con los 31 días anteriores; un rango de
    365 días, con los 365 anteriores. Los filtros de negocio no cambian.
    """
    dias = (hasta - desde).days + 1
    hasta_anterior = desde - timedelta(days=1)
    desde_anterior = hasta_anterior - timedelta(days=dias - 1)
    return Periodo(desde=desde_anterior, hasta=hasta_anterior)


def ticket_medio(venta_neta_cent: int, pedidos: int) -> int | None:
    """Venta neta media por pedido, en centavos (redondeo al centavo)."""
    if pedidos <= 0:
        return None
    return int(Decimal(venta_neta_cent / pedidos).quantize(Decimal(1), rounding=ROUND_HALF_UP))
