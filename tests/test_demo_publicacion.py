"""Pruebas del paquete de publicación compacto para la demo.

Cubren las piezas que pueden romper la demo sin que se note en pantalla:

* el lector de precios compatible con los dos esquemas (base local y paquete compacto),
  incluida la garantía de que los centavos enteros representan el precio con dos decimales
  sin pérdida y de que «sin precio» nunca se convierte en cero;
* las guardas de seguridad de la generación y de la importación (nada de Atlas, nada de
  hosts remotos, base destino explícita con sufijo y distinta de la base de trabajo);
* la validación del paquete: detecta una revisión activa manipulada y rechaza una base vacía.

No requieren el paquete real: construyen documentos mínimos en la base de pruebas.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from bson.binary import Binary

from app.comercial import precios_almacen
from app.comercial.conexion import HOSTS_LOCALES
from app.db.mongo import M5_PRECIOS, M5_PRECIOS_CATALOGO, M5_PUBLICACION
from scripts.generar_demo_publicacion import _validar_destino_local
from scripts.importar_demo_publicacion import importar
from scripts.validar_demo_publicacion import ESQUEMA, MANIFIESTO, validar_base

VERSION = "m5-precios-prueba-paquete"
TIENDA = "CA_1"
ARTICULOS = ["FOODS_1_001", "FOODS_1_002", "FOODS_3_090"]


def _centavos(valores: list[int]) -> Binary:
    import numpy as np

    return Binary(np.asarray(valores, dtype=np.int32).tobytes())


@pytest.fixture()
def precios_compactos(mongo_db):
    """Una tienda en esquema compacto: catálogo + tres semanas con centavos binarios."""
    mongo_db[M5_PRECIOS_CATALOGO].insert_one(
        {
            "_id": f"{VERSION}:{TIENDA}",
            "version_precios": VERSION,
            "store_id": TIENDA,
            "items": ARTICULOS,
            "items_total": len(ARTICULOS),
        }
    )
    semanas = {
        11101: [123, 0, 456],  # 1.23, sin precio, 4.56
        11102: [0, 0, 999],  # sin precio, sin precio, 9.99
        11103: [110, 250, 0],  # 1.10, 2.50, sin precio
    }
    for semana, valores in semanas.items():
        mongo_db[M5_PRECIOS].insert_one(
            {
                "_id": f"{VERSION}:{TIENDA}:{semana}",
                "version_precios": VERSION,
                "store_id": TIENDA,
                "wm_yr_wk": semana,
                "items_total": len(ARTICULOS),
                "con_precio": sum(1 for valor in valores if valor > 0),
                "centavos": _centavos(valores),
            }
        )
    try:
        yield semanas
    finally:
        mongo_db[M5_PRECIOS].delete_many({"version_precios": VERSION})
        mongo_db[M5_PRECIOS_CATALOGO].delete_many({"version_precios": VERSION})


def test_lee_centavos_binarios_sin_perder_precision(mongo_db, precios_compactos):
    semanas = precios_almacen.semanas_tienda(mongo_db, VERSION, TIENDA)

    assert set(semanas) == {11101, 11102, 11103}
    assert semanas[11101] == {"FOODS_1_001": 1.23, "FOODS_3_090": 4.56}
    assert semanas[11102] == {"FOODS_3_090": 9.99}
    assert semanas[11103] == {"FOODS_1_001": 1.10, "FOODS_1_002": 2.50}
    # El centinela 0 significa «sin precio»: no aparece como precio cero.
    assert all(precio > 0 for semana in semanas.values() for precio in semana.values())


def test_esquema_legado_y_compacto_dan_el_mismo_resultado(mongo_db, precios_compactos):
    compacto = precios_almacen.semanas_tienda(mongo_db, VERSION, TIENDA)

    # Los mismos precios con el esquema de la base local: listas paralelas en float.
    mongo_db[M5_PRECIOS].delete_many({"version_precios": VERSION})
    for semana, valores in precios_compactos.items():
        items = [item for item, valor in zip(ARTICULOS, valores, strict=True) if valor > 0]
        precios = [round(valor / 100.0, 2) for valor in valores if valor > 0]
        mongo_db[M5_PRECIOS].insert_one(
            {
                "version_precios": VERSION,
                "store_id": TIENDA,
                "wm_yr_wk": semana,
                "items": items,
                "precios": precios,
                "version_ventas": "m5-prueba",
            }
        )
    legado = precios_almacen.semanas_tienda(mongo_db, VERSION, TIENDA)
    assert legado == compacto


def test_semanas_pedidas_filtran_y_no_inventan_semanas(mongo_db, precios_compactos):
    parcial = precios_almacen.precios_por_semana(mongo_db, VERSION, TIENDA, [11101, 11103, 55555])
    assert set(parcial) == {11101, 11103}
    assert parcial[11103]["FOODS_1_002"] == 2.50
    assert precios_almacen.precios_por_semana(mongo_db, VERSION, TIENDA, []) == {}


@pytest.mark.parametrize(
    "precio", [0.01, 0.29, 0.7, 1.23, 4.56, 9.99, 12.5, 99.95, 1234.56]
)
def test_centavos_enteros_son_exactos(precio: float):
    """El paquete guarda centavos: la conversión de ida y vuelta no cambia el precio."""
    centavos = int(round(precio * 100))
    assert centavos > 0
    assert centavos / 100.0 == pytest.approx(precio, abs=0.0, rel=0.0)


# ·· guardas de la generación ··································································


def test_generacion_acepta_destino_local_con_sufijo():
    assert {"127.0.0.1", "localhost"} <= HOSTS_LOCALES
    _validar_destino_local(
        "mongodb://127.0.0.1:27017/atlas_comercial_local",
        "atlas_comercial_demo",
        "atlas_comercial_local",
    )


@pytest.mark.parametrize(
    ("uri", "destino", "fuente", "mensaje"),
    [
        (
            "mongodb+srv://usuario:clave@cluster.mongodb.net/atlas_comercial_local",
            "atlas_comercial_demo",
            "atlas_comercial_local",
            "srv",
        ),
        (
            "mongodb://cluster0.mongodb.net:27017/atlas_comercial_local",
            "atlas_comercial_demo",
            "atlas_comercial_local",
            "local",
        ),
        (
            "mongodb://127.0.0.1:27017/atlas_comercial_local",
            "atlas_comercial_local",
            "atlas_comercial_local",
            "distinta",
        ),
        (
            "mongodb://127.0.0.1:27017/atlas_comercial_local",
            "demo_sin_sufijo",
            "atlas_comercial_local",
            "sufijo",
        ),
    ],
)
def test_generacion_rechaza_destinos_no_permitidos(uri, destino, fuente, mensaje):
    with pytest.raises(SystemExit) as error:
        _validar_destino_local(uri, destino, fuente)
    texto = str(error.value).lower()
    assert mensaje in texto or "permitido" in texto or "_demo, _pub o _demo_test" in texto


# ·· guardas de la importación ·································································


def test_importacion_exige_base_destino_explicita(tmp_path):
    with pytest.raises(SystemExit) as error:
        importar(tmp_path, "")
    assert "destino" in str(error.value).lower()


def test_importacion_rechaza_la_base_de_trabajo(tmp_path):
    """La base de trabajo nunca es destino: la guarda de sufijo ya la excluye."""
    with pytest.raises(SystemExit) as error:
        importar(tmp_path, "atlas_comercial_local")
    assert "sufijo" in str(error.value).lower()


def test_importacion_rechaza_destino_sin_sufijo(tmp_path):
    with pytest.raises(SystemExit) as error:
        importar(tmp_path, "atlas_comercial")
    assert "sufijo" in str(error.value).lower()


# ·· validación del paquete ····································································


def test_validacion_rechaza_base_vacia(mongo_db):
    informe = validar_base(mongo_db)
    assert informe["ok"] is False
    assert informe["hallazgos"]


def test_validacion_detecta_revision_activa_manipulada(mongo_db):
    manifiesto = {
        "_id": ESQUEMA,
        "esquema_paquete": ESQUEMA,
        "revision": "rev-esperada",
        "version": "m5-prueba",
    }
    mongo_db[MANIFIESTO].replace_one({"_id": ESQUEMA}, manifiesto, upsert=True)
    mongo_db[M5_PUBLICACION].insert_one({"_id": "rev-manipulada", "activa": True})
    try:
        informe = validar_base(mongo_db, json.loads(json.dumps(manifiesto)))
        assert informe["ok"] is False
        assert any("revisión" in hallazgo for hallazgo in informe["hallazgos"])
    finally:
        mongo_db[MANIFIESTO].delete_many({"_id": ESQUEMA})
        mongo_db[M5_PUBLICACION].delete_many({"_id": "rev-manipulada"})


# ·· ventanas de evaluación del detalle de serie ············································


def test_detalle_no_duplica_las_ventanas_de_la_revision_anterior(mongo_db):
    """El paquete solo trae la revisión activa; la base de trabajo conserva las anteriores.

    Antes de corregirlo, el detalle de serie leía `m5_evaluacion_series` sin filtrar por
    revisión: cada corte aparecía dos veces (una con enteros y otra con decimales) y la ficha
    declaraba el doble de ventanas de las publicadas. La comparación entre ambas bases lo
    destapó.
    """
    from app.comercial import planificacion
    from app.db.mongo import (
        M5_BLOQUES,
        M5_CALENDARIO,
        M5_EVALUACION_SERIES,
        M5_PLANIFICACION,
    )

    version = "m5-prueba-ventanas"
    run_id = "run-prueba"
    colecciones = (
        M5_PUBLICACION,
        M5_PLANIFICACION,
        M5_EVALUACION_SERIES,
        M5_CALENDARIO,
        M5_BLOQUES,
    )
    for coleccion in colecciones:
        mongo_db[coleccion].delete_many({})
    mongo_db[M5_PUBLICACION].insert_one({"_id": "rev-activa", "activa": True, "politica": "decimal-v1"})
    mongo_db[M5_PLANIFICACION].insert_one(
        {
            "version": version,
            "run_id": run_id,
            "revision": "rev-activa",
            "store_id": "CA_1",
            "item_id": "FOODS_1_001",
            "cat_id": "FOODS",
            "dept_id": "FOODS_1",
            "origen_d": 3,
            "origen_fecha": "2016-05-22",
            "horizonte_inicio": "2016-05-23",
            "horizonte_fin": "2016-06-19",
            "observado_desde": "2016-04-25",
            "observado_hasta": "2016-05-22",
            "pronostico_28": 4.4,
            "observado_28": 2.0,
            "baseline_28": 2.0,
            "diferencia": 2.4,
            "semanas": [1.1, 1.1, 1.1, 1.1],
            "semanas_baseline": [0.5, 0.5, 0.5, 0.5],
            "semanas_observado": [0.5, 0.5, 0.5, 0.5],
            "evaluacion": {"estado": "mixto", "cortes": 2, "mejoran": 1, "empeoran": 1, "empatan": 0},
        }
    )
    for dia in range(1, 6):
        mongo_db[M5_CALENDARIO].insert_one(
            {"d": dia, "fecha": datetime(2016, 5, 20 + dia)}
        )
    mongo_db[M5_BLOQUES].insert_one(
        {
            "version": version,
            "store_id": "CA_1",
            "item_id": "FOODS_1_001",
            "d_inicio": 1,
            "d_fin": 5,
            "unidades": [1, 2, 3, 4, 5],
        }
    )
    # Dos ventanas de la revisión activa y sus dos copias de la publicación anterior (sin campo).
    for origen_d, fin, error in ((1885, "2016-04-24", -3.684), (1913, "2016-05-22", 16.352)):
        for revision in ("rev-activa", None):
            documento = {
                "version": version,
                "run_id": run_id,
                "store_id": "CA_1",
                "item_id": "FOODS_1_001",
                "origen_d": origen_d,
                "origen_fecha": "2016-03-27",
                "fin_fecha": fin,
                "observado_28": 20.0,
                "pronostico_28": 20.0,
                "error_28": error,
                "error_absoluto_28": abs(error),
                "baseline_28": 24.0,
                "baseline_error_28": -9.0,
            }
            if revision:
                documento["revision"] = revision
            mongo_db[M5_EVALUACION_SERIES].insert_one(documento)

    try:
        assert mongo_db[M5_EVALUACION_SERIES].count_documents({}) == 4
        detalle = planificacion.detalle_serie(
            mongo_db, version, run_id, tienda="CA_1", item="FOODS_1_001"
        )
        assert detalle is not None
        ventanas = detalle["evaluacion_horizonte"]
        assert [ventana["origen_d"] for ventana in ventanas] == [1885, 1913]
        assert ventanas[0]["error_28"] == pytest.approx(-3.684)
        assert detalle["evaluacion_resumen"]["cortes"] == 2
    finally:
        for coleccion in colecciones:
            mongo_db[coleccion].delete_many({})
