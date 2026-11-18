"""Pruebas de los análisis descriptivos de exploración (Atlas comercial).

Ventas y precios **sintéticos** (``tests/m5_fixture.py`` y un ``sell_prices.csv``
generado en la carpeta temporal). Sirven para verificar denominadores, cobertura,
reconciliaciones y las reglas que distinguen cero de ausencia.
"""

from __future__ import annotations

import math
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.comercial import ingesta, precios
from app.db.mongo import (
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_CALENDARIO,
    M5_CATALOGO,
    M5_DERIVADOS,
    M5_EVALUACION,
    M5_EVALUACION_MODELOS,
    M5_IMPORTACIONES,
    M5_MODELOS,
    M5_PRECIOS,
    M5_PRECIOS_MANIFEST,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PRONOSTICOS,
)
from app.main import create_app
from tests import m5_fixture

COLECCIONES = (
    M5_IMPORTACIONES,
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_CATALOGO,
    M5_CALENDARIO,
    M5_PRONOSTICOS,
    M5_EVALUACION,
    M5_DERIVADOS,
    M5_PRECIOS,
    M5_PRECIOS_MANIFEST,
    M5_MODELOS,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_EVALUACION_MODELOS,
)


@pytest.fixture()
def m5(mongo_db, tmp_path):
    """Ventas sintéticas importadas como versión publicada."""
    for nombre in COLECCIONES:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})
    m5_fixture.escribir(tmp_path)
    manifiesto = ingesta.importar(mongo_db, tmp_path)
    yield {"db": mongo_db, "version": manifiesto["version"], "carpeta": tmp_path}
    for nombre in COLECCIONES:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})


def _escribir_precios(carpeta, filas) -> None:
    (carpeta / "sell_prices.csv").write_text(
        "store_id,item_id,wm_yr_wk,sell_price\n"
        + "\n".join(",".join(str(valor) for valor in fila) for fila in filas)
        + "\n",
        encoding="utf-8",
    )


def _semanas(db) -> list[int]:
    return sorted({int(d["wm_yr_wk"]) for d in db[M5_CALENDARIO].find({}, {"wm_yr_wk": 1})})


@pytest.fixture()
def cliente(m5, settings):
    app = create_app(settings)
    with TestClient(app) as cliente_http:
        yield {"cliente": cliente_http, "db": m5["db"], "version": m5["version"], "carpeta": m5["carpeta"]}


# --------------------------------------------------------------------------- #
# Comparación tienda × categoría
# --------------------------------------------------------------------------- #


def test_comparacion_combina_solo_lo_que_existe(cliente):
    """El conjunto sintético tiene dos categorías con artículos: no se inventan filas."""
    cliente_http, db, version = cliente["cliente"], cliente["db"], cliente["version"]
    desde = db[M5_CALENDARIO].find_one(sort=[("d", 1)])["fecha"].date()
    hasta = db[M5_CALENDARIO].find_one(sort=[("d", -1)])["fecha"].date()
    datos = cliente_http.get(
        "/api/v1/comercial/m5/comparacion",
        params={"estado": "CA", "desde": desde.isoformat(), "hasta": hasta.isoformat()},
    ).json()
    assert datos["resumen_filas"]["tiendas"] == 1
    assert datos["resumen_filas"]["categorias"] == 2
    assert datos["resumen_filas"]["combinaciones"] == 2
    assert {fila["clave"] for fila in datos["filas"]} == {"CA_1|FOODS", "CA_1|HOBBIES"}
    # Conteos del contexto: se distinguen artículos únicos, series y departamentos.
    assert datos["conteos"]["articulos_unicos"] == 3
    assert datos["conteos"]["series_articulo_tienda"] == 3
    assert datos["conteos"]["departamentos_unicos"] == 2
    assert datos["conteos"]["combinaciones_tienda_departamento"] == 2


