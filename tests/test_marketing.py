"""Pruebas del tablero «Marketing y clientes» (GA4 · BigQuery).

Cubren tres capas, todas **sin red**:

1. **Esquema y SQL**: validación de columnas, normalización de marcadores y las
   garantías que deben estar en el SQL (orden y ventana del embudo, deduplicación
   de compras, sufijo de tablas).
2. **Validación de la ingesta**: el embudo no puede crecer, no puede haber más
   compradores que compras, las cohortes no admiten índices negativos.
3. **API completa** con una versión publicada a partir de una fuente de prueba:
   cifras conciliadas, usuarios distintos (no suma de días), conversión,
   cohortes con ventanas incompletas, filtros OR/AND, tabla, exportación y el
   camino de fallo que conserva la versión publicada.

La fuente de prueba es un *fixture* que solo existe en la base de datos de
pruebas: la aplicación nunca sirve estos datos. Con el entorno sin acceso a
BigQuery, es la única forma de comprobar que el cálculo y la publicación funcionan
de extremo a extremo sin inventar cifras reales.
"""

from __future__ import annotations

import csv
import io

from datetime import date

import pytest

from app.marketing import consultas, esquema, ingesta
from app.models.marketing import MarketingFiltros, normalizar_lista

# ------------------------------------------------------------------- esquema


def test_validacion_detecta_columnas_imprescindibles():
    completo = esquema.Esquema(
        columnas={
            **{columna: tipo for columna, tipo in esquema.REQUERIDAS_BASE.items()},
            **{columna: tipo for columna, tipo in esquema.REQUERIDAS_COMPRA.items()},
        }
    )
    revision = esquema.validar(completo)
    assert revision["valida"] is True
    assert revision["capacidades"]["categoria_producto"] is False
    assert any("item_category" in aviso for aviso in revision["avisos"])

    faltante = esquema.Esquema(
        columnas={k: v for k, v in completo.columnas.items() if k != "user_pseudo_id"}
    )
    revision = esquema.validar(faltante)
    assert revision["valida"] is False
    assert any("user_pseudo_id" in error for error in revision["errores"])


def test_marcadores_se_conservan_como_sin_informacion():
    for valor in ["", "  ", None, "(not set)", "(other)", "<Other>", "NULL"]:
        assert esquema.normalizar(valor) == esquema.SIN_INFORMACION
    assert esquema.normalizar("google") == "google"


def test_sql_garantiza_orden_y_ventana_del_embudo():
    sql = consultas.sql_embudo("20201101", "20210131")
    assert "_TABLE_SUFFIX BETWEEN '20201101' AND '20210131'" in sql
    assert "ga_session_id" in sql
    assert "TIMESTAMP_DIFF" in sql
    assert f"<= {esquema.VENTANA_EMBUDO_HORAS}" in sql
    # Cada paso exige ser posterior al anterior.
    assert "t2 >= t1" in sql and "t3 >= t2" in sql and "t4 >= t3" in sql


def test_sql_deduplica_compras_sobre_todo_el_periodo():
    sql = consultas.sql_compras("20201101", "20210131")
    assert "transaction_id" in sql
    assert "ANY_VALUE(user_pseudo_id)" in sql
    assert "GROUP BY clave_compra" in sql
    # Se queda con el primer día de la compra: así una transacción vista en dos
    # días no se cuenta dos veces y la suma de días coincide con el total.
    assert "MIN(fecha)" in sql
    assert "_TABLE_SUFFIX BETWEEN" in sql


def test_sufijo_de_tabla_normaliza_la_fecha():
    assert ingesta.sufijo_tabla("2020-11-01") == "20201101"
    assert ingesta.sufijo_tabla("20201101") == "20201101"
    assert ingesta.sufijo_tabla(" 2021-01-31 ") == "20210131"
    for invalida in ("", None, "2020-11", "noviembre"):
        with pytest.raises(ValueError):
            ingesta.sufijo_tabla(invalida)


def test_extraer_usa_sufijos_sin_guiones(tmp_path):
    """Las consultas filtran por `_TABLE_SUFFIX` en formato AAAAMMDD."""
    consultas_vistas: list[str] = []

    class FuenteQueRecuerda:
        nombre = "memoria"

        def ejecutar(self, sql, max_filas=200_000):
            consultas_vistas.append(sql)
            return []

    ingesta.extraer(FuenteQueRecuerda(), "2020-11-01", "2020-11-04", tmp_path, 100)
    assert consultas_vistas
    for sql in consultas_vistas:
        assert "_TABLE_SUFFIX BETWEEN '20201101' AND '20201104'" in sql


def test_sql_no_expone_identificadores_en_la_tabla():
    sql = consultas.sql_eventos("20201101", "20210131", 1500)
    # El identificador solo se usa para el hash que ordena la muestra; la
    # proyección final no lo devuelve.
    proyeccion = sql.split("SELECT\n  fecha,", 1)[1]
    assert "user_pseudo_id" not in proyeccion
    assert "orden <= 1500" in sql


# -------------------------------------------------------- validación de la ingesta


def _resumen(fecha="2020-11-02", canal="google / organic", **cambios):
    fila = {
        "fecha": fecha,
        "dispositivo": "desktop",
        "pais": "United States",
        "region": "California",
        "fuente": "google",
        "medio": "organic",
        "eventos": 100,
        "usuarios": 40,
        "sesiones": 55,
        "eventos_sin_sesion": 0,
    }
    fila.update(cambios)
    return fila


