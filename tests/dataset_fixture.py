"""Dataset de pruebas pequeño, con resultados calculados a mano.

Seis líneas repartidas en dos periodos:

* periodo actual: 2024-02-01 .. 2024-02-02 (4 líneas, 2 pedidos)
* periodo anterior: 2024-01-30 .. 2024-01-31 (2 líneas, 2 pedidos), que es el
  bloque de 2 días inmediatamente anterior al periodo actual.

Los importes se calculan con :func:`app.services.metrics.line_amounts` para que
la fixture no duplique la fórmula; los totales esperados están escritos a mano en
:data:`ACTUAL`, :data:`ANTERIOR` y :data:`RANGO_COMPLETO`.
"""

from __future__ import annotations

from datetime import date, datetime

from app.services.metrics import line_amounts

DATASET_ID = "test_ds"
DATASET_VERSION = 1
OTRO_DATASET_ID = "test_ds_ajeno"

ACTUAL_DESDE = date(2024, 2, 1)
ACTUAL_HASTA = date(2024, 2, 2)
ANTERIOR_DESDE = date(2024, 1, 30)
ANTERIOR_HASTA = date(2024, 1, 31)


def linea(
    *,
    linea_id: str,
    pedido_id: str,
    fecha: date,
    cliente_id: str,
    cliente_nombre: str,
    region_slug: str,
    region: str,
    canal_slug: str,
    canal: str,
    producto_id: str,
    producto_nombre: str,
    categoria_slug: str,
    categoria: str,
    cantidad: int,
    precio_unitario_cent: int,
    descuento_cent: int,
    coste_unitario_cent: int,
    dataset_id: str = DATASET_ID,
) -> dict:
    importes = line_amounts(cantidad, precio_unitario_cent, descuento_cent, coste_unitario_cent)
    return {
        "dataset_id": dataset_id,
        "dataset_version": DATASET_VERSION,
        "linea_id": linea_id,
        "pedido_id": pedido_id,
        "fecha": datetime.combine(fecha, datetime.min.time()),
        "cliente_id": cliente_id,
        "cliente_nombre": cliente_nombre,
        "region_slug": region_slug,
        "region": region,
        "canal_slug": canal_slug,
        "canal": canal,
        "producto_id": producto_id,
        "producto_nombre": producto_nombre,
        "categoria_slug": categoria_slug,
        "categoria": categoria,
        "cantidad": cantidad,
        "precio_unitario_cent": precio_unitario_cent,
        "descuento_cent": importes["descuento_cent"],
        "coste_unitario_cent": coste_unitario_cent,
        "venta_bruta_cent": importes["venta_bruta_cent"],
        "venta_neta_cent": importes["venta_neta_cent"],
        "coste_cent": importes["coste_cent"],
        "margen_cent": importes["margen_cent"],
        "moneda": "MXN",
        "origen": "sintetico",
    }


LINEAS: tuple[dict, ...] = (
    # --- periodo actual -----------------------------------------------------
    # pedido 1: cliente uno, norte, retail
    linea(
        linea_id="ln00000001",
        pedido_id="ped_o1",
        fecha=date(2024, 2, 1),
        cliente_id="cli_uno",
        cliente_nombre="Cliente Uno SA",
        region_slug="norte",
        region="Norte",
        canal_slug="retail",
        canal="Retail",
        producto_id="prd_uno",
        producto_nombre="Producto Uno",
        categoria_slug="cat_a",
        categoria="Categoría A",
        cantidad=2,
        precio_unitario_cent=10_000,  # bruto 20.000
        descuento_cent=0,
        coste_unitario_cent=6_000,  # coste 12.000
    ),
    linea(
        linea_id="ln00000002",
        pedido_id="ped_o1",
        fecha=date(2024, 2, 1),
        cliente_id="cli_uno",
        cliente_nombre="Cliente Uno SA",
        region_slug="norte",
        region="Norte",
        canal_slug="retail",
        canal="Retail",
        producto_id="prd_dos",
        producto_nombre="Producto Dos",
        categoria_slug="cat_b",
        categoria="Categoría B",
        cantidad=1,
        precio_unitario_cent=50_000,  # bruto 50.000
        descuento_cent=5_000,  # neto 45.000
        coste_unitario_cent=20_000,  # coste 20.000
    ),
    # pedido 2: cliente dos, sur, mayorista
    linea(
        linea_id="ln00000003",
        pedido_id="ped_o2",
        fecha=date(2024, 2, 2),
        cliente_id="cli_dos",
        cliente_nombre="Cliente Dos SAPI",
        region_slug="sur",
        region="Sur",
        canal_slug="mayorista",
        canal="Mayorista",
        producto_id="prd_uno",
        producto_nombre="Producto Uno",
        categoria_slug="cat_a",
        categoria="Categoría A",
        cantidad=5,
        precio_unitario_cent=10_000,  # bruto 50.000
        descuento_cent=2_500,  # neto 47.500
        coste_unitario_cent=6_000,  # coste 30.000
    ),
    linea(
        linea_id="ln00000004",
        pedido_id="ped_o2",
        fecha=date(2024, 2, 2),
        cliente_id="cli_dos",
        cliente_nombre="Cliente Dos SAPI",
        region_slug="sur",
        region="Sur",
        canal_slug="mayorista",
        canal="Mayorista",
        producto_id="prd_dos",
        producto_nombre="Producto Dos",
        categoria_slug="cat_b",
        categoria="Categoría B",
        cantidad=3,
        precio_unitario_cent=30_000,  # bruto 90.000
        descuento_cent=0,
        coste_unitario_cent=30_000,  # margen 0
    ),
    # --- periodo anterior (2024-01-30 .. 2024-01-31) ------------------------
    linea(
        linea_id="ln00000005",
        pedido_id="ped_p1",
        fecha=date(2024, 1, 30),
        cliente_id="cli_uno",
        cliente_nombre="Cliente Uno SA",
        region_slug="norte",
        region="Norte",
        canal_slug="retail",
        canal="Retail",
        producto_id="prd_uno",
        producto_nombre="Producto Uno",
        categoria_slug="cat_a",
        categoria="Categoría A",
        cantidad=2,
        precio_unitario_cent=10_000,  # neto 20.000
        descuento_cent=0,
        coste_unitario_cent=6_000,  # margen 8.000
    ),
    linea(
        linea_id="ln00000006",
        pedido_id="ped_p2",
        fecha=date(2024, 1, 31),
        cliente_id="cli_dos",
        cliente_nombre="Cliente Dos SAPI",
        region_slug="sur",
        region="Sur",
        canal_slug="mayorista",
        canal="Mayorista",
        producto_id="prd_dos",
        producto_nombre="Producto Dos",
        categoria_slug="cat_b",
        categoria="Categoría B",
        cantidad=2,
        precio_unitario_cent=50_000,  # bruto 100.000
        descuento_cent=10_000,  # neto 90.000
        coste_unitario_cent=20_000,  # coste 40.000, margen 50.000
    ),
)