def test_comparacion_reconcilia_y_usa_dias_validos(cliente):
    cliente_http, db = cliente["cliente"], cliente["db"]
    desde = db[M5_CALENDARIO].find_one(sort=[("d", 1)])["fecha"].date()
    hasta = db[M5_CALENDARIO].find_one(sort=[("d", -1)])["fecha"].date()
    datos = cliente_http.get(
        "/api/v1/comercial/m5/comparacion",
        params={"estado": "CA", "desde": desde.isoformat(), "hasta": hasta.isoformat()},
    ).json()
    assert datos["reconciliacion"]["cuadra_a"] is True
    assert datos["reconciliacion"]["cuadra_b"] is True
    assert datos["reconciliacion"]["filas_a"] == datos["reconciliacion"]["contexto_a"]
    for fila in datos["filas"]:
        total = fila["metricas"]["total"]
        promedio = fila["metricas"]["promedio_diario"]
        assert fila["dias_validos_a"] == fila["dias_esperados_a"] == datos["a"]["dias"]
        assert total["a"] == pytest.approx(fila["unidades_a"])
        assert promedio["a"] == pytest.approx(fila["unidades_a"] / fila["dias_validos_a"])
        assert total["diferencia"] == pytest.approx(fila["unidades_b"] - fila["unidades_a"])
        assert fila["cobertura"] == "completa"


def test_comparacion_sin_base_en_a(cliente):
    """Una combinación con cero unidades en A no publica variación porcentual."""
    cliente_http, db = cliente["cliente"], cliente["db"]
    desde = db[M5_CALENDARIO].find_one(sort=[("d", 1)])["fecha"].date()
    hasta = db[M5_CALENDARIO].find_one(sort=[("d", -1)])["fecha"].date()
    datos = cliente_http.get(
        "/api/v1/comercial/m5/comparacion",
        params={
            "estado": "WI",
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            # HOBBIES de WI_1 son todo ceros en el conjunto sintético.
            "categorias": ["HOBBIES"],
        },
    ).json()
    fila = datos["filas"][0]
    assert fila["unidades_a"] == 0 and fila["unidades_b"] == 0
    assert fila["metricas"]["total"]["diferencia"] == 0
    assert fila["metricas"]["total"]["variacion_pct"] is None
    assert fila["metricas"]["total"]["motivo"] == "cero en ambos periodos"


# --------------------------------------------------------------------------- #
# Estacionalidad
# --------------------------------------------------------------------------- #


def test_estacionalidad_denominadores_y_reconciliacion(cliente):
    cliente_http, db = cliente["cliente"], cliente["db"]
    desde = db[M5_CALENDARIO].find_one(sort=[("d", 1)])["fecha"].date()
    hasta = db[M5_CALENDARIO].find_one(sort=[("d", -1)])["fecha"].date()
    datos = cliente_http.get(
        "/api/v1/comercial/m5/estacionalidad",
        params={
            "dimension": "categoria",
            "estado": "CA",
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
        },
    ).json()
    assert datos["dimension"] == "categoria"
    assert len(datos["semanal"]["grupos"]) == 2
    total_semanal = 0
    for grupo in datos["semanal"]["grupos"]:
        assert len(grupo["puntos"]) == 7
        # El perfil semanal cubre los días del periodo, no las 365 columnas del año.
        assert sum(punto["fechas_validas"] for punto in grupo["puntos"]) == (
            hasta - desde
        ).days + 1
        for punto in grupo["puntos"]:
            if punto["fechas_validas"]:
                assert punto["promedio"] == pytest.approx(
                    punto["unidades"] / punto["fechas_validas"], rel=1e-6
                )
            else:
                assert punto["promedio"] is None
        total_semanal += grupo["total"]
    # El perfil semanal cubre exactamente las mismas unidades que el mensual.
    total_mensual = sum(
        mes["unidades"] or 0
        for anio in datos["mensual"]["anios"]
        for mes in anio["meses"]
    )
    assert total_semanal == total_mensual