def _compra(usuario="u1", ingresos=100.0, fecha="2020-11-02"):
    return {
        "fecha": fecha,
        "user_pseudo_id": usuario,
        "ingresos_usd": ingresos,
        "dispositivo": "desktop",
        "pais": "United States",
        "region": "California",
        "fuente": "google",
        "medio": "organic",
    }


def _embudo(paso=1, sesiones=10):
    return {"fecha": "2020-11-02", "dispositivo": "desktop", "pais": "United States",
            "fuente": "google", "medio": "organic", "paso": paso, "sesiones": sesiones}


def _datos(**cambios):
    base = {
        "resumen": [_resumen()],
        "usuarios": [{"fecha": "2020-11-02", "dispositivo": "desktop", "pais": "United States",
                      "region": "California", "fuente": "google", "medio": "organic",
                      "user_pseudo_id": "u1", "compro": 1}],
        "compras": [_compra(), _compra(), _compra(), _compra()],
        "embudo": [_embudo(1, 10), _embudo(2, 6), _embudo(3, 3), _embudo(4, 2)],
        "cohortes": [{"cohorte": "2020-11-02", "indice": 0, "dimension": "canal",
                      "valor": "google / organic", "usuarios": 10, "compradores": 2}],
        "categorias": [],
        "eventos": [],
    }
    base.update(cambios)
    return base


def _capacidades(**cambios):
    base = {"adquisicion": True, "conversion": True, "cohortes": True, "ingresos": True,
            "categoria_producto": False}
    base.update(cambios)
    return base


def test_validacion_rechaza_embudo_creciente():
    datos = _datos(embudo=[_embudo(1, 5), _embudo(2, 9)])
    revision = ingesta.validar(datos, _capacidades())
    assert revision["valida"] is False
    assert any("embudo crece" in error for error in revision["errores"])


def test_validacion_rechaza_mas_compradores_que_usuarios():
    datos = _datos(
        usuarios=[{"fecha": "2020-11-02", "dispositivo": "desktop", "pais": "United States",
                   "region": "California", "fuente": "google", "medio": "organic",
                   "user_pseudo_id": "u9", "compro": 0}],
        compras=[_compra("u1"), _compra("u2")],
    )
    revision = ingesta.validar(datos, _capacidades())
    assert revision["valida"] is False
    assert any("compradores" in error for error in revision["errores"])


def test_validacion_avisa_de_compras_sin_importe():
    datos = _datos(compras=[_compra(ingresos=None)])
    revision = ingesta.validar(datos, _capacidades())
    assert revision["valida"] is True
    assert revision["cifras"]["compras_sin_importe"] == 1
    assert any("no traen importe" in aviso for aviso in revision["avisos"])


def test_validacion_rechaza_indice_de_cohorte_negativo():
    datos = _datos(cohortes=[{"cohorte": "2020-11-02", "indice": -1, "dimension": "canal",
                              "valor": "x", "usuarios": 1, "compradores": 1}])
    revision = ingesta.validar(datos, _capacidades())
    assert revision["valida"] is False


def test_validacion_acepta_datos_coherentes():
    revision = ingesta.validar(_datos(), _capacidades())
    assert revision["valida"] is True
    assert revision["cifras"]["eventos"] == 100
    assert revision["cifras"]["compras"] == 4
    assert revision["periodo"] == {"desde": "2020-11-02", "hasta": "2020-11-02"}


def test_usuarios_se_guardan_hasheados():
    documentos = ingesta.construir_documentos(_datos(), "ga4-prueba")
    usuarios = documentos["marketing_usuarios"]
    assert usuarios and all(len(fila["usuario"]) == 32 for fila in usuarios)
    assert all("u1" not in fila["usuario"] for fila in usuarios)
    assert usuarios[0]["compro"] == 1


# --------------------------------------------------- fuente de prueba y fixture

VERSION_DATOS = [
    # Día 1: dos canales y dos dispositivos.
    {
        "fecha": "2020-11-02", "dispositivo": "desktop", "pais": "United States",
        "region": "California", "fuente": "google", "medio": "organic",
        "eventos": 120, "usuarios": 50, "sesiones": 60, "eventos_sin_sesion": 2,
        "compras": 5, "compradores": 4, "ingresos_usd": 500.0, "compras_sin_importe": 0,
    },
    {
        "fecha": "2020-11-02", "dispositivo": "mobile", "pais": "United States",
        "region": "California", "fuente": "direct", "medio": "(none)",
        "eventos": 60, "usuarios": 30, "sesiones": 32, "eventos_sin_sesion": 1,
        "compras": 1, "compradores": 1, "ingresos_usd": 80.0, "compras_sin_importe": 0,
    },
    # Día 2 del mismo canal: los usuarios se repiten (no deben sumarse).
    {
        "fecha": "2020-11-09", "dispositivo": "desktop", "pais": "United States",
        "region": "California", "fuente": "google", "medio": "organic",
        "eventos": 90, "usuarios": 40, "sesiones": 45, "eventos_sin_sesion": 0,
        "compras": 2, "compradores": 2, "ingresos_usd": 200.0, "compras_sin_importe": 0,
    },
]

