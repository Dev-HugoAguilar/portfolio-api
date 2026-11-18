"""Pruebas de la política de precisión y de la revisión de publicación.

Reproducen los defectos corregidos:

* la publicación guardaba un entero por día y su total no coincidía con el artefacto
  (1 289 637 u frente a 1 335 219 u con −4 % de desviación), así que tabla, evaluación y CSV
  mostraban cifras distintas de la serie canónica;
* sin filtro de revisión convivían las dos publicaciones y el detalle se contaba dos veces;
* la serie observada de los niveles «departamento» y «departamento × tienda» se leía con la
  clave equivocada del agregado y la gráfica de evaluación se quedaba sin historia;
* el manifiesto declaraba una política de precios en una variante sin variables de precio.

No requieren la descarga de Kaggle: trabajan con documentos mínimos en la base de pruebas.
"""

from __future__ import annotations

import pytest

from app.comercial import consultas, planificacion
from app.db.mongo import (
    M5_AGREGADOS,
    M5_EVALUACION_MODELOS,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PUBLICACION,
)
from app.services.comercial_service import _reconciliar_predicciones, supuestos_modelo

VERSION = "m5-prueba-precision"
RUN = "run-calendario-prueba"


class RepositorioFalso:
    """Repositorio mínimo: la base de datos y la publicación activa."""

    def __init__(self, db, publicacion):
        self._db = db
        self._publicacion = publicacion

    def publicacion(self):
        return self._publicacion


def _indices_con_revision(db) -> None:
    """Deja los índices como los crea la publicación: la revisión forma parte de la clave.

    Un índice antiguo por ``run_id``/``origen_d`` impediría guardar dos revisiones del mismo
    corte, que es justo lo que permite volver a la publicación anterior.
    """
    for coleccion, claves in (
        (M5_PREDICCIONES, [("revision", 1), ("origen_d", 1), ("dimension", 1)]),
        (M5_PREDICCIONES_SERIES, [("revision", 1), ("store_id", 1), ("item_id", 1)]),
    ):
        for nombre, informacion in db[coleccion].index_information().items():
            if nombre == "_id_":
                continue
            if "revision" not in dict(informacion["key"]):
                db[coleccion].drop_index(nombre)
        db[coleccion].create_index(claves)


@pytest.fixture()
def publicaciones(mongo_db):
    _indices_con_revision(mongo_db)
    mongo_db[M5_PUBLICACION].delete_many({})
    mongo_db[M5_PREDICCIONES].delete_many({})
    mongo_db[M5_PREDICCIONES_SERIES].delete_many({})
    mongo_db[M5_EVALUACION_MODELOS].delete_many({})
    mongo_db[M5_AGREGADOS].delete_many({})
    mongo_db[M5_PUBLICACION].insert_many(
        [
            {
                "_id": "rev-decimal",
                "activa": True,
                "politica": "decimal-v1",
                "precision": "decimal; el redondeo se aplica solo al presentar",
                "version": VERSION,
                "run_id": RUN,
                "anterior": "rev-enteros",
            },
            {
                "_id": "rev-enteros",
                "activa": False,
                "politica": "enteros-por-dia",
                "precision": "un entero por día",
                "version": VERSION,
                "run_id": RUN,
                "estado": "anterior",
            },
        ]
    )
    yield mongo_db
    mongo_db[M5_PUBLICACION].delete_many({})
    mongo_db[M5_PREDICCIONES].delete_many({})
    mongo_db[M5_PREDICCIONES_SERIES].delete_many({})
    mongo_db[M5_EVALUACION_MODELOS].delete_many({})
    mongo_db[M5_AGREGADOS].delete_many({})


def _sembrar_predicciones(db) -> None:
    """Dos series por revisión con la misma suma canónica 2,5 + 3,5 = 6,0.

    La revisión antigua guardaba 2 y 4 redondeando cada serie por día, y su agregado sumaba 6;
    la revisión decimal guarda 2,5 y 3,5 con el mismo agregado 6,0 sin perder precisión.
    """
    for revision, (izquierda, derecha) in {
        "rev-decimal": (2.5, 3.5),
        "rev-enteros": (2.0, 4.0),
    }.items():
        db[M5_PREDICCIONES].insert_one(
            {
                "version": VERSION,
                "run_id": RUN,
                "revision": revision,
                "origen_d": 1941,
                "dimension": "total",
                "clave": "total",
                "pronostico": [izquierda + derecha],
                "baseline": [izquierda + derecha],
            }
        )
        for item, valor in {"ITEM_1": izquierda, "ITEM_2": derecha}.items():
            db[M5_PREDICCIONES_SERIES].insert_one(
                {
                    "version": VERSION,
                    "run_id": RUN,
                    "revision": revision,
                    "origen_d": 1941,
                    "item_id": item,
                    "store_id": "CA_1",
                    "pronostico": [valor],
                    "baseline": [valor],
                }
            )
        db[M5_EVALUACION_MODELOS].insert_one(
            {
                "version": VERSION,
                "run_id": RUN,
                "revision": revision,
                "etapa": "final",
                "origen_d": 1941,
                "series": 2,
            }
        )


def test_publicacion_activa_y_revision_por_defecto(publicaciones):
    """Solo la publicación marcada como activa gobierna las respuestas."""
    activa = planificacion.publicacion_activa(publicaciones)
    assert activa is not None
    assert activa["_id"] == "rev-decimal"
    assert planificacion.revision_para(publicaciones) == "rev-decimal"
    assert planificacion.revision_para(publicaciones, "rev-enteros") == "rev-enteros"


