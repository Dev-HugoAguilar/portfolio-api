"""Generación del dataset sintético de demostración.

Características:

* **Reproducible**: usa una semilla fija y ningún orden no determinista.
* **Ficticio**: empresas, productos y regiones inventados; ninguna persona real.
* **24 meses completos** (2024-01-01 .. 2025-12-31) con estacionalidad, efecto
  de día de la semana, crecimiento interanual y diferencias por segmento.
* **30.000 líneas** exactas (1.250 por mes).
* Los importes se calculan con :func:`app.services.metrics.line_amounts`, las
  mismas reglas que usa la API: no se duplica la fórmula.

Una línea = una línea de pedido. Los importes derivados se guardan ya calculados
en centavos enteros para que MongoDB solo tenga que sumar.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.services.metrics import line_amounts

DATASET_ID = "demo_rentabilidad_2024_2025"
DATASET_VERSION = 1
SCHEMA_VERSION = "1.0.0"
SEED = 20260401
MONEDA = "MXN"

DESDE = date(2024, 1, 1)
HASTA = date(2025, 12, 31)
LINEAS_POR_MES = 1250


@dataclass(frozen=True)
class Producto:
    producto_id: str
    nombre: str
    categoria_slug: str
    categoria: str
    precio_cent: int
    coste_cent: int


REGIONES: tuple[tuple[str, str, float], ...] = (
    ("norte", "Norte", 0.22),
    ("centro", "Centro", 0.28),
    ("bajio", "Bajío", 0.16),
    ("occidente", "Occidente", 0.14),
    ("sur", "Sur", 0.12),
    ("sureste", "Sureste", 0.08),
)

CANALES: tuple[tuple[str, str, float], ...] = (
    ("retail", "Retail", 0.34),
    ("mayorista", "Mayorista", 0.26),
    ("ecommerce", "E-commerce", 0.28),
    ("corporativo", "Corporativo", 0.12),
)

CATEGORIAS: tuple[tuple[str, str], ...] = (
    ("computo", "Cómputo"),
    ("perifericos", "Periféricos"),
    ("redes", "Redes"),
    ("almacenamiento", "Almacenamiento"),
    ("accesorios", "Accesorios"),
)

PRODUCTOS: tuple[Producto, ...] = (
    Producto("prd_001", "Laptop Nova 14", "computo", "Cómputo", 1_899_900, 1_330_000),
    Producto("prd_002", "Laptop Nova 16 Pro", "computo", "Cómputo", 2_799_900, 2_000_000),
    Producto("prd_003", "Monitor Vision 27", "computo", "Cómputo", 649_900, 449_000),
    Producto("prd_004", "Monitor Vision 34 Ultra", "computo", "Cómputo", 1_149_900, 820_000),
    Producto("prd_005", "Mini PC Cubo i5", "computo", "Cómputo", 1_249_900, 900_000),
    Producto("prd_006", "Teclado Mecánico Tacto 87", "perifericos", "Periféricos", 159_900, 98_000),
    Producto("prd_007", "Mouse Ergo Pro", "perifericos", "Periféricos", 89_900, 52_000),
    Producto("prd_008", "Diadema Sonar 7.1", "perifericos", "Periféricos", 189_900, 121_000),
    Producto("prd_009", "Cámara Web Clear 4K", "perifericos", "Periféricos", 219_900, 148_000),
    Producto("prd_010", "Dock USB-C 11 en 1", "perifericos", "Periféricos", 259_900, 170_000),
    Producto("prd_011", "Router Mesh Link AX3000", "redes", "Redes", 289_900, 189_000),
    Producto("prd_012", "Switch 8 puertos Pro", "redes", "Redes", 139_900, 94_000),
    Producto("prd_013", "Access Point Wave 6", "redes", "Redes", 249_900, 165_000),
    Producto("prd_014", "Firewall Edge 500", "redes", "Redes", 849_900, 610_000),
    Producto("prd_015", "Repetidor Link Mini", "redes", "Redes", 74_900, 45_000),
    Producto("prd_016", "SSD Velo 500 GB", "almacenamiento", "Almacenamiento", 109_900, 74_000),
    Producto("prd_017", "SSD Velo 1 TB", "almacenamiento", "Almacenamiento", 179_900, 125_000),
    Producto("prd_018", "NAS Cube 2 bahías", "almacenamiento", "Almacenamiento", 899_900, 640_000),
    Producto("prd_019", "Disco Externo Atlas 4 TB", "almacenamiento", "Almacenamiento", 319_900, 228_000),
    Producto("prd_020", "Memoria Flash 128 GB", "almacenamiento", "Almacenamiento", 39_900, 22_000),
    Producto("prd_021", "Base Elevadora Alu", "accesorios", "Accesorios", 99_900, 57_000),
    Producto("prd_022", "Cable HDMI 2.1 3 m", "accesorios", "Accesorios", 29_900, 13_000),
    Producto("prd_023", "Mochila Tech 15 pulgadas", "accesorios", "Accesorios", 149_900, 88_000),
    Producto("prd_024", "Soporte Dual Monitor", "accesorios", "Accesorios", 199_900, 128_000),
    Producto("prd_025", "Regleta Inteligente USB", "accesorios", "Accesorios", 79_900, 45_000),
)

#: Mezcla de categorías por canal (mismo orden que :data:`CATEGORIAS`).
MEZCLA_CATEGORIA: dict[str, tuple[float, ...]] = {
    "retail": (0.25, 0.30, 0.10, 0.15, 0.20),
    "mayorista": (0.30, 0.15, 0.25, 0.25, 0.05),
    "ecommerce": (0.15, 0.28, 0.05, 0.22, 0.30),
    "corporativo": (0.32, 0.16, 0.24, 0.20, 0.08),
}

#: Cantidades típicas por canal.
CANTIDADES: dict[str, tuple[tuple[int, ...], tuple[float, ...]]] = {
    "retail": ((1, 2, 3), (0.60, 0.28, 0.12)),
    "mayorista": ((8, 15, 25, 40), (0.35, 0.30, 0.22, 0.13)),
    "ecommerce": ((1, 2), (0.78, 0.22)),
    "corporativo": ((3, 6, 10, 20), (0.30, 0.30, 0.25, 0.15)),
}

#: Rango de descuento por canal, en puntos base (1 pb = 0,01 %).
DESCUENTO_BP: dict[str, tuple[int, int]] = {
    "retail": (0, 800),
    "mayorista": (800, 2000),
    "ecommerce": (0, 1200),
    "corporativo": (500, 1500),
}

#: Estacionalidad mensual (índice 0 = enero).
ESTACIONALIDAD: tuple[float, ...] = (
    0.86, 0.92, 1.00, 0.98, 1.02, 0.96, 1.04, 1.10, 1.02, 1.06, 1.14, 1.34,
)

#: Efecto de día de la semana (lunes = 0).
DIA_SEMANA: tuple[float, ...] = (1.05, 1.00, 0.98, 1.02, 1.08, 0.60, 0.35)

#: Crecimiento interanual aplicado a 2025.
CRECIMIENTO_ANUAL = 0.12

_PREFIJOS = (
    "Grupo", "Comercial", "Distribuidora", "Suministros", "Operadora",
    "Servicios", "Industrial", "Tecnología", "Almacenes", "Sistemas",
    "Consultoría", "Redes",
)
_NOMBRES = (
    "Aurora", "Lumen", "Nexo", "Vértice", "Cobalto", "Horizonte", "Alameda",
    "Roble", "Delta", "Prisma", "Andar", "Quetzal", "Mirador", "Fénix",
    "Boreal", "Cumbre", "Atlas", "Origen", "Faro", "Solar", "Marina", "Cauce",
    "Terral", "Nube", "Sendero", "Cardinal", "Ámbar", "Olivo", "Tundra",
    "Cobre",
)
_SUFIJOS = ("SA de CV", "SAPI de CV", "S. de R.L.", "SA", "SC")


def _pesos(valores: tuple[float, ...]) -> list[float]:
    total = sum(valores)
    return [valor / total for valor in valores]


def _elegir(rng: random.Random, opciones: tuple, pesos: tuple[float, ...]):
    return rng.choices(opciones, weights=_pesos(pesos), k=1)[0]


def _clientes(rng: random.Random) -> list[dict]:
    """120 empresas ficticias con región y canal preferente estables."""
    nombres: list[str] = []
    for prefijo in _PREFIJOS:
        for nombre in _NOMBRES:
            nombres.append(f"{prefijo} {nombre}")
            if len(nombres) >= 120:
                break
        if len(nombres) >= 120:
            break

    clientes: list[dict] = []
    for indice, base in enumerate(nombres):
        sufijo = _SUFIJOS[indice % len(_SUFIJOS)]
        region = _elegir(rng, REGIONES, tuple(r[2] for r in REGIONES))
        canal = _elegir(rng, CANALES, tuple(c[2] for c in CANALES))
        clientes.append(
            {
                "cliente_id": f"cli_{indice + 1:03d}",
                "cliente_nombre": f"{base} {sufijo}",
                "region_slug": region[0],
                "region": region[1],
                "canal_preferente": canal[0],
                # Pesos de compra: unos clientes concentran mucho volumen.
                "peso": round(rng.uniform(0.4, 3.0), 4),
            }
        )
    return clientes


def _fechas_del_mes(anio: int, mes: int) -> tuple[list[date], list[float]]:
    primero = date(anio, mes, 1)
    siguiente = date(anio + (1 if mes == 12 else 0), (mes % 12) + 1, 1)
    dias: list[date] = []
    pesos: list[float] = []
    dia = primero
    while dia < siguiente:
        dias.append(dia)
        pesos.append(DIA_SEMANA[dia.weekday()])
        dia += timedelta(days=1)
    return dias, _pesos(tuple(pesos))


def _descuento_bp(rng: random.Random, canal: str, mes: int) -> int:
    minimo, maximo = DESCUENTO_BP[canal]
    extra = 200 if mes in (11, 12) else 0
    return min(rng.randint(minimo, maximo) + extra, 2500)


def lineas_por_mes() -> list[int]:
    """Reparte las 30.000 líneas entre los 24 meses.

    El volumen mensual sigue la estacionalidad declarada y el crecimiento
    interanual, de modo que esos factores se ven en los datos y no solo en los
    metadatos. El reparto es determinista y suma exactamente
    ``LINEAS_POR_MES * 24``.
    """
    total = LINEAS_POR_MES * 24
    pesos: list[float] = []
    for indice in range(24):
        anio, mes = DESDE.year + indice // 12, indice % 12 + 1
        factor = ESTACIONALIDAD[mes - 1] * (1 + CRECIMIENTO_ANUAL) ** (anio - DESDE.year)
        pesos.append(factor)

    suma = sum(pesos)
    exacto = [total * peso / suma for peso in pesos]
    objetivo = [int(valor) for valor in exacto]
    resto = total - sum(objetivo)
    # El sobrante se reparte por mayor parte fraccionaria, con desempate estable.
    orden = sorted(range(24), key=lambda i: (-(exacto[i] - objetivo[i]), i))
    for i in orden[:resto]:
        objetivo[i] += 1
    return objetivo


def generate_dataset() -> tuple[list[dict], dict]:
    """Devuelve ``(documentos, metadatos)`` del dataset sintético."""
    rng = random.Random(SEED)
    clientes = _clientes(rng)

    pesos_clientes = [cliente["peso"] for cliente in clientes]
    canales_por_slug = {canal[0]: canal for canal in CANALES}
    objetivos = lineas_por_mes()

    documentos: list[dict] = []
    pedidos: set[str] = set()
    consecutivo_pedido = 0
    consecutivo_linea = 0

    anio, mes = DESDE.year, DESDE.month
    indice_mes = 0
    while (anio, mes) <= (HASTA.year, HASTA.month):
        dias, pesos_dias = _fechas_del_mes(anio, mes)
        objetivo_mes = objetivos[indice_mes]
        lineas_mes = 0

        while lineas_mes < objetivo_mes:
            cliente = rng.choices(clientes, weights=_pesos(tuple(pesos_clientes)), k=1)[0]

            # 15 % de los pedidos de un cliente van por otro canal (compra puntual).
            if rng.random() < 0.15:
                canal_slug = _elegir(rng, CANALES, tuple(c[2] for c in CANALES))[0]
            else:
                canal_slug = cliente["canal_preferente"]
            canal = canales_por_slug[canal_slug]

            # 12 % de los pedidos se facturan en otra región (sucursales).
            if rng.random() < 0.12:
                region = _elegir(rng, REGIONES, tuple(r[2] for r in REGIONES))
            else:
                region = (cliente["region_slug"], cliente["region"], 0.0)

            fecha = rng.choices(dias, weights=pesos_dias, k=1)[0]

            n_lineas = rng.choices((1, 2, 3, 4), weights=(0.45, 0.30, 0.18, 0.07), k=1)[0]
            n_lineas = max(1, min(n_lineas, objetivo_mes - lineas_mes))

            pedido_id = f"ped_{consecutivo_pedido:06d}"
            pedidos.add(pedido_id)

            for _ in range(n_lineas):
                categoria_slug, _etiqueta = CATEGORIAS[
                    rng.choices(
                        tuple(range(len(CATEGORIAS))),
                        weights=_pesos(MEZCLA_CATEGORIA[canal_slug]),
                        k=1,
                    )[0]
                ]
                candidatos = [p for p in PRODUCTOS if p.categoria_slug == categoria_slug]
                producto = rng.choice(candidatos)

                cantidades, pesos_cantidad = CANTIDADES[canal_slug]
                cantidad = rng.choices(cantidades, weights=_pesos(pesos_cantidad), k=1)[0]

                # Ligero ajuste de precio por año (inflación ficticia del 4 %).
                factor_precio = 1.0 if anio == DESDE.year else 1.04
                precio_cent = int(
                    (Decimal(producto.precio_cent) * Decimal(str(factor_precio))).quantize(
                        Decimal(1), rounding=ROUND_HALF_UP
                    )
                )
                coste_unitario_cent = int(
                    (Decimal(producto.coste_cent) * Decimal(str(factor_precio))).quantize(
                        Decimal(1), rounding=ROUND_HALF_UP
                    )
                )

                bruto_cent = cantidad * precio_cent
                bp = _descuento_bp(rng, canal_slug, mes)
                descuento_cent = (bruto_cent * bp + 5_000) // 10_000

                importes = line_amounts(
                    cantidad=cantidad,
                    precio_unitario_cent=precio_cent,
                    descuento_cent=descuento_cent,
                    coste_unitario_cent=coste_unitario_cent,
                )

                consecutivo_linea += 1
                documentos.append(
                    {
                        "dataset_id": DATASET_ID,
                        "dataset_version": DATASET_VERSION,
                        "linea_id": f"ln{consecutivo_linea:08d}",
                        "pedido_id": pedido_id,
                        "fecha": datetime.combine(fecha, datetime.min.time()),
                        "cliente_id": cliente["cliente_id"],
                        "cliente_nombre": cliente["cliente_nombre"],
                        "region_slug": region[0],
                        "region": region[1],
                        "canal_slug": canal[0],
                        "canal": canal[1],
                        "producto_id": producto.producto_id,
                        "producto_nombre": producto.nombre,
                        "categoria_slug": producto.categoria_slug,
                        "categoria": producto.categoria,
                        "cantidad": cantidad,
                        "precio_unitario_cent": precio_cent,
                        "descuento_cent": importes["descuento_cent"],
                        "coste_unitario_cent": coste_unitario_cent,
                        "venta_bruta_cent": importes["venta_bruta_cent"],
                        "venta_neta_cent": importes["venta_neta_cent"],
                        "coste_cent": importes["coste_cent"],
                        "margen_cent": importes["margen_cent"],
                        "moneda": MONEDA,
                        "origen": "sintetico",
                    }
                )
                lineas_mes += 1

            consecutivo_pedido += 1

        mes += 1
        if mes > 12:
            mes = 1
            anio += 1
        indice_mes += 1

    metadatos = {
        "dataset_id": DATASET_ID,
        "nombre": "Rentabilidad comercial 2024-2025 (sintético)",
        "descripcion": (
            "Detalle de líneas de venta de una empresa ficticia de tecnología: seis "
            "regiones, cuatro canales, cinco categorías, 25 productos y 120 clientes "
            "durante 24 meses completos. Con estacionalidad, efecto de día de la semana, "
            "crecimiento interanual y descuentos distintos por canal."
        ),
        "origen": "sintetico",
        "moneda": MONEDA,
        "dataset_version": DATASET_VERSION,
        "esquema_version": SCHEMA_VERSION,
        "semilla": SEED,
        "generado_en": datetime.now(timezone.utc),
        "periodo": {"desde": DESDE.isoformat(), "hasta": HASTA.isoformat()},
        "filas": len(documentos),
        "pedidos": len(pedidos),
        "clientes": len(clientes),
        "productos": len(PRODUCTOS),
        "inmutable": True,
        # Marcado explícito: este dataset es SOLO para desarrollo y pruebas.
        # No se usa como respaldo de los endpoints de datos reales (CNBV) ni se
        # carga automáticamente al arrancar la API.
        "proposito": "desarrollo",
        "uso": "exclusivo_desarrollo_pruebas",
        "es_sintetico": True,
        "estacionalidad": {
            "indice_mensual": list(ESTACIONALIDAD),
            "efecto_dia_semana": list(DIA_SEMANA),
            "crecimiento_anual": CRECIMIENTO_ANUAL,
        },
        "lineas_por_mes": objetivos,
    }
    return documentos, metadatos