def test_estacionalidad_mensual_separa_los_cuatro_conteos(cliente):
    """Días del mes, del intervalo, con cobertura y observados son cosas distintas."""
    cliente_http, db = cliente["cliente"], cliente["db"]
    desde = db[M5_CALENDARIO].find_one(sort=[("d", 1)])["fecha"].date()
    hasta = db[M5_CALENDARIO].find_one(sort=[("d", -1)])["fecha"].date()
    datos = cliente_http.get(
        "/api/v1/comercial/m5/estacionalidad",
        params={"estado": "CA", "desde": desde.isoformat(), "hasta": hasta.isoformat()},
    ).json()
    anio = datos["mensual"]["anios"][0]
    enero = anio["meses"][0]
    assert enero["mes"] == 1
    assert enero["dias_del_mes"] == 31, "enero tiene 31 días aunque los datos empiecen el 29"
    assert enero["dias_en_intervalo"] == 3
    assert enero["dias_con_cobertura"] == 3
    assert enero["dias_observados"] == 3
    assert enero["completo"] is False and enero["parcial"] is True
    assert "3 días observados de 31" in enero["motivo"]
    assert "29" in enero["motivo"], "el motivo debe nombrar el inicio de los datos"
    # El promedio divide entre días observados, no entre días del mes.
    assert enero["promedio_diario"] == pytest.approx(enero["unidades"] / 3, rel=1e-6)

    marzo = anio["meses"][2]
    assert marzo["completo"] is True
    assert marzo["dias_del_mes"] == marzo["dias_observados"] == 31
    assert marzo["motivo"] is None

    abril = anio["meses"][3]
    assert abril["completo"] is False
    assert abril["dias_del_mes"] == 30
    assert abril["dias_en_intervalo"] == abril["dias_observados"] == 8
    assert "8 días observados de 30" in abril["motivo"]

    mayo = anio["meses"][4]
    assert mayo["dias_del_mes"] == 31
    assert mayo["dias_en_intervalo"] == 0
    assert mayo["unidades"] is None and mayo["motivo"]


def test_ventana_descriptiva_admite_mes_completo_antes_de_la_cobertura(cliente):
    """Se puede pedir enero desde el día 1; las fechas sin datos se marcan, no se inventan."""
    cliente_http = cliente["cliente"]
    datos = cliente_http.get(
        "/api/v1/comercial/m5/estacionalidad",
        params={"estado": "CA", "desde": "2011-01-01", "hasta": "2011-01-31"},
    )
    assert datos.status_code == 200, datos.text
    cuerpo = datos.json()
    enero = cuerpo["mensual"]["anios"][0]["meses"][0]
    assert enero["dias_del_mes"] == 31
    assert enero["dias_en_intervalo"] == 31
    assert enero["dias_con_cobertura"] == 3
    assert enero["dias_observados"] == 3
    assert enero["dias_fuera_intervalo"] == 0
    assert "3 días observados de 31" in enero["motivo"]
    assert cuerpo["cobertura_ventas"]["dias_fuera_de_cobertura"] == 28
    assert cuerpo["dominio"]["desde"] == "2011-01-01"
    # Fuera del dominio descriptivo es un error explícito.
    assert (
        cliente_http.get(
            "/api/v1/comercial/m5/estacionalidad",
            params={"estado": "CA", "desde": "2010-01-01", "hasta": "2010-12-31"},
        ).status_code
        == 422
    )


# --------------------------------------------------------------------------- #
# Matriz temporal
# --------------------------------------------------------------------------- #