def test_filtro_de_revision_separa_las_dos_publicaciones(publicaciones):
    """Sin filtro de revisión las dos publicaciones se mezclan y duplican el detalle."""
    _sembrar_predicciones(publicaciones)
    base = {"version": VERSION, "run_id": RUN}
    assert publicaciones[M5_PREDICCIONES_SERIES].count_documents(base) == 4
    assert (
        publicaciones[M5_PREDICCIONES_SERIES].count_documents(
            planificacion._filtro_revision(base, "rev-decimal")
        )
        == 2
    )
    assert (
        publicaciones[M5_PREDICCIONES_SERIES].count_documents(
            planificacion._filtro_revision(base, None)
        )
        == 0
    ), "una publicación sin campo `revision` no puede colarse en la revisión vigente"


def test_reconciliacion_cuenta_una_sola_revision(publicaciones):
    """El agregado reconcilia con el detalle de la misma revisión, sin duplicar series."""
    _sembrar_predicciones(publicaciones)
    repo = RepositorioFalso(publicaciones, planificacion.publicacion_activa(publicaciones))
    resultado = _reconciliar_predicciones(
        repo, VERSION, RUN, [{"origen_d": 1941}], revision="rev-decimal"
    )
    assert resultado["cuadra"] is True
    assert resultado["series"] == 2, "el detalle debe traer una serie por documento, no dos"
    assert resultado["max_diferencia_absoluta"] == pytest.approx(0.0)
    assert resultado["revision"] == "rev-decimal"
    assert "decimal" in resultado["nota"]


def test_reconciliacion_detecta_redondeo_por_dia(publicaciones):
    """Con la política en enteros la tolerancia es amplia; con decimales, una millonésima."""
    _sembrar_predicciones(publicaciones)
    publicaciones[M5_PREDICCIONES_SERIES].insert_one(
        {
            "version": VERSION,
            "run_id": RUN,
            "revision": "rev-decimal",
            "origen_d": 1941,
            "item_id": "ITEM_3",
            "store_id": "CA_1",
            "pronostico": [0.4],
            "baseline": [0.4],
        }
    )
    publicaciones[M5_PREDICCIONES].update_one(
        {"revision": "rev-decimal"},
        {"$set": {"pronostico": [6.0], "baseline": [6.0]}},
    )
    repo = RepositorioFalso(publicaciones, planificacion.publicacion_activa(publicaciones))
    resultado = _reconciliar_predicciones(
        repo, VERSION, RUN, [{"origen_d": 1941}], revision="rev-decimal"
    )
    assert resultado["cuadra"] is False
    assert resultado["tolerancia"] == pytest.approx(1e-6)


def test_publicacion_declara_politica_y_anterior_recuperable(publicaciones):
    """La revisión activa declara su política de precisión y cuál es la anterior recuperable."""
    activa = planificacion.publicacion_activa(publicaciones)
    anterior = publicaciones[M5_PUBLICACION].find_one({"_id": activa["anterior"]})
    assert activa["politica"] == "decimal-v1"
    assert "decimal" in activa["precision"]
    assert anterior is not None, "la publicación anterior debe seguir guardada para poder revertir"
    assert anterior["activa"] is False
    assert anterior["politica"] == "enteros-por-dia"


def test_historico_traduce_la_clave_del_agregado(mongo_db):
    """La historia de departamento y departamento × tienda usa la clave del agregado."""
    assert consultas.clave_agregado("total", "total") == "todas"
    assert consultas.clave_agregado("departamento", "FOODS_1") == "FOODS|FOODS_1"
    assert consultas.clave_agregado("departamento_tienda", "FOODS_1|CA_1") == "CA_1|FOODS_1"
    # Una clave que ya viene en formato de agregado no se traduce dos veces.
    assert consultas.clave_agregado("departamento", "FOODS|FOODS_1") == "FOODS|FOODS_1"
    assert consultas.clave_agregado("departamento_tienda", "CA_1|FOODS_1") == "CA_1|FOODS_1"
    assert consultas.clave_agregado("tienda", "CA_1") == "CA_1"

    mongo_db[M5_AGREGADOS].insert_one(
        {
            "version": VERSION,
            "dimension": "departamento",
            "clave": "FOODS|FOODS_1",
            "d_inicio": 1,
            "unidades": list(range(1, 101)),
            "d_fin": 100,
        }
    )
    historia = consultas.historico_antes_de(
        mongo_db, VERSION, nivel="departamento", claves=["FOODS_1"], origen_d=100, dias=5
    )
    assert list(historia) == ["FOODS_1"], "la historia debe devolverse con la clave pedida"
    assert historia["FOODS_1"] == [96, 97, 98, 99, 100]


def test_supuestos_no_congelan_precios_sin_variables_de_precio():
    """La variante de calendario no puede declarar una política de precios que no usa."""
    calendario = {
        "variables": ["h", "dow", "lag_1", "media_7", "snap"],
        "supuestos": {
            "precio_futuro": "El precio de los 28 días es el último conocido.",
            "redondeo": "No se redondea antes de calcular métricas; el vector publicado es entero.",
        },
    }
    supuestos = supuestos_modelo(calendario)
    assert "no usa precios" in supuestos["precio_futuro"].lower()
    assert "decimal-v1" in supuestos["redondeo"]
    assert "entero" not in supuestos["redondeo"]

    con_precios = {
        "variables": ["h", "precio_medio_4s", "precio_cambio_4s"],
        "supuestos": {"precio_futuro": "El precio de los 28 días es el último conocido."},
    }
    assert "último conocido" in supuestos_modelo(con_precios)["precio_futuro"]
