"""Lectura de los CSV de hechos de tránsito de la SSC.

Los cinco recursos del catálogo tienen **la misma granularidad comprobada**: una
fila por folio (30 655 filas y 30 655 folios únicos en cada archivo, año 2024).
Por eso se leen y se cruzan por ``folio`` sin riesgo de multiplicar hechos: la
relación es 1:1 y se verifica en cada importación.

Los recursos de personas y de vehículos describen, en cambio, **un registro por
hecho**: su columna ``total`` coincide folio a folio con ``personas_lesionadas``
del archivo principal, de modo que no son un desglose completo de todas las
víctimas. El parser conserva ese valor original en ``total_original`` y marca la
cobertura real de cada atributo.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from app.atlas import normalizacion as nz

#: Mapa de encabezados del recurso «por tipo de persona», que repite
#: ``condicion_persona``. La primera aparición es la consecuencia y la segunda el
#: tipo de usuario de la vía. Nunca se sobrescribe una columna: cada clave
#: original se conserva en ``_originales``.
CABECERAS_USUARIO = {
    "condicion_persona": ["consecuencia", "tipo_usuario"],
}

RECURSOS = ("hechos", "sexo", "edad", "usuario", "vehiculos")


@dataclass
class Tabla:
    """Una tabla leída de un CSV, con su encabezado ya resuelto."""

    ruta: Path
    cabecera: list[str]
    claves: list[str]
    filas: list[dict[str, Any]]
    duplicados: dict[str, int] = field(default_factory=dict)
    separador: str = ","


def _detectar_separador(cabecera: str) -> str:
    if cabecera.count(";") > cabecera.count(","):
        return ";"
    if cabecera.count("\t") > cabecera.count(","):
        return "\t"
    return ","


def _claves(cabecera: list[str], recurso: str) -> tuple[list[str], dict[str, int]]:
    """Claves únicas por columna, resolviendo los encabezados repetidos."""
    mapa = CABECERAS_USUARIO if recurso == "usuario" else {}
    vistos: dict[str, int] = {}
    claves: list[str] = []
    duplicados: dict[str, int] = {}
    for nombre in cabecera:
        vistos[nombre] = vistos.get(nombre, 0) + 1
        ocurrencia = vistos[nombre]
        if ocurrencia > 1:
            duplicados[nombre] = vistos[nombre]
        alias = mapa.get(nombre)
        if alias and ocurrencia - 1 < len(alias):
            claves.append(alias[ocurrencia - 1])
        else:
            claves.append(nombre if ocurrencia == 1 else f"{nombre}__{ocurrencia}")
    return claves, duplicados


def leer_tabla(ruta: Path, recurso: str) -> Tabla:
    """Lee el archivo completo (no una muestra) y repara el texto."""
    texto = ruta.read_text(encoding="utf-8-sig")
    primera = texto.split("\n", 1)[0]
    separador = _detectar_separador(primera)
    lector = csv.reader(io.StringIO(texto), delimiter=separador)
    cabecera = next(lector)
    claves, duplicados = _claves(cabecera, recurso)

    filas: list[dict[str, Any]] = []
    for valores in lector:
        if not valores or all(v.strip() == "" for v in valores):
            continue
        registro: dict[str, Any] = {}
        for indice, clave in enumerate(claves):
            valor = valores[indice] if indice < len(valores) else ""
            registro[clave] = nz.reparar_mojibake(valor).strip()
        filas.append(registro)

    return Tabla(ruta=ruta, cabecera=cabecera, claves=claves, filas=filas, duplicados=duplicados, separador=separador)


def _entero(valor: str) -> int:
    numero, ausente = nz.leer_entero(valor)
    return 0 if ausente or numero is None else numero


def construir_hechos(
    tablas: dict[str, Tabla],
    geometria: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Cruza los cinco recursos por folio (1:1) y devuelve los documentos.

    El resultado tiene **un documento por hecho**: no hay uniones que puedan
    duplicar hechos ni víctimas. ``geometria`` es un objeto con
    ``alcaldia_de(lon, lat)`` para comprobar la pertenencia espacial.
    """
    principales = tablas["hechos"].filas
    por_folio: dict[str, dict[str, Any]] = {}
    incidencias = {
        "folios_duplicados_hechos": 0,
        "complementarios_sin_hecho": 0,
        "hechos_sin_complementario": 0,
        "coordenadas": {"ok": 0, "ausente": 0, "no_numerica": 0, "fuera_de_rango": 0, "fuera_de_cdmx": 0},
        "alcaldia_texto_no_reconocida": 0,
        "alcaldia_geometria_no_encontrada": 0,
        "desajuste_alcaldia": 0,
        "total_distinto_de_lesionadas": 0,
        "edad_sin_informacion": 0,
        "tipo_usuario_sin_dato": 0,
        "sexo_sin_dato": 0,
    }

    for fila in principales:
        folio = fila.get("folio", "").strip()
        if not folio:
            continue
        if folio in por_folio:
            incidencias["folios_duplicados_hechos"] += 1
            continue

        fecha = nz.leer_fecha(fila.get("fecha_evento", ""))
        hora = nz.leer_hora(fila.get("hora_evento", ""))
        lat, lon, estado = nz.leer_coordenadas(fila.get("latitud", ""), fila.get("longitud", ""))
        incidencias["coordenadas"][estado] += 1

        alcaldia_texto = fila.get("alcaldia", "")
        alcaldia = nz.alcaldia_canonica(alcaldia_texto)
        if alcaldia is None:
            incidencias["alcaldia_texto_no_reconocida"] += 1

        alcaldia_geo = None
        if geometria is not None and lat is not None and lon is not None and estado == "ok":
            alcaldia_geo = geometria.alcaldia_de(lon, lat)
            if alcaldia_geo is None:
                incidencias["alcaldia_geometria_no_encontrada"] += 1
            elif alcaldia and alcaldia_geo != alcaldia:
                incidencias["desajuste_alcaldia"] += 1

        lesionadas = _entero(fila.get("personas_lesionadas", ""))
        fallecidas = _entero(fila.get("personas_fallecidas", ""))

        documento = {
            "folio": folio,
            "fecha": fecha.isoformat() if fecha else None,
            # Mongo necesita datetime, no date: medianoche UTC del día del evento.
            "fecha_dt": datetime.combine(fecha, time(0, 0)) if fecha else None,
            "anio": fecha.year if fecha else None,
            "mes": fecha.month if fecha else None,
            "dia": fecha.day if fecha else None,
            "dia_semana": fecha.weekday() if fecha else None,
            "hora": hora,
            "tipo_evento": fila.get("tipo_evento", ""),
            "tipo_evento_norm": nz.tipo_evento(fila.get("tipo_evento", "")),
            "alcaldia_texto": alcaldia_texto,
            "alcaldia": alcaldia,
            "alcaldia_geometria": alcaldia_geo,
            "colonia": fila.get("colonia", ""),
            "punto_1": fila.get("punto_1", ""),
            "punto_2": fila.get("punto_2", ""),
            "zona_vial": fila.get("zona_vial", ""),
            "sector": fila.get("sector", ""),
            "tipo_interseccion": fila.get("tipo_de_interseccion", ""),
            "tipo_interseccion_norm": nz.interseccion(fila.get("tipo_de_interseccion", "")),
            "semaforizada": nz.si_no(fila.get("interseccion_semaforizada", "")),
            "vialidad": fila.get("clasificacion_de_la_vialidad", ""),
            "vialidad_norm": nz.vialidad(fila.get("clasificacion_de_la_vialidad", "")),
            "sentido": fila.get("sentido_de_circulacion", ""),
            "prioridad": fila.get("prioridad", ""),
            "origen": fila.get("origen", ""),
            "origen_norm": nz.origen(fila.get("origen", "")),
            "unidad_a_cargo": fila.get("unidad_a_cargo", ""),
            "trasladado_lesionados": nz.si_no(fila.get("trasladado_lesionados", "")),
            "unidad_medica": fila.get("unidad_medica_de_apoyo", ""),
            "matricula_unidad_medica": fila.get("matricula_unidad_medica", ""),
            "personas_lesionadas": lesionadas,
            "personas_fallecidas": fallecidas,
            "personas_totales": lesionadas + fallecidas,
            "lat": lat,
            "lon": lon,
            "coord_estado": estado,
            "ubicacion": {"type": "Point", "coordinates": [lon, lat]} if estado == "ok" else None,
        }
        por_folio[folio] = documento

    # --- recursos de personas y vehículos: un registro por folio ------------
    def cruzar(recurso: str, aplicar) -> None:
        if recurso not in tablas:
            return
        vistos: set[str] = set()
        for fila in tablas[recurso].filas:
            folio = fila.get("folio", "").strip()
            documento = por_folio.get(folio)
            if documento is None:
                incidencias["complementarios_sin_hecho"] += 1
                continue
            if folio in vistos:
                # Un segundo registro por folio invalidaría la relación 1:1.
                incidencias.setdefault("folios_con_varias_filas", {})[recurso] = (
                    incidencias.get("folios_con_varias_filas", {}).get(recurso, 0) + 1
                )
                continue
            vistos.add(folio)
            aplicar(documento, fila)

    def aplicar_sexo(documento: dict[str, Any], fila: dict[str, Any]) -> None:
        valor = nz.sexo(fila.get("sexo", ""))
        if valor in (None, "Sin dato"):
            incidencias["sexo_sin_dato"] += 1
        documento["personas_sexo"] = valor
        documento["personas_consecuencia"] = nz.consecuencia(fila.get("condicion_persona", ""))
        documento["personas_sexo_total"] = _entero(fila.get("total", ""))
        if documento["personas_sexo_total"] != documento["personas_lesionadas"]:
            incidencias["total_distinto_de_lesionadas"] += 1

    def aplicar_edad(documento: dict[str, Any], fila: dict[str, Any]) -> None:
        edad, texto = nz.edad_valor(fila.get("edad", ""))
        documento["personas_edad"] = edad
        documento["personas_edad_texto"] = texto
        if edad is None:
            incidencias["edad_sin_informacion"] += 1

    def aplicar_usuario(documento: dict[str, Any], fila: dict[str, Any]) -> None:
        valor = nz.tipo_usuario(fila.get("tipo_usuario", ""))
        if valor in (None, "Sin dato"):
            incidencias["tipo_usuario_sin_dato"] += 1
        documento["personas_tipo_usuario"] = valor

    def aplicar_vehiculo(documento: dict[str, Any], fila: dict[str, Any]) -> None:
        documento["vehiculo_tipo"] = nz.tipo_vehiculo(fila.get("tipo_vehiculo", ""))
        documento["vehiculo_numero"] = fila.get("no_vehiculo", "")

    cruzar("sexo", aplicar_sexo)
    cruzar("edad", aplicar_edad)
    cruzar("usuario", aplicar_usuario)
    cruzar("vehiculos", aplicar_vehiculo)

    documentos = list(por_folio.values())
    incidencias["hechos_sin_complementario"] = sum(
        1 for documento in documentos if "personas_sexo" not in documento
    )

    # Orden estable por fecha y hora: la tabla y la exportación lo aprovechan.
    documentos.sort(key=lambda d: (d["fecha"] or "", d["hora"] if d["hora"] is not None else 99, d["folio"]))
    return documentos, incidencias


