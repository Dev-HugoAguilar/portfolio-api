"""Exportación CSV del detalle de ventas.

Reglas:

* Se aplican **los mismos filtros** que el detalle (misma normalización).
* Hay un tope documentado de filas (``max_export_rows``); si el filtro lo supera
  se responde con error 413 y se pide acotar el rango.
* Protección contra inyección de fórmulas: en las celdas **de texto** cuyo primer
  carácter sea ``=``, ``+``, ``-``, ``@``, tabulador o retorno de carro se antepone
  un apóstrofo. Las celdas numéricas no se tocan, así los importes negativos
  siguen siendo números.
* Se emite BOM UTF-8 para que Excel abra bien los acentos.
"""

from __future__ import annotations

import csv
import io
from typing import Iterable, Iterator

#: Columnas del CSV, en orden, con su tipo lógico.
CSV_COLUMNS: list[tuple[str, str]] = [
    ("linea_id", "texto"),
    ("pedido_id", "texto"),
    ("fecha", "texto"),
    ("cliente_id", "texto"),
    ("cliente", "texto"),
    ("region", "texto"),
    ("canal", "texto"),
    ("producto_id", "texto"),
    ("producto", "texto"),
    ("categoria", "texto"),
    ("cantidad", "numero"),
    ("precio_unitario_cent", "numero"),
    ("descuento_cent", "numero"),
    ("coste_unitario_cent", "numero"),
    ("venta_bruta_cent", "numero"),
    ("venta_neta_cent", "numero"),
    ("coste_cent", "numero"),
    ("margen_cent", "numero"),
    ("margen_pct", "numero"),
    ("moneda", "texto"),
]

#: Prefijos que un programa de hojas de cálculo interpreta como fórmula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_BOM = "\ufeff"


def protect_cell(value: object, kind: str) -> object:
    """Antepone un apóstrofo a las celdas de texto peligrosas."""
    if kind != "texto" or value is None:
        return value
    texto = str(value)
    if texto.startswith(_FORMULA_PREFIXES):
        return "'" + texto
    return value


def csv_rows(lines: Iterable[dict]) -> Iterator[str]:
    """Genera el CSV por trozos para no cargar todo en memoria."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([nombre for nombre, _tipo in CSV_COLUMNS])

    pendientes = 0
    primera = True
    for linea in lines:
        fila = []
        for nombre, tipo in CSV_COLUMNS:
            if nombre == "margen_pct":
                valor = linea["margen_pct"]
                fila.append("" if valor is None else valor)
                continue
            fila.append(protect_cell(linea[nombre], tipo))
        writer.writerow(fila)

        pendientes += 1
        if pendientes >= 500:
            trozo = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            pendientes = 0
            yield (_BOM if primera else "") + trozo
            primera = False

    trozo = buffer.getvalue()
    if trozo or primera:
        yield (_BOM if primera else "") + trozo


def export_headers(dataset_id: str, dataset_version: int, filas: int) -> dict[str, str]:
    """Cabeceras que documentan la exportación."""
    return {
        "Content-Disposition": f'attachment; filename="ventas_{dataset_id}.csv"',
        "X-Dataset-Id": dataset_id,
        "X-Dataset-Version": str(dataset_version),
        "X-Total-Filas": str(filas),
        "X-Moneda": "MXN",
    }