def test_matriz_temporal_es_el_anio_completo(cliente):
    """El eje es el año del calendario, con estados distintos para cada fecha."""
    cliente_http = cliente["cliente"]
    datos = cliente_http.get(
        "/api/v1/comercial/m5/matriz-temporal", params={"estado": "CA", "anio": 2011}
    ).json()
    assert len(datos["dias"]) == 365, "2011 se representa completo aunque los datos empiecen el 29"
    assert datos["dias"][0]["fecha"] == "2011-01-01"
    assert datos["dias"][-1]["fecha"] == "2011-12-31"
    sin_dato = [dia for dia in datos["dias"] if dia["estado"] == "sin_dato"]
    # El conjunto sintético cubre 70 días: faltan los 28 previos y el resto del año.
    assert len(sin_dato) == 365 - m5_fixture.DIAS
    assert {dia["fecha"] for dia in sin_dato if dia["mes"] == 1} == {
        f"2011-01-{dia:02d}" for dia in range(1, 29)
    }
    assert datos["cobertura"]["dias_del_anio"] == 365
    assert datos["cobertura"]["celdas_sin_dato"] == (365 - m5_fixture.DIAS) * len(datos["tiendas"])
    assert datos["cobertura"]["celdas_fuera_filtro"] == 0
    fila = datos["tiendas"][0]
    assert len(fila["valores"]) == 365
    assert fila["valores"][0] is None, "el 1 de enero no tiene valor"
    assert datos["escala"]["dominio"][1] == max(v for v in fila["valores"] if v is not None)
    # Un año sin días en el periodo es un error explícito.
    assert (
        cliente_http.get(
            "/api/v1/comercial/m5/matriz-temporal", params={"estado": "CA", "anio": 2009}
        ).status_code
        == 422
    )


def test_matriz_temporal_marca_fuera_del_filtro(cliente):
    """Una fecha con cobertura pero fuera del periodo pedido es «fuera del filtro»."""
    datos = cliente["cliente"].get(
        "/api/v1/comercial/m5/matriz-temporal",
        params={"estado": "CA", "anio": 2011, "desde": "2011-02-01", "hasta": "2011-02-28"},
    ).json()
    assert len(datos["dias"]) == 365
    fuera = [dia for dia in datos["dias"] if dia["estado"] == "fuera_del_filtro"]
    # Toda fecha con cobertura fuera de febrero es «fuera del filtro»: 29-31 ene, marzo y
    # los ocho primeros días de abril (el conjunto sintético dura 70 días).
    assert len(fuera) == 3 + 31 + 8
    assert {dia["fecha"] for dia in fuera if dia["mes"] == 1} == {
        "2011-01-29",
        "2011-01-30",
        "2011-01-31",
    }
    assert datos["cobertura"]["celdas_fuera_filtro"] == len(fuera) * len(datos["tiendas"])
    # Los totales observados no cambian con la corrección: febrero completo.
    assert datos["tiendas"][0]["dias_observados"] == 28


def test_matriz_temporal_incluye_ceros_observados(cliente):
    """WI_1 HOBBIES es todo ceros: la celda debe ser 0, no ausencia."""
    cliente_http = cliente["cliente"]
    datos = cliente_http.get(
        "/api/v1/comercial/m5/matriz-temporal",
        params={"estado": "WI", "anio": 2011, "categorias": ["HOBBIES"]},
    ).json()
    fila = datos["tiendas"][0]
    observados = [valor for valor in fila["valores"] if valor is not None]
    assert observados and all(valor == 0 for valor in observados)
    assert fila["dias_observados"] == m5_fixture.DIAS
    assert datos["cobertura"]["celdas_sin_dato"] == 365 - m5_fixture.DIAS
    assert datos["cobertura"]["celdas_observadas"] == m5_fixture.DIAS


# --------------------------------------------------------------------------- #
# Precios
# --------------------------------------------------------------------------- #


@pytest.fixture()
def precios_importados(cliente):
    """Precios sintéticos: un artículo con dos semanas y otro sin precio."""
    db, carpeta, version = cliente["db"], cliente["carpeta"], cliente["version"]
    semanas = _semanas(db)
    _escribir_precios(
        carpeta,
        [
            ("CA_1", "FOODS_1_001", semanas[0], "1.50"),
            ("CA_1", "FOODS_1_001", semanas[1], "1.50"),
            ("CA_1", "FOODS_1_001", semanas[2], "2.00"),
            ("CA_1", "FOODS_1_002", semanas[2], "3.00"),
        ],
    )
    manifiesto = precios.importar_precios(db, version, carpeta)
    cliente["version_precios"] = manifiesto["version_precios"]
    cliente["semanas"] = semanas
    return cliente