USUARIOS_DATOS = [
    # u1 y u2 son las mismas personas en los dos días de google/organic.
    {"fecha": "2020-11-02", "dispositivo": "desktop", "pais": "United States",
     "region": "California", "fuente": "google", "medio": "organic",
     "user_pseudo_id": "u1", "compro": 1},
    {"fecha": "2020-11-02", "dispositivo": "desktop", "pais": "United States",
     "region": "California", "fuente": "google", "medio": "organic",
     "user_pseudo_id": "u2", "compro": 0},
    {"fecha": "2020-11-02", "dispositivo": "mobile", "pais": "United States",
     "region": "California", "fuente": "direct", "medio": "(none)",
     "user_pseudo_id": "u3", "compro": 1},
    {"fecha": "2020-11-09", "dispositivo": "desktop", "pais": "United States",
     "region": "California", "fuente": "google", "medio": "organic",
     "user_pseudo_id": "u1", "compro": 1},
    {"fecha": "2020-11-09", "dispositivo": "desktop", "pais": "United States",
     "region": "California", "fuente": "google", "medio": "organic",
     "user_pseudo_id": "u4", "compro": 0},
]

def _sesion(indice: int, pasos: int, usuario: str = "u1") -> dict:
    """Sesión del fixture: ``pasos`` indica cuántos pasos del embudo alcanzó."""
    base = 1604203200 + indice * 3_600  # 2020-11-01T04:00:00Z + horas
    marcas = [None, None, None, None]
    for paso in range(pasos):
        marcas[paso] = base + paso * 120
    return {
        "clave_sesion": f"{usuario}|{indice}",
        "fecha": "2020-11-02",
        "user_pseudo_id": usuario,
        "dispositivo": "desktop",
        "pais": "United States",
        "region": "California",
        "fuente": "google",
        "medio": "organic",
        "inicio": base,
        "fin": (marcas[pasos - 1] if pasos else base) + 60,
        "eventos": 10 + indice,
        "t1": marcas[0],
        "t2": marcas[1],
        "t3": marcas[2],
        "t4": marcas[3],
        "e1": 1 if marcas[0] else 0,
        "e2": 1 if marcas[1] else 0,
        "e3": 1 if marcas[2] else 0,
        "e4": 1 if marcas[3] else 0,
        "ingresos_usd": 100.0 if marcas[3] else None,
    }


#: 137 sesiones con el primer paso, 62 con el segundo, 31 con el tercero y 8 con
#: la compra: la misma escalera que el resto de fixtures.
SESIONES_DATOS = [
    _sesion(indice, 4 if indice < 8 else 3 if indice < 31 else 2 if indice < 62 else 1)
    for indice in range(137)
]

#: Compras con dos categorías: cada compra aporta exactamente un par.
ARTICULOS_DATOS = [
    fila
    for indice in range(1, 8)
    for fila in (
        {"clave_compra": f"u1|c{indice}", "fecha": "2020-11-02", "user_pseudo_id": "u1",
         "session_id": indice, "dispositivo": "desktop", "pais": "United States",
         "fuente": "google", "medio": "organic", "articulo": "Google Sticker",
         "categoria": "Accessories", "unidades": 1, "precio_unitario_usd": 100.0,
         "ingresos_linea_usd": 100.0},
        {"clave_compra": f"u1|c{indice}", "fecha": "2020-11-02", "user_pseudo_id": "u1",
         "session_id": indice, "dispositivo": "desktop", "pais": "United States",
         "fuente": "google", "medio": "organic", "articulo": "Google Tote",
         "categoria": "Bags", "unidades": 1, "precio_unitario_usd": 50.0,
         "ingresos_linea_usd": 50.0},
    )
]

COMPRAS_DATOS = [
    # Una fila por compra deduplicada (usuario + transacción). u1 compra en los dos
    # días: son compras distintas, pero un solo comprador.
    {"fecha": "2020-11-02", "user_pseudo_id": "u1", "ingresos_usd": 100.0,
     "dispositivo": "desktop", "pais": "United States", "region": "California",
     "fuente": "google", "medio": "organic"},
    {"fecha": "2020-11-02", "user_pseudo_id": "u1", "ingresos_usd": 100.0,
     "dispositivo": "desktop", "pais": "United States", "region": "California",
     "fuente": "google", "medio": "organic"},
    {"fecha": "2020-11-02", "user_pseudo_id": "u1", "ingresos_usd": 100.0,
     "dispositivo": "desktop", "pais": "United States", "region": "California",
     "fuente": "google", "medio": "organic"},
    {"fecha": "2020-11-02", "user_pseudo_id": "u1", "ingresos_usd": 100.0,
     "dispositivo": "desktop", "pais": "United States", "region": "California",
     "fuente": "google", "medio": "organic"},
    {"fecha": "2020-11-02", "user_pseudo_id": "u1", "ingresos_usd": 100.0,
     "dispositivo": "desktop", "pais": "United States", "region": "California",
     "fuente": "google", "medio": "organic"},
    {"fecha": "2020-11-02", "user_pseudo_id": "u3", "ingresos_usd": 80.0,
     "dispositivo": "mobile", "pais": "United States", "region": "California",
     "fuente": "direct", "medio": "(none)"},
    {"fecha": "2020-11-09", "user_pseudo_id": "u1", "ingresos_usd": 100.0,
     "dispositivo": "desktop", "pais": "United States", "region": "California",
     "fuente": "google", "medio": "organic"},
    {"fecha": "2020-11-09", "user_pseudo_id": "u1", "ingresos_usd": 100.0,
     "dispositivo": "desktop", "pais": "United States", "region": "California",
     "fuente": "google", "medio": "organic"},
]

