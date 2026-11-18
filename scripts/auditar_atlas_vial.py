"""Auditoría de los cinco CSV de la SSC para «Atlas vial · Ciudad de México».

Lee los archivos COMPLETOS (no muestras), con el módulo `csv` de la biblioteca
estándar, y responde preguntas verificables:

  · separador, codificación, BOM y encabezados (incluidos los duplicados);
  · número de registros y de folios únicos, duplicados de folio;
  · fechas mínima y máxima interpretadas como dd/mm/aaaa, y rango de horas;
  · valores ausentes por columna;
  · cobertura entre el archivo principal de hechos y cada recurso complementario;
  · granularidad real de cada fila (una fila por folio o varias);
  · coherencia de los totales de personas con el archivo principal;
  · cuántos campos vienen con doble codificación (mojibake) y si la reparación
    propuesta revierte el texto sin pérdida.

Uso:
    python scripts/auditar_atlas_vial.py            # resumen por consola
    python scripts/auditar_atlas_vial.py --json     # informe completo (JSON)

El resultado completo se escribe en `docs/atlas-vial/auditoria-fuentes.json`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ENTRADA = Path("D:/Downloads")
SALIDA = RAIZ.parent / "docs" / "atlas-vial"

# Los cinco recursos oficiales, por su identificador de descarga del catálogo.
RECURSOS = {
    "hechos": {
        "archivo": "27b88c58-e741-4526-bfd0-5200fe272f8d.csv",
        "etiqueta": "Hechos de tránsito",
    },
    "sexo": {
        "archivo": "273ac0c3-01f0-46e9-9000-a2d4ddb556de.csv",
        "etiqueta": "Personas por sexo",
    },
    "edad": {
        "archivo": "6eafd074-df59-4f9c-86f3-57e2fc5094c4.csv",
        "etiqueta": "Personas por edad",
    },
    "usuario": {
        "archivo": "e152d68d-eda7-4ac9-972b-b926c116d4ab.csv",
        "etiqueta": "Personas por tipo de usuario de la vía",
    },
    "vehiculos": {
        "archivo": "d377236d-d2b7-4a14-aa65-88dca057af3a.csv",
        "etiqueta": "Vehículos involucrados",
    },
}

# Marcas de doble codificación: un carácter «Ã»/«Â» seguido de otro de la banda
# latin-1 alta. Es el resultado de decodificar UTF-8 como latin-1 y volver a
# guardar como UTF-8.
MOJIBAKE = re.compile(r"[\u00c2\u00c3][\u0080-\u00bf]")

FECHA = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
HORA = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def leer_texto(ruta: Path) -> tuple[str, dict]:
    """Devuelve el texto y los metadatos de codificación del archivo."""
    crudo = ruta.read_bytes()
    meta = {
        "bytes": len(crudo),
        "sha256": hashlib.sha256(crudo).hexdigest(),
        "bom_utf8": crudo[:3] == b"\xef\xbb\xbf",
    }
    try:
        texto = crudo.decode("utf-8-sig")
        meta["codificacion"] = "utf-8"
        meta["decodifica_sin_error"] = True
    except UnicodeDecodeError as error:
        texto = crudo.decode("cp1252", errors="replace")
        meta["codificacion"] = "cp1252 (utf-8 falló)"
        meta["decodifica_sin_error"] = False
        meta["error_utf8"] = str(error)
    return texto, meta


def reparar_mojibake(valor: str) -> str | None:
    """Revierte la doble codificación si y solo si el texto vuelve íntegro."""
    if not MOJIBAKE.search(valor):
        return None
    try:
        reparado = valor.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return reparado if reparado != valor else None


def auditar_archivo(clave: str, definicion: dict) -> dict:
    ruta = ENTRADA / definicion["archivo"]
    if not ruta.exists():
        return {"clave": clave, "error": f"no se encuentra {ruta}"}

    texto, meta = leer_texto(ruta)
    lineas = texto.splitlines()
    cabecera_cruda = lineas[0] if lineas else ""

    # Separador: se prueba con el módulo csv sobre la cabecera.
    delimitador = ","
    if cabecera_cruda.count(";") > cabecera_cruda.count(","):
        delimitador = ";"
    elif cabecera_cruda.count("\t") > cabecera_cruda.count(","):
        delimitador = "\t"

    lector = csv.reader(io.StringIO(texto), delimiter=delimitador)
    cabecera = next(lector)

    # Encabezados duplicados: se conservan todas las columnas y se numeran para
    # no sobrescribir valores.
    conteo_cabecera = Counter(cabecera)
    duplicados = {nombre: veces for nombre, veces in conteo_cabecera.items() if veces > 1}
    columnas = []
    vistos: Counter = Counter()
    for indice, nombre in enumerate(cabecera):
        vistos[nombre] += 1
        columnas.append({
            "indice": indice,
            "nombre": nombre,
            "ocurrencia": vistos[nombre],
            "clave": nombre if vistos[nombre] == 1 else f"{nombre}__{vistos[nombre]}",
        })

    filas = 0
    folios = Counter()
    fechas: list[datetime] = []
    fechas_invalidas = 0
    horas_validas = 0
    horas_invalidas = 0
    vacios = Counter()
    mojibake = Counter()
    reparados = Counter()
    ejemplos_mojibake: dict[str, str] = {}
    columnas_largas = set()

    valores_folio_por_columna: dict[str, Counter] = {}
    for col in columnas:
        if col["indice"] >= len(cabecera):
            continue
    indice_por_clave = {col["clave"]: col["indice"] for col in columnas}
    indice_folio = indice_por_clave.get("folio")
    indice_fecha = indice_por_clave.get("fecha_evento")
    indice_hora = indice_por_clave.get("hora_evento")

    for fila in lector:
        filas += 1
        if len(fila) != len(cabecera):
            columnas_largas.add(len(fila))
        for col in columnas:
            posicion = col["indice"]
            valor = fila[posicion] if posicion < len(fila) else ""
            if valor.strip() == "":
                vacios[col["clave"]] += 1
            if MOJIBAKE.search(valor):
                mojibake[col["clave"]] += 1
                arreglado = reparar_mojibake(valor)
                if arreglado is not None:
                    reparados[col["clave"]] += 1
                    if col["clave"] not in ejemplos_mojibake:
                        ejemplos_mojibake[col["clave"]] = f"{valor} → {arreglado}"
        if indice_folio is not None and indice_folio < len(fila):
            folios[fila[indice_folio].strip()] += 1
        if indice_fecha is not None and indice_fecha < len(fila):
            valor = fila[indice_fecha].strip()
            coincidencia = FECHA.match(valor)
            if coincidencia:
                dia, mes, anio = (int(parte) for parte in coincidencia.groups())
                try:
                    fechas.append(datetime(anio, mes, dia))
                except ValueError:
                    fechas_invalidas += 1
            elif valor:
                fechas_invalidas += 1
        if indice_hora is not None and indice_hora < len(fila):
            valor = fila[indice_hora].strip()
            coincidencia = HORA.match(valor)
            if coincidencia:
                horas_validas += 1
            elif valor:
                horas_invalidas += 1

    # Cardinalidad: filas por folio (1 = una fila por hecho).
    repeticiones = Counter(folios.values())

    # Columnas categóricas: valores distintos (hasta 40) para conocer el dominio.
    categoricas = {}
    candidatas = [
        "tipo_evento", "alcaldia", "zona_vial", "sector", "tipo_de_interseccion",
        "interseccion_semaforizada", "clasificacion_de_la_vialidad", "sentido_de_circulacion",
        "dia", "prioridad", "origen", "sexo", "condicion_persona",
        "condicion_persona__2", "tipo_vehiculo", "trasladado_lesionados",
    ]
    if filas:
        lector2 = csv.reader(io.StringIO(texto), delimiter=delimitador)
        next(lector2)
        acumulado: dict[str, Counter] = {nombre: Counter() for nombre in candidatas if nombre in indice_por_clave}
        for fila in lector2:
            for nombre in acumulado:
                posicion = indice_por_clave[nombre]
                if posicion < len(fila):
                    valor = reparar_mojibake(fila[posicion]) or fila[posicion]
                    acumulado[nombre][valor.strip()] += 1
        for nombre, contador in acumulado.items():
            categoricas[nombre] = {
                "distintos": len(contador),
                "frecuentes": contador.most_common(12),
            }

    return {
        "clave": clave,
        "etiqueta": definicion["etiqueta"],
        "archivo": definicion["archivo"],
        **meta,
        "lineas": len(lineas),
        "separador": delimitador,
        "columnas": columnas,
        "columnas_totales": len(cabecera),
        "encabezados_duplicados": duplicados,
        "filas": filas,
        "filas_con_columnas_distintas": sorted(columnas_largas),
        "folios_unicos": len(folios),
        "folios_duplicados": sum(1 for veces in folios.values() if veces > 1),
        "filas_por_folio": {str(k): v for k, v in sorted(repeticiones.items())},
        "fecha_min": min(fechas).strftime("%Y-%m-%d") if fechas else None,
        "fecha_max": max(fechas).strftime("%Y-%m-%d") if fechas else None,
        "fechas_interpretadas": len(fechas),
        "fechas_invalidas": fechas_invalidas,
        "horas_validas": horas_validas,
        "horas_invalidas": horas_invalidas,
        "vacios": dict(vacios),
        "mojibake": {"columnas": dict(mojibake), "reparables": dict(reparados), "ejemplos": ejemplos_mojibake},
        "categoricas": categoricas,
        "_folios": folios,
        "_texto": texto,
    }


def sumar_columna(filas: list[dict], nombre: str) -> tuple[int, int]:
    """Suma una columna numérica: devuelve (total, celdas no numéricas)."""
    total = 0
    invalidas = 0
    for fila in filas:
        valor = fila.get(nombre, "").strip()
        if valor == "":
            continue
        try:
            total += int(float(valor))
        except ValueError:
            invalidas += 1
    return total, invalidas


def filas_dict(reporte: dict) -> list[dict]:
    lector = csv.reader(io.StringIO(reporte["_texto"]), delimiter=reporte["separador"])
    cabecera = next(lector)
    claves = [col["clave"] for col in reporte["columnas"]]
    filas = []
    for fila in lector:
        registro = {}
        for clave, posicion in zip(claves, range(len(cabecera))):
            registro[clave] = fila[posicion] if posicion < len(fila) else ""
        filas.append(registro)
    for registro in filas:
        for clave, valor in registro.items():
            arreglado = reparar_mojibake(valor)
            if arreglado is not None:
                registro[clave] = arreglado
    return filas


def main() -> int:
    parser = argparse.ArgumentParser(description="Auditoría de los CSV de la SSC")
    parser.add_argument("--json", action="store_true", help="imprime el informe completo")
    argumentos = parser.parse_args()

    reportes: dict[str, dict] = {}
    for clave, definicion in RECURSOS.items():
        reporte = auditar_archivo(clave, definicion)
        reportes[clave] = reporte
        if "error" in reporte:
            print(f"[{clave}] {reporte['error']}", file=sys.stderr)
            continue
        print(
            f"[{clave}] {reporte['archivo']} · {reporte['filas']} filas · "
            f"{reporte['folios_unicos']} folios · {reporte['fecha_min']}→{reporte['fecha_max']} · "
            f"{reporte['columnas_totales']} columnas · duplicados de cabecera: "
            f"{reporte['encabezados_duplicados'] or 'ninguno'}"
        )

    principal = reportes.get("hechos")
    comparaciones = {}
    if principal and "error" not in principal:
        filas_principales = filas_dict(principal)
        lesionadas, inv_lesionadas = sumar_columna(filas_principales, "personas_lesionadas")
        fallecidas, inv_fallecidas = sumar_columna(filas_principales, "personas_fallecidas")
        con_fallecidos = sum(
            1 for fila in filas_principales
            if fila.get("personas_fallecidas", "").strip().isdigit()
            and int(fila["personas_fallecidas"]) > 0
        )
        comparaciones["hechos"] = {
            "personas_lesionadas": lesionadas,
            "personas_fallecidas": fallecidas,
            "hechos_con_fallecimientos": con_fallecidos,
            "celdas_no_numericas": {"lesionadas": inv_lesionadas, "fallecidas": inv_fallecidas},
        }
        folios_principales = set(fila["folio"].strip() for fila in filas_principales)

        for clave in ("sexo", "edad", "usuario", "vehiculos"):
            reporte = reportes.get(clave)
            if not reporte or "error" in reporte:
                continue
            filas = filas_dict(reporte)
            folios = set(fila["folio"].strip() for fila in filas)
            detalle = {
                "filas": len(filas),
                "folios": len(folios),
                "folios_fuera_del_principal": len(folios - folios_principales),
                "folios_del_principal_sin_fila": len(folios_principales - folios),
                "cobertura_folios_principal": round(
                    100 * len(folios & folios_principales) / len(folios_principales), 2
                ),
            }
            if "total" in {col["clave"] for col in reporte["columnas"]}:
                total, invalidos = sumar_columna(filas, "total")
                detalle["suma_total"] = total
                detalle["total_no_numerico"] = invalidos
            if clave == "edad":
                edades = [fila.get("edad", "").strip() for fila in filas]
                validas = [e for e in edades if e.isdigit()]
                detalle["edad_min"] = min((int(e) for e in validas), default=None)
                detalle["edad_max"] = max((int(e) for e in validas), default=None)
                detalle["edad_vacia"] = sum(1 for e in edades if e == "")
                detalle["edad_sin_informacion"] = sum(
                    1 for e in edades if e == "" or not e.isdigit()
                )
            if clave == "sexo":
                detalle["condiciones"] = Counter(
                    fila.get("condicion_persona", "").strip() for fila in filas
                ).most_common()
            if clave == "usuario":
                detalle["consecuencias"] = Counter(
                    fila.get("condicion_persona", "").strip() for fila in filas
                ).most_common()
                detalle["tipos_usuario"] = Counter(
                    fila.get("condicion_persona__2", "").strip() for fila in filas
                ).most_common()
            if clave == "vehiculos":
                detalle["tipos_vehiculo"] = Counter(
                    fila.get("tipo_vehiculo", "").strip() for fila in filas
                ).most_common(15)
                detalle["numeros_vehiculo"] = Counter(
                    fila.get("no_vehiculo", "").strip() for fila in filas
                ).most_common(6)
            comparaciones[clave] = detalle

    informe = {
        "generado": datetime.now().isoformat(timespec="seconds"),
        "entrada": str(ENTRADA),
        "recursos": {
            clave: {k: v for k, v in reporte.items() if not k.startswith("_")}
            for clave, reporte in reportes.items()
        },
        "comparaciones": comparaciones,
    }

    SALIDA.mkdir(parents=True, exist_ok=True)
    destino = SALIDA / "auditoria-fuentes.json"
    destino.write_text(json.dumps(informe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nInforme: {destino}")
    print(json.dumps(comparaciones, ensure_ascii=False, indent=2, default=str))
    if argumentos.json:
        print(json.dumps(informe, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