def resumen_legible(hechos: list[dict[str, Any]], incidencias: dict[str, Any]) -> dict[str, Any]:
    """Cifras de control que se guardan con la importación y se muestran en la API."""
    fechas = [hecho["fecha"] for hecho in hechos if hecho["fecha"]]
    con_coordenadas = sum(1 for hecho in hechos if hecho["coord_estado"] == "ok")
    return {
        "hechos": len(hechos),
        "folios_unicos": len({hecho["folio"] for hecho in hechos}),
        "periodo": {"desde": min(fechas) if fechas else None, "hasta": max(fechas) if fechas else None},
        "personas_lesionadas": sum(hecho["personas_lesionadas"] for hecho in hechos),
        "personas_fallecidas": sum(hecho["personas_fallecidas"] for hecho in hechos),
        "hechos_con_fallecimientos": sum(1 for hecho in hechos if hecho["personas_fallecidas"] > 0),
        "hechos_sin_lesionados_ni_fallecidos": sum(1 for hecho in hechos if hecho["personas_totales"] == 0),
        "con_coordenadas": con_coordenadas,
        "sin_coordenadas": len(hechos) - con_coordenadas,
        "alcaldias_distintas": len({hecho["alcaldia"] for hecho in hechos if hecho["alcaldia"]}),
        "incidencias": incidencias,
    }