METADATOS: dict = {
    "dataset_id": DATASET_ID,
    "nombre": "Dataset de pruebas",
    "descripcion": "Seis líneas con resultados calculados a mano para las pruebas.",
    "origen": "sintetico",
    "moneda": "MXN",
    "dataset_version": DATASET_VERSION,
    "esquema_version": "1.0.0",
    "semilla": 1,
    "generado_en": datetime(2024, 3, 1, 0, 0, 0),
    "periodo": {"desde": ANTERIOR_DESDE.isoformat(), "hasta": ACTUAL_HASTA.isoformat()},
    "filas": len(LINEAS),
    "pedidos": 4,
    "clientes": 2,
    "productos": 2,
    "inmutable": True,
}

#: Periodo actual 2024-02-01 .. 2024-02-02 (4 líneas).
ACTUAL = {
    "lineas": 4,
    "pedidos": 2,
    "clientes": 2,
    "unidades": 11,
    "venta_bruta_cent": 210_000,
    "descuento_cent": 7_500,
    "venta_neta_cent": 202_500,
    "coste_cent": 152_000,
    "margen_cent": 50_500,
    "margen_pct": 24.94,
    "ticket_medio_cent": 101_250,
}

#: Periodo anterior equivalente 2024-01-30 .. 2024-01-31 (2 líneas).
ANTERIOR = {
    "lineas": 2,
    "pedidos": 2,
    "clientes": 2,
    "unidades": 4,
    "venta_bruta_cent": 120_000,
    "descuento_cent": 10_000,
    "venta_neta_cent": 110_000,
    "coste_cent": 52_000,
    "margen_cent": 58_000,
    "margen_pct": 52.73,
    "ticket_medio_cent": 55_000,
}

#: Variaciones del periodo actual frente al anterior.
VARIACION = {
    "venta_neta_pct": 84.09,  # 100·(202.500−110.000)/110.000
    "margen_pct": -12.93,  # 100·(50.500−58.000)/58.000
    "margen_pt": -27.79,  # 24.94 − 52.73
}

#: Rango completo del dataset (ambos periodos, 6 líneas).
RANGO_COMPLETO = {
    "lineas": 6,
    "pedidos": 4,
    "clientes": 2,
    "unidades": 15,
    "venta_bruta_cent": 330_000,
    "descuento_cent": 17_500,
    "venta_neta_cent": 312_500,
    "coste_cent": 204_000,
    "margen_cent": 108_500,
    "margen_pct": 34.72,
    "ticket_medio_cent": 78_125,
}


def linea_cero(linea_id: str = "ln00000099") -> dict:
    """Línea con venta neta 0 para probar la división por cero."""
    return linea(
        linea_id=linea_id,
        pedido_id="ped_cero",
        fecha=date(2024, 2, 2),
        cliente_id="cli_tres",
        cliente_nombre="Cliente Tres SC",
        region_slug="norte",
        region="Norte",
        canal_slug="retail",
        canal="Retail",
        producto_id="prd_cero",
        producto_nombre="Producto Cero",
        categoria_slug="cat_c",
        categoria="Categoría C",
        cantidad=1,
        precio_unitario_cent=0,
        descuento_cent=0,
        coste_unitario_cent=0,
    )


def linea_peligrosa() -> dict:
    """Línea con texto que parece una fórmula de hoja de cálculo."""
    return linea(
        linea_id="ln00000098",
        pedido_id="ped_formula",
        fecha=date(2024, 2, 2),
        cliente_id="cli_formula",
        cliente_nombre="=SUM(A1:A9)",
        region_slug="norte",
        region="@region-peligrosa",
        canal_slug="retail",
        canal="Retail",
        producto_id="prd_formula",
        producto_nombre="+Producto Falso",
        categoria_slug="cat_c",
        categoria="Categoría C",
        cantidad=1,
        precio_unitario_cent=1_000,
        descuento_cent=0,
        coste_unitario_cent=500,
    )