def test_precios_distribucion_ponderada_y_ausente_no_cero(precios_importados):
    cliente_http = precios_importados["cliente"]
    datos = cliente_http.get(
        "/api/v1/comercial/m5/precios/distribucion", params={"tienda": "CA_1"}
    ).json()
    assert datos["combinaciones_incluidas"] == 3
    assert datos["articulos"] == 2, "solo dos artículos tienen precio en el periodo"
    cobertura = datos["cobertura"]
    assert (
        cobertura["completa"] + cobertura["parcial"] + cobertura["sin_precio"]
        == datos["combinaciones_incluidas"]
    ), "el desglose debe sumar el universo"
    assert cobertura["sin_precio"] == 1
    # FOODS_1_001 tiene precio en las tres semanas; FOODS_1_002 solo en la última.
    assert cobertura["completa"] == 1
    assert cobertura["parcial"] == 1
    assert cobertura["pct_dias_cubiertos"] is not None
    assert datos["sin_precio"]["combinaciones"] == 1
    valores = {valor["item_id"]: valor for valor in datos["valores"]}
    assert valores["HOBBIES_1_001"]["precio_medio"] is None
    # Media ponderada por días cubiertos: 1,50 (dos semanas) y 2,00 (una semana).
    esperado = (1.50 * 14 + 2.00 * 7) / 21 if datos["periodo"]["dias"] >= 21 else None
    if esperado is not None:
        assert valores["FOODS_1_001"]["precio_medio"] == pytest.approx(esperado, abs=1e-4)
    assert datos["cuartiles"] is not None
    assert datos["cuartiles"]["mediana"] <= datos["cuartiles"]["max"]
    assert "no se pondera por unidades" in datos["regla_ponderacion"]


def test_precios_cambios_cuentan_centavos_y_umbral_es_explicito(precios_importados):
    """1,50 → 1,50 → 2,00: dos transiciones (una sin cambio y un aumento)."""
    cliente_http = precios_importados["cliente"]
    datos = cliente_http.get(
        "/api/v1/comercial/m5/precios/cambios", params={"tienda": "CA_1"}
    ).json()
    # Ya no hay tolerancia técnica: el umbral es comercial, explícito y 0 por omisión.
    assert datos["umbral"]["valor_usd"] == 0.0
    assert "comercial" in datos["umbral"]["tipo"]
    assert "centavos" in datos["umbral"]["precision"]
    assert datos["precision_comparacion"] == "centavos"
    assert datos["totales"]["transiciones"] >= 2
    assert datos["totales"]["aumentos"] >= 1
    assert (
        datos["totales"]["transiciones"]
        == datos["totales"]["aumentos"]
        + datos["totales"]["disminuciones"]
        + datos["totales"]["sin_cambio"]
    )
    assert datos["series_con_precio"] == 2
    assert "no es una promoción" in datos["nota"]


def test_precios_cambios_detectan_un_centavo(precios_importados):
    """Un cambio de un centavo cuenta; un precio idéntico no; el ruido flotante tampoco."""
    import numpy as np

    from app.comercial import precios_analisis as pa

    serie = np.array(
        [
            [2.00],
            [2.01],  # aumento de un centavo
            [2.01],  # sin cambio
            [2.00],  # disminución de un centavo
            [2.68],  # valor que en binario es 2.6799999…
            [2.68],
        ],
        dtype=np.float64,
    )
    conteos = pa._transiciones(serie)
    assert int(conteos["aumentos"][0]) == 2  # 2,00 → 2,01 y 2,00 → 2,68
    assert int(conteos["disminuciones"][0]) == 1  # 2,01 → 2,00
    assert int(conteos["sin_cambio"][0]) == 2  # 2,01 → 2,01 y 2,68 → 2,68
    # Con umbral comercial de 5 centavos, los cambios de un centavo dejan de contar.
    con_umbral = pa._transiciones(serie, 0.05)
    assert int(con_umbral["aumentos"][0]) == 1
    assert int(con_umbral["disminuciones"][0]) == 0


