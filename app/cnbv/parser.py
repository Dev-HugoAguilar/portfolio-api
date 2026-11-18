"""Lectura y normalización del Boletín Estadístico de Banca Múltiple (CNBV).

El parser no conoce MongoDB y no inventa nada: lee las celdas del archivo
original, las conserva y, aparte, produce los hechos normalizados.

Reglas comprobadas sobre los archivos reales
--------------------------------------------
* La fila de encabezado **no está en una posición fija**: en los boletines de
  2022-05 y 2023-01 está en la fila 5 y en los de 2024 en la fila 6. Se localiza
  buscando la celda ``Sistema``.
* La columna de conceptos es la anterior a ``Sistema``; las instituciones van a
  su derecha y su número y orden **cambian entre periodos** (por eso se
  identifican por nombre, nunca por posición).
* La primera fila trae el título, p. ej. ``Octubre de 2024\\nMillones de pesos``.
  El periodo se toma del nombre del archivo y se contrasta con el título cuando
  este es legible (hay erratas en la fuente, p. ej. "Septimebre de 2024").
* Algunos conceptos se repiten con significados distintos (``Créditos
  Comerciales`` aparece en etapa 1, etapa 2 y etapa 3). El identificador incluye
  el número de ocurrencia en el orden del boletín, que es estable entre
  periodos.
* Hay filas de notas al pie dentro de la misma columna de conceptos: se rechazan
  y se informan en el reporte.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import openpyxl

from app.cnbv import sources

#: Máximo de columnas leídas (el archivo declara 16181 por formato, no por datos).
MAX_COLUMNAS = 200
#: Máximo de filas leídas de la hoja de saldos.
MAX_FILAS = 400

#: Marcas de nota al pie que se eliminan del nombre del concepto.
_MARCAS = re.compile(r"(\*+|\(\d+\)|\d+/)+$")

#: Filas que no son hechos sino títulos de sección.
SECCIONES: dict[str, tuple[str, str]] = {
    "estado de situacion financiera": ("balance", "saldo"),
    "estado de resultado integral": ("resultados", "flujo_acumulado"),
    "cuentas de orden": ("cuentas_orden", "saldo"),
}

MESES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


class ErrorFormatoFuente(ValueError):
    """El archivo no tiene la estructura esperada del boletín."""


@dataclass
class ResultadoBoletin:
    """Resultado del parseo de un boletín, listo para importarse."""

    periodo: str
    fecha: date
    archivo: str
    titulo_hoja: str | None
    unidad_declarada: str
    filas_crudas: list[dict] = field(default_factory=list)
    hechos: list[dict] = field(default_factory=list)
    indicadores: list[dict] = field(default_factory=list)
    rechazados: list[dict] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    instituciones: list[dict] = field(default_factory=list)
    conceptos: list[dict] = field(default_factory=list)
    ausentes: int = 0


def _texto(valor: object) -> str:
    return "" if valor is None else str(valor)


def _limpiar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto.replace("\n", " ")).strip()


def normalizar_concepto(texto: str) -> str:
    """Minúsculas, sin acentos y sin marcas de nota al pie."""
    limpio = _limpiar(texto)
    limpio = _MARCAS.sub("", limpio).strip()
    sin_acentos = unicodedata.normalize("NFKD", limpio).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", sin_acentos).strip().lower()


def slug_concepto(texto: str) -> str:
    """Identificador estable a partir del nombre del concepto."""
    base = normalizar_concepto(texto)
    slug = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    if not slug:
        raise ErrorFormatoFuente(f"concepto sin identificador válido: {texto!r}")
    return slug[:120]


def slug_institucion(nombre: str) -> str:
    sin_acentos = unicodedata.normalize("NFKD", _limpiar(nombre)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", sin_acentos.lower()).strip("_")


def _es_nota_al_pie(texto: str) -> bool:
    limpio = _limpiar(texto)
    return bool(re.match(r"^\(\d+\)", limpio)) or bool(re.match(r"^\d+[.)]\s", limpio))


def _es_numero(valor: object) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _localizar_estructura(filas: list[tuple]) -> tuple[int, str, int, int]:
    """Devuelve (fila_unidades, texto_unidades, fila_cabecera, col_sistema)."""
    fila_unidades = -1
    texto_unidades = ""
    for indice, fila in enumerate(filas):
        for celda in fila:
            if isinstance(celda, str) and "millones de pesos" in celda.lower():
                fila_unidades = indice
                texto_unidades = _limpiar(celda)
                break
        if fila_unidades >= 0:
            break

    for indice, fila in enumerate(filas):
        for columna, celda in enumerate(fila):
            if isinstance(celda, str) and normalizar_concepto(celda) == "sistema":
                return fila_unidades, texto_unidades, indice, columna

    raise ErrorFormatoFuente("no se encontró la columna 'Sistema' en la hoja de saldos")


def _periodo_del_titulo(titulo: str | None) -> tuple[int, int] | None:
    if not titulo:
        return None
    coincidencia = re.search(r"([A-Za-zÁÉÍÓÚáéíóú]+)\s+de\s+(\d{4})", titulo)
    if not coincidencia:
        return None
    mes = MESES.get(coincidencia.group(1).lower())
    if mes is None:
        return None
    return mes, int(coincidencia.group(2))


def parsear_boletin(ruta: Path, periodo: str) -> ResultadoBoletin:
    """Lee un boletín ``BE_BM_AAAAMM.xlsx`` y devuelve filas crudas y hechos."""
    iso = sources.periodo_a_iso(periodo)
    anio, mes = int(iso[:4]), int(iso[5:])
    fecha_periodo = date(anio, mes, 1)

    libro = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
    if sources.HOJA_SALDOS not in libro.sheetnames:
        libro.close()
        raise ErrorFormatoFuente(f"el archivo no tiene la hoja {sources.HOJA_SALDOS!r}")

    hoja = libro[sources.HOJA_SALDOS]
    filas = list(
        hoja.iter_rows(
            min_row=1,
            max_row=min(hoja.max_row or MAX_FILAS, MAX_FILAS),
            max_col=MAX_COLUMNAS,
            values_only=True,
        )
    )

    resultado = ResultadoBoletin(
        periodo=periodo,
        fecha=fecha_periodo,
        archivo=ruta.name,
        titulo_hoja=None,
        unidad_declarada="",
    )

    fila_unidades, texto_unidades, fila_cabecera, col_sistema = _localizar_estructura(filas)
    resultado.titulo_hoja = texto_unidades or None
    resultado.unidad_declarada = texto_unidades

    if "millones de pesos" not in texto_unidades.lower():
        libro.close()
        raise ErrorFormatoFuente(
            "la hoja de saldos no declara 'Millones de pesos'; no se asume unidad"
        )

    # Contraste del periodo: el archivo manda, el título solo confirma.
    del_titulo = _periodo_del_titulo(texto_unidades)
    if del_titulo is None:
        resultado.avisos.append(
            "No se pudo leer el mes en el título del boletín; el periodo se toma del "
            "nombre del archivo."
        )
    elif del_titulo != (mes, anio):
        libro.close()
        raise ErrorFormatoFuente(
            f"el título del boletín indica {del_titulo[0]:02d}/{del_titulo[1]} pero el "
            f"archivo corresponde a {mes:02d}/{anio}"
        )

    cabecera = filas[fila_cabecera]
    col_concepto = col_sistema - 1
    if col_concepto < 0:
        libro.close()
        raise ErrorFormatoFuente("la columna 'Sistema' está en la primera columna")

    columnas: list[tuple[int, str, str, bool]] = []
    for columna in range(col_sistema, len(cabecera)):
        celda = cabecera[columna]
        if isinstance(celda, str) and celda.strip():
            nombre = _limpiar(celda)
            columnas.append(
                (
                    columna,
                    slug_institucion(normalizar_concepto(nombre)),
                    nombre,
                    normalizar_concepto(nombre).startswith("sistema"),
                )
            )

    if not columnas:
        libro.close()
        raise ErrorFormatoFuente("no se encontraron instituciones en el encabezado")
    if not columnas[0][3]:
        libro.close()
        raise ErrorFormatoFuente("la primera columna de datos debería ser 'Sistema'")

    resultado.instituciones = [
        {"institucion_id": ident, "nombre": nombre, "es_sistema": es_sistema}
        for _col, ident, nombre, es_sistema in columnas
    ]

    seccion, naturaleza = "sin_clasificar", "saldo"
    ocurrencias: dict[str, int] = {}

    for indice in range(fila_cabecera + 1, len(filas)):
        fila = filas[indice]
        numero_fila = indice + 1
        concepto_celda = fila[col_concepto] if len(fila) > col_concepto else None
        if not isinstance(concepto_celda, str) or not concepto_celda.strip():
            continue

        concepto_original = _limpiar(concepto_celda)
        normalizado = normalizar_concepto(concepto_original)

        if _es_nota_al_pie(concepto_original):
            resultado.rechazados.append(
                {
                    "fila": numero_fila,
                    "concepto": concepto_original,
                    "motivo": "nota_al_pie",
                }
            )
            continue

        valores_crudos = []
        valores_numericos = 0
        for columna, ident, nombre, es_sistema in columnas:
            celda = fila[columna] if len(fila) > columna else None
            valores_crudos.append(
                {
                    "columna": columna + 1,
                    "institucion_id": ident,
                    "institucion_original": nombre,
                    "valor_crudo": celda,
                }
            )
            if _es_numero(celda):
                valores_numericos += 1
            elif celda is None or (isinstance(celda, str) and not celda.strip()):
                resultado.ausentes += 1
            else:
                resultado.rechazados.append(
                    {
                        "fila": numero_fila,
                        "concepto": concepto_original,
                        "institucion": nombre,
                        "motivo": "valor_no_numerico",
                        "valor_crudo": str(celda),
                    }
                )

        if normalizado in SECCIONES:
            seccion, naturaleza = SECCIONES[normalizado]
            continue

        if valores_numericos == 0:
            resultado.rechazados.append(
                {
                    "fila": numero_fila,
                    "concepto": concepto_original,
                    "motivo": "sin_valores_numericos",
                }
            )
            continue

        base = slug_concepto(concepto_original)
        ocurrencias[base] = ocurrencias.get(base, 0) + 1
        concepto_id = f"{base}#{ocurrencias[base]}"

        resultado.filas_crudas.append(
            {
                "hoja": sources.HOJA_SALDOS,
                "fila": numero_fila,
                "concepto_original": concepto_original,
                "concepto_id": concepto_id,
                "seccion": seccion,
                "valores": valores_crudos,
            }
        )

        if not any(c["concepto_id"] == concepto_id for c in resultado.conceptos):
            resultado.conceptos.append(
                {
                    "concepto_id": concepto_id,
                    "nombre_original": concepto_original,
                    "seccion": seccion,
                    "naturaleza": naturaleza,
                    "unidad": sources.UNIDAD_SALDOS,
                    "orden": numero_fila,
                    "ocurrencia": ocurrencias[base],
                }
            )

        for columna, ident, nombre, es_sistema in columnas:
            celda = fila[columna] if len(fila) > columna else None
            if not _es_numero(celda):
                continue  # ausente o no numérico: no se convierte en cero
            resultado.hechos.append(
                {
                    "periodo": sources.periodo_a_iso(periodo),
                    "fecha": fecha_periodo,
                    "institucion_id": ident,
                    "institucion_nombre": nombre,
                    "es_sistema": es_sistema,
                    "concepto_id": concepto_id,
                    "concepto_nombre": concepto_original,
                    "seccion": seccion,
                    "naturaleza": naturaleza,
                    "valor": float(celda),
                    "unidad": sources.UNIDAD_SALDOS,
                    "hoja": sources.HOJA_SALDOS,
                    "fila": numero_fila,
                    "columna": columna + 1,
                }
            )

    if sources.HOJA_INDICADORES in libro.sheetnames:
        try:
            resultado.indicadores, avisos_ind = _parsear_indicadores(
                libro[sources.HOJA_INDICADORES], periodo, fecha_periodo
            )
            resultado.avisos.extend(avisos_ind)
        except ErrorFormatoFuente as error:
            resultado.avisos.append(f"No se importaron los indicadores: {error}")

    libro.close()

    if not resultado.hechos:
        raise ErrorFormatoFuente("el boletín no produjo ningún hecho importable")

    resultado.avisos.append(
        f"Se conservaron {len(resultado.filas_crudas)} filas originales; "
        f"{resultado.ausentes} celdas vacías no se convirtieron en cero."
    )
    return resultado


def _parsear_indicadores(
    hoja, periodo: str, fecha_boletin: date
) -> tuple[list[dict], list[str]]:
    """Lee la hoja ``Indicadores`` (porcentajes publicados por la CNBV)."""
    filas = list(
        hoja.iter_rows(
            min_row=1,
            max_row=min(hoja.max_row or MAX_FILAS, MAX_FILAS),
            max_col=MAX_COLUMNAS,
            values_only=True,
        )
    )
    avisos: list[str] = []

    fila_instituciones = None
    col_nombres = None
    for indice, fila in enumerate(filas):
        for columna, celda in enumerate(fila):
            if isinstance(celda, str) and normalizar_concepto(celda).startswith("sistema"):
                fila_instituciones, col_nombres = indice, columna
                break
        if fila_instituciones is not None:
            break
    if fila_instituciones is None or fila_instituciones < 2:
        raise ErrorFormatoFuente("no se localizó el bloque de instituciones")

    fila_fechas = fila_instituciones - 1
    fila_nombres = fila_instituciones - 2

    grupos: list[tuple[int, str]] = []
    for columna, celda in enumerate(filas[fila_nombres]):
        if isinstance(celda, str) and celda.strip():
            grupos.append((columna, _limpiar(celda)))
    if not grupos:
        raise ErrorFormatoFuente("no se encontraron indicadores en el encabezado")

    fechas_por_columna: dict[int, date] = {}
    for columna, celda in enumerate(filas[fila_fechas]):
        if isinstance(celda, date):
            fechas_por_columna[columna] = celda
        elif hasattr(celda, "year") and hasattr(celda, "month"):
            fechas_por_columna[columna] = date(celda.year, celda.month, 1)

    indicadores: list[dict] = []
    for indice in range(fila_instituciones, len(filas)):
        fila = filas[indice]
        celda_nombre = fila[col_nombres] if len(fila) > col_nombres else None
        if not isinstance(celda_nombre, str) or not celda_nombre.strip():
            continue
        nombre_institucion = _limpiar(celda_nombre)
        normalizado = normalizar_concepto(nombre_institucion)
        es_sistema = normalizado.startswith("sistema")
        institucion_id = slug_institucion(normalizado)

        for posicion, (columna, nombre_indicador) in enumerate(grupos):
            limite = grupos[posicion + 1][0] if posicion + 1 < len(grupos) else len(fila)
            indicador_id = slug_concepto(nombre_indicador)
            for col in range(columna, limite):
                fecha = fechas_por_columna.get(col)
                if fecha is None:
                    continue
                valor = fila[col] if len(fila) > col else None
                if not _es_numero(valor):
                    continue
                indicadores.append(
                    {
                        "periodo": f"{fecha.year:04d}-{fecha.month:02d}",
                        "fecha": fecha,
                        "periodo_boletin": sources.periodo_a_iso(periodo),
                        "fecha_boletin": fecha_boletin,
                        "institucion_id": institucion_id,
                        "institucion_nombre": nombre_institucion,
                        "es_sistema": es_sistema,
                        "indicador_id": indicador_id,
                        "indicador_nombre": nombre_indicador,
                        "nota": _nota_indicador(nombre_indicador),
                        "valor": round(float(valor), 6),
                        "unidad": sources.UNIDAD_INDICADORES,
                    }
                )

    if not indicadores:
        raise ErrorFormatoFuente("la hoja de indicadores no produjo valores")

    avisos.append(
        f"Indicadores importados tal como los publica la CNBV (porcentajes): "
        f"{len({i['indicador_id'] for i in indicadores})} indicadores."
    )
    return indicadores, avisos


def _nota_indicador(nombre: str) -> str | None:
    coincidencia = re.search(r"\(([^)]+)\)", nombre)
    return _limpiar(coincidencia.group(1)) if coincidencia else None
