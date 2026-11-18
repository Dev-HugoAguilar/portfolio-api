"""Pruebas del Atlas vial (SSC · Ciudad de México).

Tres capas, todas sin red:

1. **Normalización y lectura de archivos**: mojibake, encabezados duplicados,
   fechas, horas, coordenadas y edades tal como vienen en los CSV oficiales.
2. **Semántica de los filtros**: OR dentro de una dimensión, AND entre
   dimensiones, recorte por zona solo si se pide y búsqueda sin expresiones
   regulares.
3. **API y agregaciones** con una versión mínima sembrada en la base de pruebas:
   los totales reparten los hechos sin duplicarlos y la tabla, la exportación y
   el detalle coinciden con el resumen.

Las pruebas de integración usan ``hugo_portfolio_test`` y solo vacían las
colecciones del Atlas; nunca borran bases ni colecciones de otros proyectos.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from app.atlas import parser as atlas_parser
from app.atlas import normalizacion
from app.main import create_app
from app.models.atlas import AtlasFiltros, normalizar_lista

# --------------------------------------------------------------- normalización


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        # Los archivos traen «VehÃ\u00adculo»: la «í» se decodificó como latin-1.
        ("VehÃ\u00adculo 1", "Vehículo 1"),
        ("BOTÃ\u201cN DE AUXILIO", "BOTÓN DE AUXILIO"),
        ("Vía primaria", "Vía primaria"),
        ("", ""),
        (None, None),
    ],
)
def test_reparar_mojibake(crudo, esperado):
    assert normalizacion.reparar_mojibake(crudo) == esperado


def test_leer_fecha_admite_dia_mes_anio():
    assert normalizacion.leer_fecha("01/01/2024").isoformat() == "2024-01-01"
    assert normalizacion.leer_fecha("31/12/2024").isoformat() == "2024-12-31"
    assert normalizacion.leer_fecha("2024-01-01").isoformat() == "2024-01-01"
    assert normalizacion.leer_fecha("32/01/2024") is None
    assert normalizacion.leer_fecha("") is None


def test_leer_hora_exige_formato_con_minutos():
    """La fuente publica la hora como «HH:MM»; el entero es la hora del hecho."""
    assert normalizacion.leer_hora("08:00") == 8
    assert normalizacion.leer_hora("23:59") == 23
    assert normalizacion.leer_hora("24:00") is None
    assert normalizacion.leer_hora("08") is None, "sin minutos no se adivina la hora"
    assert normalizacion.leer_hora("") is None


def test_edad_no_convierte_desconocidos_en_cero():
    """«SD» y «NA» son ausencias: quedan sin dato y se conserva el texto."""
    for crudo in ("SD", "NA", ""):
        valor, texto = normalizacion.edad_valor(crudo)
        assert valor is None, crudo
        assert texto == crudo.strip()
    # Erratas documentadas: «2O» es 20 (letra O), «9M» son meses de vida.
    assert normalizacion.edad_valor("2O")[0] == 20
    assert normalizacion.edad_valor("9M")[0] == 0
    assert normalizacion.edad_valor("34")[0] == 34
    assert normalizacion.edad_valor("0")[0] == 0
    assert normalizacion.edad_valor("130")[0] is None


def test_leer_coordenadas_distingue_los_casos():
    lat, lon, estado = normalizacion.leer_coordenadas("19.4326", "-99.1332")
    assert estado == "ok" and lat == 19.4326 and lon == -99.1332
    assert normalizacion.leer_coordenadas("", "")[2] == "ausente"
    assert normalizacion.leer_coordenadas("SD", "-99.13")[2] == "no_numerica"
    assert normalizacion.leer_coordenadas("91.0", "-99.13")[2] == "fuera_de_rango"
    # Válidas pero fuera de la Ciudad de México (por ejemplo, Toluca).
    estado, sin_uso = normalizacion.leer_coordenadas("19.2926", "-99.6539")[2], None
    assert estado == "fuera_de_cdmx"
    assert sin_uso is None


def test_alcaldia_se_reconoce_sin_acentos_ni_mayusculas():
    assert normalizacion.clave("CUAUHTEMOC") == normalizacion.clave("Cuauhtémoc")
    assert normalizacion.clave("  Álvaro Obregón ") == normalizacion.clave("alvaro obregon")


# ---------------------------------------------------- lectura de archivos CSV


def _csv(texto: str) -> Path:
    ruta = Path(__file__).with_name("_atlas_tmp.csv")
    ruta.write_text(texto, encoding="utf-8-sig")
    return ruta


def test_encabezado_duplicado_no_sobrescribe_columnas():
    """El archivo de tipo de persona repite «condicion_persona».

    La primera columna es la consecuencia (lesionada/fallecida) y la segunda el
    tipo de persona. El lector mapea cada una a un nombre propio y registra el
    duplicado para poder auditarlo; ninguna de las dos se pierde.
    """
    ruta = _csv(
        "folio,condicion_persona,condicion_persona,total\n"
        "C1/20240101/00001,lesionado,Motociclista,1\n"
    )
    tabla = atlas_parser.leer_tabla(ruta, "usuario")
    try:
        assert tabla.duplicados == {"condicion_persona": 2}
        fila = tabla.filas[0]
        assert fila["consecuencia"] == "lesionado"
        assert fila["tipo_usuario"] == "Motociclista"
        assert fila["total"] == "1"
        assert len(tabla.claves) == len(set(tabla.claves)), "las claves no se repiten"
    finally:
        ruta.unlink(missing_ok=True)


def test_separador_y_codificacion_se_detectan():
    ruta = _csv("folio,colonia,total\nC1/20240101/00001,Doctores,2\n")
    tabla = atlas_parser.leer_tabla(ruta, "hechos")
    try:
        assert tabla.separador == ","
        assert tabla.filas[0]["colonia"] == "Doctores"
    finally:
        ruta.unlink(missing_ok=True)


def _tabla(clave: str, filas: list[dict], columnas: list[str]) -> atlas_parser.Tabla:
    return atlas_parser.Tabla(
        ruta=Path(f"{clave}.csv"),
        cabecera=list(columnas),
        claves=list(columnas),
        filas=filas,
        duplicados={},
        separador=",",
    )


def test_construir_hechos_no_multiplica_registros():
    """Un hecho por folio aunque los cinco recursos tengan su propia fila."""
    folio = "C1/20240101/00001"
    tablas = {
        "hechos": _tabla(
            "hechos",
            [
                {
                    "folio": folio,
                    "fecha_evento": "01/01/2024",
                    "hora_evento": "08:00",
                    "alcaldia": "CUAUHTEMOC",
                    "tipo_evento": "CHOQUE",
                    "personas_lesionadas": "2",
                    "personas_fallecidas": "0",
                    "clasificacion_de_la_vialidad": "VIA PRIMARIA",
                    "tipo_de_interseccion": "CRUCE",
                    "interseccion_semaforizada": "SI",
                    "latitud": "19.4326",
                    "longitud": "-99.1332",
                }
            ],
            [
                "folio",
                "fecha_evento",
                "hora_evento",
                "alcaldia",
                "tipo_evento",
                "personas_lesionadas",
                "personas_fallecidas",
                "clasificacion_de_la_vialidad",
                "tipo_de_interseccion",
                "interseccion_semaforizada",
                "latitud",
                "longitud",
            ],
        ),
        "sexo": _tabla(
            "sexo",
            [{"folio": folio, "sexo": "HOMBRE", "condicion_persona": "LESIONADO", "total": "2"}],
            ["folio", "sexo", "condicion_persona", "total"],
        ),
        "edad": _tabla(
            "edad", [{"folio": folio, "edad": "34", "total": "2"}], ["folio", "edad", "total"]
        ),
        "usuario": _tabla(
            "usuario",
            [{"folio": folio, "consecuencia": "LESIONADO", "tipo_usuario": "MOTOCICLISTA", "total": "2"}],
            ["folio", "consecuencia", "tipo_usuario", "total"],
        ),
        "vehiculos": _tabla(
            "vehiculos",
            [{"folio": folio, "tipo_vehiculo": "MOTOCICLETA", "no_vehiculo": "Vehículo 1"}],
            ["folio", "tipo_vehiculo", "no_vehiculo"],
        ),
    }
    hechos, incidencias = atlas_parser.construir_hechos(tablas)
    assert len(hechos) == 1
    hecho = hechos[0]
    assert hecho["folio"] == folio
    assert hecho["personas_lesionadas"] == 2
    assert hecho["personas_fallecidas"] == 0
    assert hecho["tipo_evento_norm"] == "Choque"
    assert hecho["alcaldia"] == "Cuauhtémoc"
    assert hecho["hora"] == 8
    assert hecho["dia_semana"] == 0, "1 de enero de 2024 fue lunes"
    assert hecho["personas_sexo"] == "Hombre"
    assert hecho["personas_edad"] == 34
    assert hecho["personas_tipo_usuario"] == "Motociclista"
    assert hecho["vehiculo_tipo"] == "Motocicleta"
    assert hecho["coord_estado"] == "ok"
    assert hecho["semaforizada"] is True
    assert incidencias["coordenadas"]["ok"] == 1
    assert incidencias["folios_duplicados_hechos"] == 0


def test_un_folio_repetido_en_los_complementarios_no_duplica_la_persona():
    """Si un recurso repitiera el folio, se registra y no se aplica dos veces."""
    folio = "C1/20240101/00001"
    tablas = {
        "hechos": _tabla(
            "hechos",
            [
                {
                    "folio": folio,
                    "fecha_evento": "01/01/2024",
                    "hora_evento": "08:00",
                    "personas_lesionadas": "1",
                    "personas_fallecidas": "0",
                }
            ],
            ["folio", "fecha_evento", "hora_evento", "personas_lesionadas", "personas_fallecidas"],
        ),
        "sexo": _tabla(
            "sexo",
            [
                {"folio": folio, "sexo": "HOMBRE", "total": "1"},
                {"folio": folio, "sexo": "MUJER", "total": "1"},
            ],
            ["folio", "sexo", "total"],
        ),
    }
    hechos, incidencias = atlas_parser.construir_hechos(tablas)
    assert len(hechos) == 1
    assert hechos[0]["personas_lesionadas"] == 1, "las personas nunca se multiplican"
    assert incidencias["folios_con_varias_filas"]["sexo"] == 1


# --------------------------------------------------------------------- filtros


def test_dentro_de_una_dimension_los_valores_se_suman():
    filtros = AtlasFiltros(alcaldias=["Cuauhtémoc", "Iztapalapa"])
    condiciones = filtros.condiciones()
    assert condiciones["alcaldia"] == {"$in": ["Cuauhtémoc", "Iztapalapa"]}


def test_entre_dimensiones_distintas_se_cruzan():
    filtros = AtlasFiltros(alcaldias=["Cuauhtémoc"], tipos_evento=["Choque"])
    condiciones = filtros.condiciones()
    assert condiciones["alcaldia"] == {"$in": ["Cuauhtémoc"]}
    assert condiciones["tipo_evento_norm"] == {"$in": ["Choque"]}


def test_la_zona_solo_se_aplica_si_se_pide():
    filtros = AtlasFiltros(bbox=(-99.2, 19.3, -99.1, 19.4))
    assert "ubicacion" not in filtros.condiciones()
    filtros.filtrar_por_zona = True
    assert "ubicacion" in filtros.condiciones()


def test_la_busqueda_no_acepta_expresiones_regulares():
    filtros = AtlasFiltros(buscar="^.*$")
    condiciones = filtros.condiciones()
    patron = condiciones["$and"][0]["$or"][0]["folio"]["$regex"]
    assert patron == r"\^\.\*\$", "el término debe ir escapado"


def test_filtro_por_semaforo_mapea_los_tres_estados():
    filtros = AtlasFiltros(semaforizada=["si", "sin_dato"])
    assert filtros.condiciones()["semaforizada"] == {"$in": [True, None]}


def test_consecuencia_filtra_por_personas_no_por_tipo():
    con_muertos = AtlasFiltros(consecuencias=["con_fallecimientos"]).condiciones()
    assert {"personas_fallecidas": {"$gt": 0}} in con_muertos["$or"]


def test_estado_activo_se_describe_para_la_url():
    filtros = AtlasFiltros(
        desde=None,
        hasta=None,
        alcaldias=["Cuauhtémoc"],
        tipos_evento=["Choque"],
        buscar="Insurgentes",
    )
    activos = filtros.activos()
    claves = {activo["clave"] for activo in activos}
    assert {"alcaldia", "tipo_evento", "buscar"} <= claves
    assert filtros.consulta()["alcaldia"] == ["Cuauhtémoc"]


def test_normalizar_lista_quita_repetidos_y_ordena():
    assert normalizar_lista(["b", "a", "b", "  a  "]) == ["a", "b"]
    assert normalizar_lista(None) == []


# -------------------------------------------------------- API con datos mínimos

VERSION = "ssc-2024-prueba"


def _hecho(**cambios):
    base = {
        "version": VERSION,
        "folio": "C1/20240101/00001",
        "fecha": "2024-01-01",
        "hora": 8,
        "dia_semana": 0,
        "anio": 2024,
        "mes": 1,
        "dia": 1,
        "alcaldia": "Cuauhtémoc",
        "alcaldia_texto": "CUAUHTEMOC",
        "tipo_evento": "CHOQUE",
        "tipo_evento_norm": "Choque",
        "vialidad_norm": "Vía primaria",
        "tipo_interseccion_norm": "Cruce",
        "semaforizada": True,
        "personas_lesionadas": 2,
        "personas_fallecidas": 0,
        "personas_sexo": "Hombre",
        "personas_edad": 34,
        "personas_tipo_usuario": "Motociclista",
        "vehiculo_tipo": "Motocicleta",
        "coord_estado": "ok",
        "lat": 19.4326,
        "lon": -99.1332,
        "ubicacion": {"type": "Point", "coordinates": [-99.1332, 19.4326]},
        "colonia": "Centro",
        "punto_1": "AV. JUÁREZ",
        "punto_2": "EJE CENTRAL",
    }
    base.update(cambios)
    return base


@pytest.fixture()
def atlas_db(mongo_client, settings):
    """Versión mínima publicada: tres hechos con los que se puede conciliar todo."""
    from app.db.mongo import ATLAS_ESTADO, ATLAS_HECHOS, ATLAS_IMPORTACIONES

    db = mongo_client[settings.mongodb_db]
    for nombre in (ATLAS_HECHOS, ATLAS_IMPORTACIONES, ATLAS_ESTADO):
        db[nombre].delete_many({})

    db[ATLAS_HECHOS].insert_many(
        [
            _hecho(),
            _hecho(
                folio="C2/20240102/00002",
                fecha="2024-01-02",
                hora=19,
                dia_semana=1,
                alcaldia="Iztapalapa",
                alcaldia_texto="IZTAPALAPA",
                tipo_evento="ATROPELLADO",
                tipo_evento_norm="Atropellado",
                semaforizada=False,
                personas_lesionadas=1,
                personas_fallecidas=1,
                personas_sexo="Mujer",
                personas_edad=None,
                personas_tipo_usuario="Peatón",
                vehiculo_tipo="Automóvil",
                lat=19.3589,
                lon=-99.0926,
                ubicacion={"type": "Point", "coordinates": [-99.0926, 19.3589]},
            ),
            _hecho(
                folio="C3/20240103/00003",
                fecha="2024-01-03",
                hora=None,
                dia_semana=2,
                alcaldia=None,
                alcaldia_texto="SIN COLONIA",
                tipo_evento="DERRAPADO",
                tipo_evento_norm="Derrapado",
                coord_estado="ausente",
                lat=None,
                lon=None,
                ubicacion=None,
                personas_lesionadas=0,
                personas_fallecidas=0,
                personas_sexo=None,
                personas_edad=None,
                personas_tipo_usuario="Conductor",
                vehiculo_tipo=None,
            ),
        ]
    )
    importacion = {
        "version": VERSION,
        "estado": "publicada",
        "publicado_en": "2026-11-04T20:00:00",
        "periodo": {"desde": "2024-01-01", "hasta": "2024-01-03"},
        "cifras": {"hechos": 3, "personas_lesionadas": 3, "personas_fallecidas": 1},
        "incidencias": {"coordenadas": {"ok": 2, "ausente": 1}},
        "fuente": {
            "conjunto": "hechos-de-transito",
            "titulo": "Hechos de tránsito (prueba)",
            "portal": "https://datos.cdmx.gob.mx/",
            "licencia": "CC-BY-4.0-ESP",
            "notas": "Conjunto de prueba",
            "catalogo_modificado": "2026-09-01T00:00:00",
            "recursos": {},
        },
        "recursos": {"hechos": {"nombre": "Hechos", "url": "https://x", "bytes": 1, "sha256": "abc"}},
    }
    db[ATLAS_IMPORTACIONES].insert_one(dict(importacion))
    db[ATLAS_ESTADO].insert_one({"_id": "activo", "version": VERSION})

    yield db

    for nombre in (ATLAS_HECHOS, ATLAS_IMPORTACIONES, ATLAS_ESTADO):
        db[nombre].delete_many({})


@pytest.fixture()
def atlas_client(atlas_db, settings):
    app = create_app(settings)
    from fastapi.testclient import TestClient

    with TestClient(app) as cliente:
        yield cliente


def test_estado_publica_la_version_activa(atlas_client):
    datos = atlas_client.get("/api/v1/atlas-vial/estado").json()
    assert datos["publicada"] is True
    assert datos["version"] == VERSION


def test_resumen_no_mezcla_hechos_con_personas(atlas_client):
    datos = atlas_client.get("/api/v1/atlas-vial/resumen").json()
    indicadores = datos["indicadores"]
    assert indicadores["hechos"]["valor"] == 3
    assert indicadores["personas_lesionadas"]["valor"] == 3
    assert indicadores["personas_fallecidas"]["valor"] == 1
    assert indicadores["hechos_con_fallecimientos"]["valor"] == 1
    assert indicadores["hechos_con_fallecimientos"]["denominador"] == 3
    # Cobertura explícita: un hecho sin coordenadas no desaparece del total.
    assert datos["cobertura"]["con_coordenadas"] == 2
    assert datos["cobertura"]["sin_coordenadas"] == 1
    assert datos["cobertura"]["sin_hora"] == 1


def test_territorio_reparte_los_hechos_sin_duplicarlos(atlas_client):
    datos = atlas_client.get("/api/v1/atlas-vial/territorio?incluir_puntos=false").json()
    suma = sum(fila["hechos"] for fila in datos["alcaldias"])
    assert suma == 3
    nombres = {fila["alcaldia"] for fila in datos["alcaldias"]}
    assert nombres == {"Cuauhtémoc", "Iztapalapa", None}, "el hecho sin alcaldía se conserva"


def test_puntos_solo_incluye_coordenadas_validas(atlas_client):
    datos = atlas_client.get("/api/v1/atlas-vial/puntos").json()
    assert datos["total"] == 2
    assert datos["mostrados"] == 2
    assert len(datos["folios"]) == 2
    assert datos["folio" if "folio" in datos else "folios"] is not None


def test_tiempo_cuadra_con_el_total(atlas_client):
    datos = atlas_client.get("/api/v1/atlas-vial/tiempo?agregacion=dia").json()
    assert sum(fila["hechos"] for fila in datos["serie"]) == 3
    assert datos["matriz"]["total_hechos"] == 2, "el hecho sin hora no entra en la matriz"
    suma_tipos = sum(fila["hechos"] for fila in datos["distribuciones"]["tipo_evento"])
    assert suma_tipos == 3


def test_personas_cuenta_hechos_y_muestra_el_desconocido(atlas_client):
    datos = atlas_client.get("/api/v1/atlas-vial/personas").json()
    assert sum(fila["hechos"] for fila in datos["por_tipo_usuario"]) == 3
    sexos = {fila["etiqueta"]: fila["hechos"] for fila in datos["por_sexo"]}
    assert sexos["Sin dato"] == 1, "el desconocido se muestra, no se convierte en cero"
    assert datos["cobertura"]["con_sexo"] == 2


def test_filtros_or_dentro_y_and_entre_dimensiones(atlas_client):
    una = atlas_client.get("/api/v1/atlas-vial/resumen?alcaldia=Cuauhtémoc").json()
    otra = atlas_client.get("/api/v1/atlas-vial/resumen?alcaldia=Iztapalapa").json()
    ambas = atlas_client.get(
        "/api/v1/atlas-vial/resumen?alcaldia=Cuauhtémoc&alcaldia=Iztapalapa"
    ).json()
    assert (
        ambas["indicadores"]["hechos"]["valor"]
        == una["indicadores"]["hechos"]["valor"] + otra["indicadores"]["hechos"]["valor"]
    )
    cruzado = atlas_client.get(
        "/api/v1/atlas-vial/resumen?alcaldia=Cuauhtémoc&tipo_evento=Choque"
    ).json()
    assert cruzado["indicadores"]["hechos"]["valor"] == 1


def test_busqueda_por_folio(atlas_client):
    datos = atlas_client.get("/api/v1/atlas-vial/resumen?buscar=C2/20240102/00002").json()
    assert datos["indicadores"]["hechos"]["valor"] == 1


def test_tabla_y_exportacion_coinciden_con_el_resumen(atlas_client):
    tabla = atlas_client.get("/api/v1/atlas-vial/tabla?tamano=50").json()
    assert tabla["total"] == 3
    assert len(tabla["filas"]) == 3
    fila = tabla["filas"][0]
    assert set(
        ["folio", "fecha", "hora", "alcaldia", "tipo_evento", "ubicacion", "lesionadas", "fallecidas"]
    ) <= set(fila)

    exportacion = atlas_client.get("/api/v1/atlas-vial/exportar")
    assert exportacion.status_code == 200
    texto = exportacion.content.decode("utf-8-sig")
    lineas = [linea for linea in texto.splitlines() if linea and not linea.startswith("#")]
    assert len(lineas) - 1 == 3
    filas = list(csv.DictReader(io.StringIO("\n".join(lineas))))
    assert {fila["folio"] for fila in filas} == {
        "C1/20240101/00001",
        "C2/20240102/00002",
        "C3/20240103/00003",
    }
    assert "Fuente:" in texto and "Versión de datos" in texto


def test_detalle_por_folio_con_barras(atlas_client):
    datos = atlas_client.get("/api/v1/atlas-vial/evento/C1/20240101/00001")
    assert datos.status_code == 200
    assert datos.json()["folio"] == "C1/20240101/00001"
    inexistente = atlas_client.get("/api/v1/atlas-vial/evento/C9/20240101/99999")
    assert inexistente.status_code == 404


def test_comparacion_usa_la_misma_base(atlas_client):
    datos = atlas_client.get(
        "/api/v1/atlas-vial/comparar?comparar=Cuauhtémoc&comparar=Iztapalapa"
    ).json()
    assert len(datos["alcaldias"]) == 2
    for fila in datos["alcaldias"]:
        assert fila["hechos"] >= 1
        assert fila["composicion_tipo_evento"]
        total = sum(tipo["hechos"] for tipo in fila["composicion_tipo_evento"])
        assert total == fila["hechos"]


def test_validacion_de_bbox_invertida(atlas_client):
    respuesta = atlas_client.get("/api/v1/atlas-vial/resumen?bbox=-99.0,19.3,-99.2,19.4")
    assert respuesta.status_code == 422


def test_fallo_de_sincronizacion_conserva_la_version_publicada(atlas_db, monkeypatch):
    """Si el catálogo no responde, la versión publicada no cambia."""
    from app.atlas import importer

    def explotar(*_argumentos, **_claves):
        raise importer.sources.ErrorCatalogo("catálogo no disponible")

    monkeypatch.setattr(importer.sources, "resolver_catalogo", explotar)
    resultado = importer.sincronizar(atlas_db)
    assert resultado.exito is False
    assert importer.version_activa(atlas_db)["version"] == VERSION
    fallo = atlas_db["atlas_importaciones"].find_one({"estado": "rechazada"})
    assert fallo is not None, "el intento fallido queda registrado con su motivo"
    assert "catálogo no disponible" in (fallo.get("motivo") or "")