def test_precios_evolucion_sin_precio_devuelve_motivo(precios_importados):
    cliente_http = precios_importados["cliente"]
    con_precio = cliente_http.get(
        "/api/v1/comercial/m5/precios/evolucion",
        params={"tienda": "CA_1", "item": "FOODS_1_001"},
    ).json()
    assert len(con_precio["puntos"]) == 3
    assert con_precio["motivo"] is None
    assert con_precio["cambios"]["aumentos"] == 1
    sin_precio = cliente_http.get(
        "/api/v1/comercial/m5/precios/evolucion",
        params={"tienda": "CA_1", "item": "HOBBIES_1_001"},
    ).json()
    assert sin_precio["puntos"] == []
    assert sin_precio["motivo"]


def test_precios_no_duplican_ventas(precios_importados):
    """Leer precios no modifica los bloques de ventas."""
    db = precios_importados["db"]
    antes = {d["_id"]: d["total"] for d in db[M5_BLOQUES].find({}, {"total": 1})}
    precios_importados["cliente"].get(
        "/api/v1/comercial/m5/precios/distribucion", params={"tienda": "CA_1"}
    )
    despues = {d["_id"]: d["total"] for d in db[M5_BLOQUES].find({}, {"total": 1})}
    assert antes == despues


# --------------------------------------------------------------------------- #
# Intermitencia y detalle de artículo
# --------------------------------------------------------------------------- #


def test_intermitencia_incluye_series_de_todo_cero(cliente):
    datos = cliente["cliente"].get(
        "/api/v1/comercial/m5/intermitencia",
        params={"tienda": "WI_1", "categorias": ["HOBBIES"], "tamano": 5},
    ).json()
    assert datos["resumen"]["series"] == 1
    assert datos["resumen"]["series_todas_cero"] == 1
    fila = datos["filas"][0]
    assert fila["pct_cero"] == 100.0
    assert fila["racha_maxima_ceros"] == 70
    assert fila["primera_venta_positiva"] is None
    assert "no cuenta como cero" in datos["reglas"]


def test_intermitencia_racha_no_atraviesa_huecos(cliente):
    """La racha se calcula sobre días observados: un hueco la corta."""
    datos = cliente["cliente"].get(
        "/api/v1/comercial/m5/intermitencia", params={"tienda": "CA_1", "tamano": 25}
    ).json()
    for fila in datos["filas"]:
        assert fila["dias_cero"] + fila["dias_positivos"] == fila["dias_validos"]
        assert fila["racha_maxima_ceros"] <= fila["dias_cero"]


def test_articulo_snap_del_estado_correcto(cliente):
    """SNAP sale del flag del estado de la tienda, no de una regla genérica."""
    ca = cliente["cliente"].get(
        "/api/v1/comercial/m5/articulo", params={"item": "FOODS_1_001", "tienda": "CA_1"}
    ).json()
    assert ca["articulo"]["estado"] == "CA"
    assert ca["capas"]["estado"] == "CA"
    # El fixture marca SNAP de California cuando el día del mes es múltiplo de 10.
    assert all(date.fromisoformat(marca["fecha"]).day % 10 == 0 for marca in ca["capas"]["snap"])
    fechas = [venta["fecha"] for venta in ca["ventas"]]
    esperados = [fecha for fecha in fechas if date.fromisoformat(fecha).day % 10 == 0]
    assert [marca["fecha"] for marca in ca["capas"]["snap"]] == esperados
    tx = cliente["cliente"].get(
        "/api/v1/comercial/m5/articulo", params={"item": "FOODS_1_001", "tienda": "TX_1"}
    ).json()
    assert tx["capas"]["snap"] == [], "Texas no tiene SNAP en el conjunto sintético"
    assert len(tx["ventas"]) == m5_fixture.DIAS
    assert tx["intermitencia"]["dias_validos"] == m5_fixture.DIAS


def test_articulo_inexistente_es_error(cliente):
    respuesta = cliente["cliente"].get(
        "/api/v1/comercial/m5/articulo", params={"item": "NO_EXISTE", "tienda": "CA_1"}
    )
    assert respuesta.status_code == 422
    assert respuesta.json()["detail"]["estado"] == "filtro_invalido"


