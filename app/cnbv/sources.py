"""Fuente oficial de datos: CNBV — Boletín Estadístico de Banca Múltiple.

Procedencia verificada (no supuesta)
------------------------------------
* Portal: Portafolio de Información de la CNBV
  (https://portafolioinfo.cnbv.gob.mx/Paginas/Inicio.aspx), al que se llega desde
  la página oficial de Información Estadística
  (https://www.cnbv.gob.mx/Paginas/Informacion-Estadistica.aspx) y desde la
  sección de Banca Múltiple
  (https://www.cnbv.gob.mx/SECTORES-SUPERVISADOS/BANCA-MULTIPLE/Paginas/Información-Estadística.aspx).
* Los archivos viven en la biblioteca de documentos pública del portal:
  ``https://portafolioinfo.cnbv.gob.mx/PortafolioInformacion/<archivo>``
* Nombre de archivo del boletín estadístico: ``BE_BM_AAAAMM.xlsx`` (un archivo
  por mes). La lista :data:`PERIODOS_VERIFICADOS` contiene **solo** los periodos
  cuya descarga se comprobó con respuesta HTTP 200 y contenido XLSX válido.

Unidades y naturaleza de las cifras (comprobadas en los archivos)
-----------------------------------------------------------------
* ``EF Resumen``: "Millones de pesos" — saldos y resultados, **sin conversión**
  (el valor se guarda tal como lo publica la CNBV).
* La hoja declara el mes en el título; el periodo del sistema se toma del nombre
  del archivo y se contrasta con el título cuando este es legible.
* ``Indicadores``: "Porcentajes" (ROA y ROE, flujo 12 meses) publicados por la
  CNBV; **no se recalculan**.

Condiciones de uso y atribución
-------------------------------
* Atribución: "Fuente: Comisión Nacional Bancaria y de Valores (CNBV), Boletín
  Estadístico de Banca Múltiple, <mes> <año>".
* El boletín se elabora con los reportes regulatorios que las entidades envían a
  la CNBV; su propia nota metodológica advierte que la información "puede ser
  modificada derivado de las revisiones internas" y que "las cifras aquí
  contenidas no se actualizan en el tiempo".
* En los archivos descargados **no** se encontró una licencia de datos abiertos
  explícita: aplican los términos de uso del portal de la CNBV. Por eso el
  proyecto solo cita la fuente, no redistribuye los archivos y conserva el
  original fuera de Git.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Identificador interno del dataset real (no confundir con el sintético).
DATASET_ID = "cnbv_banca_multiple_boletin"

NOMBRE_FUENTE = "CNBV · Boletín Estadístico de Banca Múltiple"
PORTAL = "https://portafolioinfo.cnbv.gob.mx/Paginas/Inicio.aspx"
PAGINA_ESTADISTICA = (
    "https://www.cnbv.gob.mx/SECTORES-SUPERVISADOS/BANCA-MULTIPLE/Paginas/"
    "Informaci%C3%B3n-Estad%C3%ADstica.aspx"
)
URL_BASE_ARCHIVOS = "https://portafolioinfo.cnbv.gob.mx/PortafolioInformacion"
PATRON_ARCHIVO = "BE_BM_{periodo}.xlsx"

#: Sector del portafolio de información (40 = Banca Múltiple).
SECTOR = "40"

MONEDA = "MXN"
#: Unidad de la hoja "EF Resumen", tal como la declara el archivo.
UNIDAD_SALDOS = "millones_mxn"
UNIDAD_INDICADORES = "porcentaje"

HOJA_SALDOS = "EF Resumen"
HOJA_INDICADORES = "Indicadores"
HOJA_NOTAS = "Nota"

ATRIBUCION = (
    "Fuente: Comisión Nacional Bancaria y de Valores (CNBV), Boletín Estadístico "
    "de Banca Múltiple."
)

#: Periodos (AAAAMM) cuya existencia y descarga se comprobaron una por una.
#: Los huecos son reales: por ejemplo 202406 no está publicado.
PERIODOS_VERIFICADOS: tuple[str, ...] = (
    "202005",
    "202006",
    "202007",
    "202008",
    "202011",
    "202012",
    "202101",
    "202102",
    "202112",
    "202201",
    "202202",
    "202203",
    "202204",
    "202205",
    "202206",
    "202207",
    "202208",
    "202209",
    "202210",
    "202211",
    "202212",
    "202301",
    "202302",
    "202303",
    "202304",
    "202305",
    "202306",
    "202307",
    "202310",
    "202311",
    "202312",
    "202401",
    "202402",
    "202403",
    "202404",
    "202405",
    "202407",
    "202408",
    "202409",
    "202410",
)

#: Periodos que se importan por omisión: una serie mensual de 2024 más el mismo
#: mes del año anterior (para comparaciones anuales de conceptos acumulados).
PERIODOS_POR_OMISION: tuple[str, ...] = (
    "202310",
    "202401",
    "202404",
    "202407",
    "202408",
    "202409",
    "202410",
)

CONDICIONES_USO: tuple[str, ...] = (
    "Las cifras provienen de los reportes regulatorios que las entidades "
    "supervisadas entregan a la CNBV; su nota metodológica advierte que pueden "
    "modificarse por revisiones internas.",
    "El boletín no se actualiza en el tiempo: para series históricas la CNBV "
    "recomienda el Portafolio de Información. Por eso aquí se registra el hash de "
    "cada archivo importado y se conservan las revisiones.",
    "Atribución obligatoria a la CNBV al mostrar cualquier cifra.",
    "Los archivos originales no se versionan en Git y no se redistribuyen.",
)

ADVERTENCIAS_METRICA: tuple[str, ...] = (
    "Cifras en millones de pesos, tal como las publica la CNBV; no se convierten "
    "de unidad.",
    "La columna «Sistema» es el agregado publicado por la CNBV: no se suma con "
    "las instituciones (sería doble conteo).",
    "Los rubros del estado de resultado integral son acumulados del ejercicio "
    "(enero–mes): no se comparan contra el mes inmediato anterior.",
    "No se suman saldos a través del tiempo ni se promedian porcentajes.",
    "Los datos son de banca múltiple en operación en el mes del boletín; el "
    "conjunto de instituciones cambia entre periodos.",
)


@dataclass(frozen=True)
class ArchivoFuente:
    periodo: str
    nombre: str
    url: str


def periodo_a_iso(periodo: str) -> str:
    """``202410`` -> ``2024-10``."""
    if len(periodo) != 6 or not periodo.isdigit():
        raise ValueError(f"periodo no válido: {periodo!r} (se espera AAAAMM)")
    mes = int(periodo[4:])
    if not 1 <= mes <= 12:
        raise ValueError(f"periodo no válido: {periodo!r} (mes fuera de rango)")
    return f"{periodo[:4]}-{periodo[4:]}"


def iso_a_periodo(iso: str) -> str:
    """``2024-10`` -> ``202410``."""
    partes = iso.split("-")
    if len(partes) != 2:
        raise ValueError(f"periodo no válido: {iso!r} (se espera AAAA-MM)")
    return f"{partes[0]}{partes[1]}"


def archivo_fuente(periodo: str) -> ArchivoFuente:
    """Nombre y URL del boletín para un periodo ya verificado."""
    if periodo not in PERIODOS_VERIFICADOS:
        raise ValueError(
            f"El periodo {periodo!r} no está en la lista verificada de boletines "
            "publicados; no se inventan URLs de descarga."
        )
    nombre = PATRON_ARCHIVO.format(periodo=periodo)
    return ArchivoFuente(periodo=periodo, nombre=nombre, url=f"{URL_BASE_ARCHIVOS}/{nombre}")


def detalle_fuente() -> dict:
    """Metadatos de la fuente para la API."""
    return {
        "dataset_id": DATASET_ID,
        "nombre": NOMBRE_FUENTE,
        "organismo": "Comisión Nacional Bancaria y de Valores (CNBV)",
        "portal": PORTAL,
        "pagina_estadistica": PAGINA_ESTADISTICA,
        "sector": SECTOR,
        "sector_nombre": "Banca Múltiple",
        "patron_archivo": PATRON_ARCHIVO,
        "url_base_archivos": URL_BASE_ARCHIVOS,
        "moneda": MONEDA,
        "unidad_saldos": UNIDAD_SALDOS,
        "unidad_indicadores": UNIDAD_INDICADORES,
        "atribucion": ATRIBUCION,
        "condiciones_uso": list(CONDICIONES_USO),
        "advertencias_metrica": list(ADVERTENCIAS_METRICA),
        "periodos_verificados": list(PERIODOS_VERIFICADOS),
        "periodos_por_omision": list(PERIODOS_POR_OMISION),
        "es_sintetico": False,
    }