EMBUDO_DATOS = [
    {"fecha": fecha, "dispositivo": dispositivo, "pais": "United States",
     "fuente": fuente, "medio": medio, "paso": paso, "sesiones": sesiones}
    for fecha, dispositivo, fuente, medio, sesiones in [
        ("2020-11-02", "desktop", "google", "organic", [60, 30, 15, 5]),
        ("2020-11-02", "mobile", "direct", "(none)", [32, 12, 6, 1]),
        ("2020-11-09", "desktop", "google", "organic", [45, 20, 10, 2]),
    ]
    for paso, sesiones in zip([1, 2, 3, 4], sesiones)
]

COHORTES_DATOS = [
    {"cohorte": "2020-11-02", "indice": 0, "dimension": "canal", "valor": "google / organic",
     "usuarios": 2, "compradores": 1},
    {"cohorte": "2020-11-02", "indice": 1, "dimension": "canal", "valor": "google / organic",
     "usuarios": 1, "compradores": 1},
    {"cohorte": "2020-11-09", "indice": 0, "dimension": "canal", "valor": "google / organic",
     "usuarios": 2, "compradores": 1},
    # La semana siguiente a la última cohorte queda fuera de la ventana observada.
    {"cohorte": "2020-11-09", "indice": 3, "dimension": "canal", "valor": "google / organic",
     "usuarios": 0, "compradores": 0},
    {"cohorte": "2020-11-02", "indice": 0, "dimension": "canal", "valor": "direct / Sin información",
     "usuarios": 1, "compradores": 1},
]

EVENTOS_DATOS = [
    {"fecha": "2020-11-02", "hora": 10, "event_name": "view_item", "dispositivo": "desktop",
     "pais": "United States", "fuente": "google", "medio": "organic",
     "articulo": "Google Sticker", "categoria": "Accessories", "ingresos_usd": 0.0},
    {"fecha": "2020-11-02", "hora": 10, "event_name": "purchase", "dispositivo": "desktop",
     "pais": "United States", "fuente": "google", "medio": "organic",
     "articulo": "Google Sticker", "categoria": "Accessories", "ingresos_usd": 120.0},
    {"fecha": "2020-11-09", "hora": 15, "event_name": "add_to_cart", "dispositivo": "mobile",
     "pais": "United States", "fuente": "direct", "medio": "(none)",
     "articulo": "Google Tote", "categoria": "Bags", "ingresos_usd": 0.0},
]


class FuentePrueba:
    """Fuente que devuelve filas del fixture según la consulta pedida."""

    nombre = "fixture-de-prueba"

    def __init__(self, fallar: bool = False) -> None:
        self.fallar = fallar

    def ejecutar(self, sql: str, max_filas: int = 200_000) -> list[dict]:
        if self.fallar:
            raise ingesta.acceso.SinAccesoBigQuery("Acceso denegado en la prueba.")
        if "INFORMATION_SCHEMA" in sql:
            return [
                {"column_name": columna, "data_type": tipo}
                for columna, tipo in {
                    **esquema.REQUERIDAS_BASE,
                    **esquema.REQUERIDAS_COMPRA,
                }.items()
            ]
        if "SELECT 1" in sql:
            return [{"ok": 1}]
        if "ROW_NUMBER()" in sql:
            return EVENTOS_DATOS
        if "AS paso" in sql:
            return EMBUDO_DATOS
        if "dispositivos_distintos" in sql:
            return SESIONES_DATOS
        if "UNNEST(c.compra.items)" in sql:
            return ARTICULOS_DATOS
        if "GROUP BY clave_compra" in sql:
            return COMPRAS_DATOS
        if "MAX(IF(event_name = 'purchase', 1, 0)) AS compro" in sql:
            return USUARIOS_DATOS
        if "'canal' AS dimension" in sql:
            return COHORTES_DATOS
        if "item.item_category" in sql:
            return []
        return VERSION_DATOS


@pytest.fixture()
def marketing_db(mongo_client, settings):
    from app.db.mongo import (
        MARKETING_BLOQUEOS,
        MARKETING_CATEGORIAS,
        MARKETING_COHORTES,
        MARKETING_COMPRAS,
        MARKETING_EMBUDO,
        MARKETING_ESTADO,
        MARKETING_EVENTOS,
        MARKETING_IMPORTACIONES,
        MARKETING_RESUMEN,
        MARKETING_USUARIOS,
    )

    db = mongo_client[settings.mongodb_db]
    colecciones = [
        MARKETING_RESUMEN, MARKETING_USUARIOS, MARKETING_COMPRAS, MARKETING_EMBUDO,
        MARKETING_COHORTES, MARKETING_CATEGORIAS, MARKETING_EVENTOS,
        MARKETING_IMPORTACIONES, MARKETING_ESTADO, MARKETING_BLOQUEOS,
    ]
    for nombre in colecciones:
        db[nombre].delete_many({})
    yield db
    for nombre in colecciones:
        db[nombre].delete_many({})


@pytest.fixture()
def marketing_publicado(marketing_db):
    resultado = ingesta.sincronizar(marketing_db, fuente=FuentePrueba())
    assert resultado.exito is True, resultado.motivo
    return marketing_db


