"""Pruebas del generador de datos sintéticos (sin base de datos)."""

from __future__ import annotations

from collections import Counter

import pytest

from app.services import demo_data
from app.services.metrics import line_amounts


@pytest.fixture(scope="module")
def generado() -> tuple[list[dict], dict]:
    return demo_data.generate_dataset()


def test_el_dataset_es_reproducible():
    primero, _ = demo_data.generate_dataset()
    segundo, _ = demo_data.generate_dataset()
    assert primero == segundo


def test_numero_de_lineas_exacto(generado):
    documentos, metadatos = generado
    assert len(documentos) == 30_000 == metadatos["filas"]


def test_cubre_24_meses_completos(generado):
    documentos, metadatos = generado
    meses = Counter(documento["fecha"].strftime("%Y-%m") for documento in documentos)
    assert len(meses) == 24
    assert min(meses) == "2024-01"
    assert max(meses) == "2025-12"
    assert all(conteo > 0 for conteo in meses.values())
    assert metadatos["periodo"] == {"desde": "2024-01-01", "hasta": "2025-12-31"}


def test_estacionalidad_y_crecimiento_se_ven_en_los_datos(generado):
    documentos, _ = generado
    meses = Counter(documento["fecha"].strftime("%Y-%m") for documento in documentos)
    # Diciembre concentra más líneas que enero.
    assert meses["2024-12"] > meses["2024-01"]
    assert meses["2025-12"] > meses["2025-01"]
    # El segundo año tiene más volumen que el primero.
    lineas_2024 = sum(v for k, v in meses.items() if k.startswith("2024"))
    lineas_2025 = sum(v for k, v in meses.items() if k.startswith("2025"))
    assert lineas_2025 > lineas_2024


def test_todos_los_importes_derivados_son_coherentes(generado):
    documentos, _ = generado
    for documento in documentos:
        esperado = line_amounts(
            documento["cantidad"],
            documento["precio_unitario_cent"],
            documento["descuento_cent"],
            documento["coste_unitario_cent"],
        )
        for campo, valor in esperado.items():
            assert documento[campo] == valor, f"{documento['linea_id']}: {campo}"


def test_identificadores_unicos(generado):
    documentos, _ = generado
    lineas = {documento["linea_id"] for documento in documentos}
    assert len(lineas) == len(documentos)
    assert all(documento["linea_id"].startswith("ln") for documento in documentos)


def test_los_pedidos_mantienen_cliente_fecha_region_y_canal(generado):
    documentos, metadatos = generado
    por_pedido: dict[str, set[tuple]] = {}
    for documento in documentos:
        clave = (
            documento["cliente_id"],
            documento["fecha"],
            documento["region_slug"],
            documento["canal_slug"],
        )
        por_pedido.setdefault(documento["pedido_id"], set()).add(clave)

    assert len(por_pedido) == metadatos["pedidos"]
    assert all(len(claves) == 1 for claves in por_pedido.values())


def test_dimensiones_del_dataset(generado):
    documentos, metadatos = generado
    assert {documento["region_slug"] for documento in documentos} == {
        "norte",
        "centro",
        "bajio",
        "occidente",
        "sur",
        "sureste",
    }
    assert {documento["canal_slug"] for documento in documentos} == {
        "retail",
        "mayorista",
        "ecommerce",
        "corporativo",
    }
    assert len({documento["categoria_slug"] for documento in documentos}) == 5
    assert len({documento["producto_id"] for documento in documentos}) == metadatos["productos"] == 25
    assert len({documento["cliente_id"] for documento in documentos}) == metadatos["clientes"] == 120


def test_metadatos_documentan_el_origen(generado):
    _, metadatos = generado
    assert metadatos["dataset_id"] == demo_data.DATASET_ID
    assert metadatos["origen"] == "sintetico"
    assert metadatos["moneda"] == "MXN"
    assert metadatos["semilla"] == demo_data.SEED
    assert metadatos["esquema_version"] == demo_data.SCHEMA_VERSION
    assert metadatos["inmutable"] is True
    assert metadatos["generado_en"] is not None


def test_no_hay_importes_negativos_ni_descuentos_imposibles(generado):
    documentos, _ = generado
    for documento in documentos:
        assert documento["descuento_cent"] <= documento["venta_bruta_cent"]
        assert documento["venta_neta_cent"] >= 0
        assert documento["cantidad"] > 0
        assert documento["moneda"] == "MXN"
        assert documento["dataset_version"] == demo_data.DATASET_VERSION


def test_un_pedido_no_supera_cuatro_lineas(generado):
    documentos, _ = generado
    conteo = Counter(documento["pedido_id"] for documento in documentos)
    assert max(conteo.values()) <= 4