# --------------------------------------------------------------------------- #
# Demanda futura: matriz de desempeño por corte y horizonte
# --------------------------------------------------------------------------- #

RUN = "run-calendario-prueba0002"


@pytest.fixture()
def matriz_publicada(cliente):
    """Publica una evaluación mínima de 2 orígenes × 28 horizontes."""
    db, version = cliente["db"], cliente["version"]
    horizonte = 28
    db[M5_MODELOS].insert_one(
        {
            "_id": RUN,
            "run_id": RUN,
            "esquema": "m5-modelo-1",
            "version": version,
            "variante": "calendario",
            "etiqueta": "Rezagos + calendario",
            "metodo": "LightGBM global condicionado al horizonte",
            "estado": "publicada",
            "recomendado": True,
            "horizonte": horizonte,
            "variables": ["h", "lag_1"],
            "cobertura": {"series": 30490, "series_modelo": 30490, "fallback": 0},
            "supuestos": {"precio_futuro": "último precio conocido"},
        }
    )
    for origen in (100, 128):
        db[M5_EVALUACION_MODELOS].insert_one(
            {
                "_id": f"{RUN}:final:{origen}",
                "run_id": RUN,
                "version": version,
                "etapa": "final",
                "origen_d": origen,
                "origen_fecha": "2011-05-08",
                "fin_fecha": "2011-06-04",
                "series": 9,
                "observaciones": 9 * horizonte,
                "candidato": {"n": 252, "mae": 1.0, "sesgo": 0.1, "wape": 10.0, "denominador": 2520},
                "baseline": {"n": 252, "mae": 2.0, "sesgo": -0.2, "wape": 20.0, "denominador": 2520},
                "mejora_pct": 50.0,
                "por_horizonte": [
                    {
                        "horizonte": paso,
                        "fecha": f"2011-05-{paso:02d}",
                        "candidato": {
                            "n": 9 if paso < 28 else 0,
                            "mae": 1.0 if paso < 28 else None,
                            "sesgo": 0.1 if paso < 28 else None,
                            "wape": 10.0 if paso < 28 else None,
                            "denominador": 90 if paso < 28 else 0,
                        },
                        "baseline": {
                            "n": 9 if paso < 28 else 0,
                            "mae": 2.0 if paso < 28 else None,
                            "sesgo": -0.2 if paso < 28 else None,
                            "wape": 20.0 if paso < 28 else None,
                            "denominador": 90 if paso < 28 else 0,
                        },
                    }
                    for paso in range(1, horizonte + 1)
                ],
                "segmentos": [],
            }
        )
    return cliente


def test_matriz_horizontes_ventaja_y_sesgo(matriz_publicada):
    datos = matriz_publicada["cliente"].get(
        "/api/v1/comercial/m5/matriz-horizontes", params={"etapa": "final"}
    ).json()
    assert datos["run_id"] == RUN
    assert datos["etapa"] == "final"
    assert len(datos["origenes"]) == 2
    fila = datos["origenes"][0]
    assert len(fila["celdas"]) == 28
    celda = fila["celdas"][0]
    assert celda["mae_candidato"] == 1.0 and celda["mae_baseline"] == 2.0
    assert celda["ventaja"] == pytest.approx(1.0), "MAE_base − MAE_cand"
    assert celda["sesgo_candidato"] == 0.1
    assert datos["escala"]["max_abs_ventaja"] == pytest.approx(1.0)
    # Una celda sin observaciones no es un cero: va con motivo.
    ultima = fila["celdas"][-1]
    assert ultima["n"] == 0
    assert ultima["mae_candidato"] is None and ultima["ventaja"] is None
    assert ultima["motivo"]
    assert "artículo × tienda" in datos["formulas"]["grano"]


def test_matriz_horizontes_sin_ejecucion(matriz_publicada):
    respuesta = matriz_publicada["cliente"].get(
        "/api/v1/comercial/m5/matriz-horizontes",
        params={"etapa": "final", "run_id": "run-no-existe"},
    )
    assert respuesta.status_code == 503