@pytest.fixture()
def marketing_client(marketing_publicado, settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as cliente:
        yield cliente


# ------------------------------------------------------------------ ingesta


def test_sincronizacion_publica_version(marketing_db):
    resultado = ingesta.sincronizar(marketing_db, fuente=FuentePrueba())
    assert resultado.exito is True
    assert resultado.version and resultado.version.startswith("ga4-")
    estado = ingesta.estado_publico(marketing_db)
    assert estado["datos_disponibles"] is True
    assert estado["periodo"] == {"desde": "2020-11-02", "hasta": "2020-11-09"}
    assert estado["cifras"]["eventos"] == 270
    assert estado["cifras"]["compras"] == 8


def test_segunda_sincronizacion_no_republica(marketing_db):
    primera = ingesta.sincronizar(marketing_db, fuente=FuentePrueba())
    segunda = ingesta.sincronizar(marketing_db, fuente=FuentePrueba())
    assert segunda.exito is True
    assert segunda.sin_cambios is True
    assert segunda.version == primera.version


def test_fallo_de_acceso_conserva_la_version(marketing_publicado):
    resultado = ingesta.sincronizar(marketing_publicado, fuente=FuentePrueba(fallar=True))
    assert resultado.exito is False
    estado = ingesta.estado_publico(marketing_publicado)
    assert estado["datos_disponibles"] is True, "la versión anterior sigue publicada"
    rechazada = marketing_publicado["marketing_importaciones"].find_one({"estado": "rechazada"})
    assert rechazada is not None


def test_sin_datos_la_api_lo_declara(marketing_db, settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as cliente:
        respuesta = cliente.get("/api/v1/marketing/resumen")
        assert respuesta.status_code == 200
        datos = respuesta.json()
        assert datos["datos_disponibles"] is False
        assert datos["indicadores"] is None
        assert "motivo" in datos and datos["motivo"]


# ----------------------------------------------------------------------- API


def test_resumen_concilia_usuarios_distintos(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/resumen").json()
    assert datos["datos_disponibles"] is True
    indicadores = datos["indicadores"]
    assert indicadores["eventos"]["valor"] == 270
    assert indicadores["sesiones"]["valor"] == 137
    assert indicadores["compras"]["valor"] == 8
    assert indicadores["ingresos_usd"]["valor"] == 780.0
    # u1, u2, u3 y u4: u1 aparece dos días y se cuenta una sola vez.
    assert indicadores["usuarios"]["valor"] == 4
    assert indicadores["compradores"]["valor"] == 2
    assert indicadores["conversion"]["valor"] == 50.0
    assert indicadores["ticket_medio_usd"]["valor"] == 97.5


def test_serie_diaria_no_suma_usuarios_de_dias_distintos(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/resumen").json()
    serie = datos["serie"]
    assert [fila["fecha"] for fila in serie] == ["2020-11-02", "2020-11-09"]
    assert serie[0]["usuarios"] == 3
    assert serie[1]["usuarios"] == 2
    assert sum(fila["usuarios"] for fila in serie) > datos["indicadores"]["usuarios"]["valor"]


def test_conversion_por_fuente_concilia_con_usuarios_unicos(marketing_client):
    """Cada usuario pertenece a un solo canal: la suma debe ser el total exacto."""
    datos = marketing_client.get("/api/v1/marketing/resumen").json()
    filas = datos["conversion_fuente"]["filas"]
    assert filas and all("base_reducida" in fila for fila in filas)
    suma = sum(fila["usuarios"] for fila in filas)
    assert suma == datos["indicadores"]["usuarios"]["valor"]
    global_ = datos["conversion_fuente"]["referencia_global"]
    assert global_["denominador"].startswith("usuarios únicos")
    assert global_["usuarios"] == suma
    # El resumen ya no calcula la comparación con el periodo anterior.
    assert datos["comparacion"] is None


def test_adquisicion_declara_que_es_adquisicion_de_usuario(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/adquisicion").json()
    assert datos["semantica"]["tipo"] == "adquisicion_de_usuario"
    canales = {fila["canal"]: fila for fila in datos["canales"]}
    assert set(canales) == {"google / organic", "direct / Sin información"}
    assert canales["google / organic"]["usuarios"] == 3
    assert canales["google / organic"]["conversion"] == round(100 * 1 / 3, 2)
    assert datos["categorias_disponibles"] is False


def test_conversion_es_un_embudo_que_decrece(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/conversion").json()
    assert datos["unidad"] == "sesion"
    assert datos["ventana_horas"] == 24
    sesiones = [paso["sesiones"] for paso in datos["pasos"]]
    assert sesiones == [137, 62, 31, 8]
    assert sesiones == sorted(sesiones, reverse=True)
    assert datos["pasos"][0]["porcentaje_del_inicio"] == 100.0
    assert datos["pasos"][1]["porcentaje_del_paso_anterior"] == round(100 * 62 / 137, 2)


def test_cohortes_marcan_ventanas_incompletas(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/cohortes?dimension=canal").json()
    assert datos["unidad"] == "usuario"
    celdas = {(fila["cohorte"], fila["indice"]): fila for fila in datos["celdas"]}
    primera = celdas[("2020-11-02", 0)]
    assert primera["ventana"] == "completa"
    # La cohorte del 2 de noviembre reúne 3 usuarios (2 orgánicos + 1 directo).
    assert primera["usuarios"] == 3
    assert primera["retencion"] == 100.0
    # La semana siguiente termina después del último día observado: no hay ventana
    # suficiente, así que la celda se devuelve vacía y nunca como retención cero.
    semana_1 = celdas[("2020-11-02", 1)]
    assert semana_1["ventana"] == "incompleta"
    assert semana_1["retencion"] is None
    assert semana_1["usuarios"] == 1
    # Índice 3 de la cohorte del 9 de noviembre queda sin ventana observada.
    fuera = celdas[("2020-11-09", 3)]
    assert fuera["ventana"] == "sin_ventana"
    assert fuera["retencion"] is None, "no se convierte en retención cero"


def test_filtros_or_dentro_y_and_entre_dimensiones(marketing_client):
    uno = marketing_client.get(
        "/api/v1/marketing/resumen?canal=google / organic"
    ).json()
    ambos = marketing_client.get(
        "/api/v1/marketing/resumen?canal=google / organic&canal=direct / Sin información"
    ).json()
    assert ambos["indicadores"]["eventos"]["valor"] == 270
    assert uno["indicadores"]["eventos"]["valor"] == 210

    cruzado = marketing_client.get(
        "/api/v1/marketing/resumen?canal=google / organic&dispositivo=mobile"
    ).json()
    assert cruzado["indicadores"]["eventos"]["valor"] == 0
    assert cruzado["indicadores"]["usuarios"]["valor"] == 0


def test_tabla_declara_que_el_detalle_no_se_almacena(marketing_client):
    """El modelo compacto no guarda eventos: la API lo dice en lugar de inventar filas."""
    datos = marketing_client.get("/api/v1/marketing/tabla?tamano=2").json()
    assert datos["datos_disponibles"] is False
    assert datos["disponible"] is False
    assert "no se almacena" in datos["motivo"]
    assert datos["filas"] == []


def test_exportacion_documenta_fuente_muestra_y_filtros(marketing_client):
    """La exportación entrega los agregados del rango, no un extracto de eventos."""
    respuesta = marketing_client.get("/api/v1/marketing/exportar?canal=google / organic")
    assert respuesta.status_code == 200
    texto = respuesta.content.decode("utf-8-sig")
    lineas = [linea for linea in texto.splitlines() if linea and not linea.startswith("#")]
    filas = list(csv.DictReader(io.StringIO("\n".join(lineas))))
    assert filas, "la exportación tiene que traer filas"
    assert {"bloque", "clave", "usuarios", "compras"} <= set(filas[0])
    bloques = {fila["bloque"] for fila in filas}
    assert {"serie", "canal", "total"} <= bloques
    assert "Fuente:" in texto
    assert "Condición:" in texto
    assert "Definiciones:" in texto
    # Declara la muestra y el método, y que no hay detalle de eventos.
    assert "Muestra publicada:" in texto
    assert "Método de selección:" in texto
    assert "No incluye eventos uno a uno" in texto
    # Las columnas no llevan identificadores de personas.
    assert "user_pseudo_id" not in lineas[0]
    assert not any(len(campo) == 32 and all(c in "0123456789abcdef" for c in campo)
                   for fila in filas for campo in fila.values())


def test_dimensiones_de_cohorte(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/dimensiones").json()
    assert {fila["valor"] for fila in datos["dimensiones"]} == {"canal", "dispositivo", "pais"}


def test_dimension_de_cohorte_invalida(marketing_client):
    respuesta = marketing_client.get("/api/v1/marketing/cohortes?dimension=ciudad")
    assert respuesta.status_code == 422


def test_filtros_normalizan_listas():
    assert normalizar_lista(["b", "a", "b ", " a "]) == ["a", "b"]
    assert normalizar_lista(None) == []


def test_filtro_de_periodo_invalido(marketing_client):
    respuesta = marketing_client.get(
        "/api/v1/marketing/resumen?desde=2020-11-09&hasta=2020-11-02"
    )
    assert respuesta.status_code == 422


def test_condiciones_solo_usan_los_campos_documentados():
    filtros = MarketingFiltros(canales=["google / organic"], dispositivos=["desktop"])
    condiciones = filtros.condiciones()
    assert condiciones["canal"] == {"$in": ["google / organic"]}
    assert condiciones["dispositivo"] == {"$in": ["desktop"]}
    assert "ubicacion" not in condiciones

# ---------------------------------------- nuevas agregaciones (sankey, afinidad)


def test_sankey_concilia_columnas(marketing_client):
    """Cada sesión aporta una vez por columna: las tres columnas suman lo mismo."""
    datos = marketing_client.get("/api/v1/marketing/resumen").json()
    sankey = datos["sankey"]
    total = sankey["total_sesiones"]
    assert total > 0
    assert sankey["sesiones_con_compra"] + sankey["sesiones_sin_compra"] == total
    assert sankey["conciliacion"]["salida_fuentes"] == total
    assert sankey["conciliacion"]["salida_dispositivos"] == total
    assert sankey["conciliacion"]["cuadra"] is True
    # Los enlaces de cada columna suman exactamente el total.
    for columna in (0, 1):
        suma = sum(enlace["sesiones"] for enlace in sankey["enlaces"] if enlace["columna"] == columna)
        assert suma == total


def test_sankey_declara_la_regla_del_dispositivo(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/resumen").json()
    sankey = datos["sankey"]
    assert "primera actividad" in sankey["conciliacion"]["regla_dispositivo"]
    assert sankey["conciliacion"]["unidad"] == "sesión"
    # Tres columnas: fuente, dispositivo y resultado.
    assert {nodo["columna"] for nodo in sankey["nodos"]} == {0, 1, 2}


def test_lollipop_usa_conversion_de_usuario(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/resumen").json()
    fuente = datos["conversion_fuente"]
    fila = fuente["filas"][0]
    assert fila["conversion"] == round(100 * fila["compradores"] / fila["usuarios"], 2)
    # La referencia global se calcula con sus totales, no promediando tasas.
    global_ = fuente["referencia_global"]
    assert global_["valor"] == round(100 * global_["compradores"] / global_["usuarios"], 2)
    assert fuente["umbral_base_reducida"] > 0


def test_afinidad_pares_sin_autorelaciones_ni_duplicados(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/productos").json()
    afinidad = datos["afinidad"]
    pares = afinidad["pares"]
    assert pares, "el fixture tiene compras con dos categorías"
    claves = [frozenset((par["a"], par["b"])) for par in pares]
    assert len(claves) == len(set(claves)), "cada par aparece una sola vez"
    assert all(par["a"] != par["b"] for par in pares), "sin autorrelaciones"
    for par in pares:
        assert par["compras_conjuntas"] <= min(par["compras_a"], par["compras_b"])
        if par["compras_a"]:
            assert par["pct_de_a"] == round(100 * par["compras_conjuntas"] / par["compras_a"], 1)
        if par["compras_b"]:
            assert par["pct_de_b"] == round(100 * par["compras_conjuntas"] / par["compras_b"], 1)


def test_perfil_por_canal_es_monotono_y_usa_su_denominador(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/conversion").json()
    perfil = datos["perfil"]
    assert perfil["canales"]
    for canal in perfil["canales"]:
        assert canal["n1"] >= canal["n2"] >= canal["n3"] >= canal["n4"]
        assert canal["monotono"] is True
        tasas = canal["tasas"]
        if canal["n1"]:
            assert tasas["n2_n1"] == round(100 * canal["n2"] / canal["n1"], 2)
        if canal["n2"]:
            assert tasas["n3_n2"] == round(100 * canal["n3"] / canal["n2"], 2)
        if canal["n3"]:
            assert tasas["n4_n3"] == round(100 * canal["n4"] / canal["n3"], 2)
        if canal["n1"]:
            assert tasas["n4_n1"] == round(100 * canal["n4"] / canal["n1"], 2)


def test_perfil_ausencia_de_tasa_cuando_el_denominador_es_cero(marketing_client):
    """Un denominador cero se devuelve como ausencia (null), nunca como 0 %."""
    datos = marketing_client.get(
        "/api/v1/marketing/conversion?canal=google / organic"
    ).json()
    perfil = datos["perfil"]
    for canal in perfil["canales"]:
        if canal["n1"] == 0:
            assert canal["tasas"]["n2_n1"] is None

# ------------------------------------------------- cohortes: base por segmento


def test_curvas_por_segmento_tienen_s0_al_cien_y_base_propia():
    """S0 debe ser 100 % de la base del segmento, no su cuota dentro de la cohorte."""
    from app.services.marketing_service import _curvas_y_puntos

    filas = [
        # Cohorte: Estados Unidos 10 usuarios e India 5 en la semana 0.
        {"cohorte": "2020-11-02", "indice": 0, "valor": "United States", "usuarios": 10, "compradores": 1},
        {"cohorte": "2020-11-02", "indice": 0, "valor": "India", "usuarios": 5, "compradores": 0},
        # Semana 1: vuelven 2 de Estados Unidos y 1 de India.
        {"cohorte": "2020-11-02", "indice": 1, "valor": "United States", "usuarios": 2, "compradores": 0},
        {"cohorte": "2020-11-02", "indice": 1, "valor": "India", "usuarios": 1, "compradores": 0},
    ]
    curvas, _ = _curvas_y_puntos(filas, date(2021, 1, 31))
    por_valor = {curva["valor"]: curva for curva in curvas}

    for valor, base in (("United States", 10), ("India", 5)):
        curva = por_valor[valor]
        inicio = curva["puntos"][0]
        assert inicio["indice"] == 0
        assert inicio["retencion"] == 100.0, "S0 debe ser 100 % de su propia base"
        assert inicio["base"] == base
        assert curva["base_s0"] == base

    # El denominador de S1 es el del segmento, no el de la cohorte completa (15).
    semana_1 = [punto for punto in por_valor["United States"]["puntos"] if punto["indice"] == 1][0]
    assert semana_1["base"] == 10
    assert semana_1["usuarios"] == 2
    assert semana_1["retencion"] == 20.0
    india_1 = [punto for punto in por_valor["India"]["puntos"] if punto["indice"] == 1][0]
    assert india_1["base"] == 5 and india_1["retencion"] == 20.0


def test_curvas_no_inventan_semanas_sin_ventana():
    """Una cohorte reciente no puede puntuar la semana que aún no se observó."""
    from app.services.marketing_service import _curvas_y_puntos

    filas = [
        {"cohorte": "2021-01-25", "indice": 0, "valor": "Spain", "usuarios": 4, "compradores": 0},
        {"cohorte": "2021-01-25", "indice": 2, "valor": "Spain", "usuarios": 0, "compradores": 0},
    ]
    curvas, _ = _curvas_y_puntos(filas, date(2021, 1, 31))
    curva = curvas[0]
    semana_2 = [punto for punto in curva["puntos"] if punto["indice"] == 2][0]
    # Sin ventana completa, la base elegible es 0 y no hay tasa (no es un cero).
    assert semana_2["base"] == 0
    assert semana_2["retencion"] is None

# ------------------------------------------------- mapa de fricción del embudo


def test_friccion_referencia_se_calcula_con_totales(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/conversion").json()
    friccion = datos["friccion"]
    for referencia in friccion["referencia"].values():
        if referencia["denominador"]:
            assert abs(referencia["tasa"] - referencia["numerador"] / referencia["denominador"]) < 1e-9
        else:
            assert referencia["tasa"] is None
        assert referencia["sin_avance"] == referencia["denominador"] - referencia["numerador"]


def test_friccion_brechas_signo_y_rangos(marketing_client):
    datos = marketing_client.get("/api/v1/marketing/conversion").json()
    friccion = datos["friccion"]
    for canal in friccion["canales"]:
        assert canal["n"]["1"] >= canal["n"]["2"] >= canal["n"]["3"] >= canal["n"]["4"]
        for transicion in canal["transiciones"]:
            if transicion["tasa"] is not None:
                assert 0 <= transicion["tasa"] <= 1
                referencia = friccion["referencia"][transicion["clave"]]["tasa"]
                esperado = round(100 * (transicion["tasa"] - referencia), 1)
                assert transicion["brecha_pp"] == esperado
            else:
                assert transicion["brecha_pp"] is None
            assert transicion["sin_avance"] == transicion["denominador"] - transicion["numerador"]


def test_friccion_universo_concuerda_con_el_embudo(marketing_client):
    conversion = marketing_client.get("/api/v1/marketing/conversion").json()
    friccion = conversion["friccion"]
    total_entrada = sum(canal["n"]["1"] for canal in friccion["canales"])
    assert friccion["referencia"]["vista_carrito"]["denominador"] == total_entrada
    # El N1 del universo es el de la primera etapa del embudo principal.
    assert total_entrada == conversion["pasos"][0]["sesiones"]

def test_cobertura_declara_poblacion_muestra_y_metodo(marketing_client):
    """La cobertura dice de dónde sale la muestra y que no se extrapola."""
    estado = marketing_client.get("/api/v1/marketing/estado").json()
    cobertura = estado["cobertura_muestra"]
    poblacion = cobertura["poblacion_original"]
    muestra = cobertura["muestra_publicada"]
    assert poblacion["usuarios"] == 270154
    assert poblacion["filas_usuario_dia"] == 319066
    assert muestra["usuarios"] and muestra["usuarios"] < poblacion["usuarios"]
    # La proporción se calcula con las dos cifras, no se copia del manifiesto.
    assert muestra["porcentaje_efectivo"] == round(
        100 * muestra["usuarios"] / poblacion["usuarios"], 3
    )
    assert "FARM_FINGERPRINT" in cobertura["metodo"]["predicado"]
    assert muestra["semilla"] in cobertura["metodo"]["predicado"]
    assert cobertura["alcance"]["no_extrapolable"] is True
    assert "sin muestreo" in poblacion["nota"]


def test_resumen_concilia_indicadores_y_graficas(marketing_client):
    """Cada indicador se compara con el total que suman las gráficas del rango."""
    datos = marketing_client.get("/api/v1/marketing/resumen").json()
    consistencia = datos["cobertura"]["consistencia"]
    assert {fila["clave"] for fila in consistencia} == {
        "eventos",
        "sesiones",
        "usuarios",
        "compradores",
        "compras",
    }
    for fila in consistencia:
        assert isinstance(fila["cuadra"], bool)
        assert fila["grafica_total"] - fila["indicador"] == fila["diferencia"]
    # Los totales de las tarjetas son los mismos que los de las gráficas.
    por_clave = {fila["clave"]: fila for fila in consistencia}
    assert por_clave["sesiones"]["grafica_total"] == datos["sankey"]["total_sesiones"]
    assert por_clave["usuarios"]["grafica_total"] == sum(
        canal["usuarios"] for canal in datos["conversion_fuente"]["filas"]
    )


def test_afinidad_declara_su_alcance_y_su_periodo(marketing_client):
    """La afinidad dice si responde a los filtros y qué periodo cubre."""
    datos = marketing_client.get("/api/v1/marketing/productos").json()
    afinidad = datos["afinidad"]
    alcance = afinidad["alcance"]
    assert alcance["aplica_filtros"] is alcance["responde_a_filtros"]
    assert alcance["periodo"]["desde"] and alcance["periodo"]["hasta"]
    assert "compras_analizadas" in afinidad["cobertura"]
    if not alcance["aplica_filtros"]:
        # Modelo compacto: los pares se publican calculados una vez.
        filtrado = marketing_client.get(
            "/api/v1/marketing/productos?desde=2020-12-01&hasta=2021-01-31"
        ).json()
        assert filtrado["afinidad"]["pares"] == afinidad["pares"]
    else:
        # Versión con detalle: se recalcula y así se declara.
        assert "rango filtrado" in alcance["descripcion"]
