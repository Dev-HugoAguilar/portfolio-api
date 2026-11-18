"""Ingesta del conjunto GA4: validar, construir agregados y publicar por versiones.

El procedimiento es el mismo patrón que ya usa el portafolio para la CNBV y el
Atlas vial:

1. se toma un **cerrojo** en MongoDB para que no haya dos ingestas a la vez;
2. se resuelve el **acceso a BigQuery** (o a una exportación local documentada);
3. se **inspecciona el esquema real** y se valida antes de calcular nada;
4. se ejecutan las consultas agregadas; los usuarios se guardan **hasheados**;
5. se validan coherencias (orden del embudo, compras ≤ compradores, ingresos solo
   de compras, cohortes no negativas);
6. se publica de forma **atómica**: los documentos llevan la versión y el puntero
   `marketing_estado` cambia en una sola operación;
7. si algo falla, **se conserva la versión publicada anterior** y el intento queda
   registrado con su motivo.

Nunca se publican cifras calculadas sobre un esquema incompleto ni sobre datos
que no vengan de la fuente documentada.
"""

from __future__ import annotations

import hashlib
import re
import csv
import gzip
import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Iterator

from pymongo.database import Database
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.marketing import cliente as acceso
from app.marketing import consultas, esquema
from app.db.mongo import (
    MARKETING_ARTICULOS,
    MARKETING_BLOQUEOS,
    MARKETING_CATEGORIAS,
    MARKETING_COHORTES,
    MARKETING_COMPRAS,
    MARKETING_EMBUDO,
    MARKETING_ESTADO,
    MARKETING_EVENTOS,
    MARKETING_IMPORTACIONES,
    MARKETING_RESUMEN,
    MARKETING_SESIONES,
    MARKETING_USUARIOS,
)

logger = logging.getLogger(__name__)

ESTADO_ACTIVO = "activo"
NOMBRE_BLOQUEO = "ingesta-marketing"
BLOQUEO_MIN = 60

#: Prefijo del modelo de datos. Cambiar la forma de los documentos exige subir esta
#: versión: la API y la ingesta comparten el mismo modelo.
MODELO = "mk-v2"

COLECCIONES_DATOS = [
    MARKETING_RESUMEN,
    MARKETING_USUARIOS,
    MARKETING_SESIONES,
    MARKETING_COMPRAS,
    MARKETING_ARTICULOS,
    MARKETING_EMBUDO,
    MARKETING_COHORTES,
    MARKETING_CATEGORIAS,
    MARKETING_EVENTOS,
]


@dataclass
class Resultado:
    """Resultado de una sincronización, apto para imprimir como JSON."""

    exito: bool = False
    version: str | None = None
    publicada: bool = False
    sin_cambios: bool = False
    motivo: str = ""
    validacion: dict[str, Any] = field(default_factory=dict)
    cifras: dict[str, Any] = field(default_factory=dict)
    duracion_s: float = 0.0

    def publico(self) -> dict[str, Any]:
        return {
            "exito": self.exito,
            "version": self.version,
            "publicada": self.publicada,
            "sin_cambios": self.sin_cambios,
            "motivo": self.motivo,
            "validacion": self.validacion,
            "cifras": self.cifras,
            "duracion_s": round(self.duracion_s, 2),
        }


# --------------------------------------------------------------------- cerrojo
def adquirir_bloqueo(db: Database, minutos: int = BLOQUEO_MIN) -> str | None:
    """Reserva exclusiva de la ingesta; ``None`` si ya hay otra en marcha.

    Es una sola operación atómica: se actualiza el documento solo si el cerrojo
    anterior caducó; si el índice único rechaza el ``upsert`` es porque otra
    ingesta lo tiene tomado.
    """
    token = uuid.uuid4().hex
    ahora = datetime.now(timezone.utc)
    expira = ahora + timedelta(minutes=minutos)
    try:
        documento = db[MARKETING_BLOQUEOS].find_one_and_update(
            {
                "nombre": NOMBRE_BLOQUEO,
                "$or": [
                    {"expira_en": {"$lt": ahora}},
                    {"expira_en": None},
                    {"expira_en": {"$exists": False}},
                ],
            },
            {
                "$set": {
                    "nombre": NOMBRE_BLOQUEO,
                    "token": token,
                    "tomado_en": ahora,
                    "expira_en": expira,
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return None
    if documento and documento.get("token") == token:
        return token
    return None


def liberar_bloqueo(db: Database, token: str) -> None:
    db[MARKETING_BLOQUEOS].delete_one({"nombre": NOMBRE_BLOQUEO, "token": token})


# ---------------------------------------------------------------------- estado
def version_activa(db: Database) -> dict[str, Any] | None:
    estado = db[MARKETING_ESTADO].find_one({"_id": ESTADO_ACTIVO})
    if not estado:
        return None
    return db[MARKETING_IMPORTACIONES].find_one({"version": estado.get("version")}, {"_id": 0})


def estado_publico(db: Database) -> dict[str, Any]:
    """Lo que la API muestra: si hay datos, de cuándo son y de dónde salieron.

    No toca Google Cloud: responde con lo que ya está guardado en MongoDB (Atlas),
    así el arranque, los filtros y los diagnósticos públicos funcionan sin
    credenciales de Google ni acceso a BigQuery.
    """
    activa = version_activa(db)
    ultimo = db[MARKETING_IMPORTACIONES].find_one(
        {},
        {"_id": 0, "version": 1, "estado": 1, "creado_en": 1, "motivo": 1},
        sort=[("creado_en", -1)],
    )
    ajustes = get_settings()

    base = {
        "fuente": {
            "conjunto": esquema.TABLAS,
            "documentacion": esquema.DOCUMENTACION,
            "esquema": esquema.ESQUEMA_OFICIAL,
            "licencia": esquema.LICENCIA,
            "condicion": esquema.CONDICION,
            "periodo_documentado": esquema.PERIODO_DOCUMENTADO,
        },
        "persistencia": {
            "motor": "mongodb",
            "servicio": _servicio(ajustes),
            "host": _host_efectivo(ajustes),
            "base": ajustes.marketing_mongodb_db or ajustes.mongodb_db,
            "tls": _uri_efectiva(ajustes).startswith("mongodb+srv"),
            "uri_propia_de_marketing": bool(ajustes.marketing_mongodb_uri),
            "conexion": _conexion(db),
            "extraccion": {
                "proveedor": "Google BigQuery (Sandbox)",
                "cuando": "solo durante la preparación de una versión",
                "requiere_credenciales_en_la_api": False,
            },
        },
        "acceso_bigquery": {
            "disponible": None,
            "via": None,
            "motivo": (
                "La extracción se ejecuta aparte (scripts.importar_marketing) y no "
                "forma parte del funcionamiento del tablero."
            ),
            "detalle": {},
            "remedio": [
                "gcloud auth login",
                "gcloud config set project TU_PROYECTO",
                r".venv\Scripts\python.exe -m scripts.importar_marketing --extraer",
            ],
        },
        "ultimo_intento": ultimo,
    }

    if not activa:
        base.update(
            {
                "datos_disponibles": False,
                "version": None,
                "ultima_sincronizacion": None,
                "periodo": esquema.PERIODO_DOCUMENTADO,
                "cifras": None,
                "capacidades": None,
                "motivo": (
                    "No hay una versión publicada en MongoDB. Carga una copia "
                    "original con `scripts.importar_marketing --desde-local <carpeta>` "
                    "o extrae una nueva; el tablero no muestra cifras hasta que exista "
                    "una versión válida."
                ),
            }
        )
        return base

    muestra = (
        activa.get("muestra")
        or (activa.get("cifras") or {}).get("muestra")
        or {"semilla": None, "porcentaje": 0.0, "unidad": "usuario", "usuarios": None}
    )
    base.update(
        {
            "datos_disponibles": True,
            "version": activa.get("version"),
            "modelo": activa.get("modelo"),
            "ultima_sincronizacion": activa.get("publicado_en"),
            "periodo": activa.get("periodo"),
            "cifras": activa.get("cifras"),
            "capacidades": activa.get("capacidades"),
            "validacion": activa.get("validacion"),
            "esquema": activa.get("esquema"),
            "manifiesto": activa.get("manifiesto"),
            "muestra": muestra,
            "cobertura_muestra": cobertura_muestra(muestra),
            "documentos": activa.get("documentos"),
            "motivo": "",
        }
    )
    return base


def cobertura_muestra(muestra: dict[str, Any] | None) -> dict[str, Any]:
    """Población original, muestra publicada y método de selección.

    La población sale de la extracción **sin muestreo** del mismo periodo
    (``esquema.POBLACION_USUARIOS``, contada en la copia original) y no vive en
    Atlas: lo que se publica es la muestra. La proporción se calcula con las dos
    cifras, no se copia del manifiesto.
    """
    muestra = muestra or {}
    usuarios = muestra.get("usuarios")
    porcentaje = float(muestra.get("porcentaje") or 0.0)
    semilla = muestra.get("semilla")
    proporcion = (
        round(100 * usuarios / esquema.POBLACION_USUARIOS, 3)
        if usuarios and esquema.POBLACION_USUARIOS
        else None
    )
    umbral = int(round(porcentaje * 100)) if porcentaje else None
    return {
        "poblacion_original": {
            "usuarios": esquema.POBLACION_USUARIOS,
            "filas_usuario_dia": esquema.POBLACION_FILAS_USUARIO_DIA,
            "periodo": esquema.PERIODO_DOCUMENTADO,
            "origen": esquema.POBLACION_ORIGEN,
            "nota": (
                "Usuarios distintos del conjunto en el periodo, contados en la "
                "extracción sin muestreo. No se guardan en Atlas: el tablero "
                "publica la muestra."
            ),
        },
        "muestra_publicada": {
            "usuarios": usuarios,
            "porcentaje_declarado": porcentaje,
            "porcentaje_efectivo": proporcion,
            "unidad": muestra.get("unidad") or "usuario",
            "semilla": semilla,
        },
        "metodo": {
            "seleccion": "usuario",
            "predicado": (
                esquema.PREDICADO_MUESTRA.format(
                    semilla=semilla or "«semilla»", umbral=umbral or "«umbral»"
                )
                if semilla
                else None
            ),
            "reproducible": True,
            "explicacion": (
                "Se eligen personas con un hash determinista y después se agregan "
                "TODAS sus sesiones, compras y eventos: agrupar no es muestrear. "
                "La misma semilla devuelve la misma muestra."
            ),
        },
        "alcance": {
            "cifras_describen": "la muestra publicada",
            "no_extrapolable": True,
            "verificacion": (
                "La conciliación cubre la muestra completa (92 días, todos los "
                "canales) y, por separado, cortes filtrados por fecha, país y "
                "dispositivo."
            ),
        },
    }


# ------------------------------------------------------------------ validación
def _uri_efectiva(ajustes: Any) -> str:
    """URI con la que el tablero lee: la propia de marketing si existe."""
    return ajustes.marketing_mongodb_uri or ajustes.mongodb_uri


def _servicio(ajustes: Any) -> str:
    return "MongoDB Atlas" if _uri_efectiva(ajustes).startswith("mongodb+srv") else "MongoDB"


def _host_efectivo(ajustes: Any) -> str:
    """Host sin credenciales de la URI que usa el tablero."""
    uri = _uri_efectiva(ajustes)
    sin_esquema = uri.split("://", 1)[1] if "://" in uri else uri
    resto = sin_esquema.split("@", 1)[1] if "@" in sin_esquema else sin_esquema
    return resto.split("/")[0].split(",")[0].split("?")[0]


#: Último diagnóstico de conexión por capas (se reutiliza durante un minuto).
_DIAGNOSTICO_CONEXION: dict[str, Any] = {}


def _conexion(db: Database) -> dict[str, Any]:
    """Estado de la conexión: solo diagnostica por capas si el `ping` falla."""
    import time

    ahora = time.time()
    if _DIAGNOSTICO_CONEXION.get("expira", 0) > ahora:
        return _DIAGNOSTICO_CONEXION["valor"]
    try:
        db.command("ping")
        valor = {"ok": True, "mensaje": "Conexión y autenticación correctas."}
    except Exception:  # noqa: BLE001 - se informa el detalle por capas
        from app.config import get_settings
        from app.db.diagnostico_mongo import diagnosticar

        ajustes = get_settings()
        valor = diagnosticar(
            _uri_efectiva(ajustes),
            ajustes.marketing_mongodb_db or ajustes.mongodb_db,
            comprobar_permisos=False,
        )
    _DIAGNOSTICO_CONEXION["valor"] = valor
    _DIAGNOSTICO_CONEXION["expira"] = ahora + 60
    return valor


def validar(datos: dict[str, list[dict[str, Any]]], capacidades: dict[str, Any]) -> dict[str, Any]:
    """Coherencias mínimas antes de publicar. Un error impide publicar."""
    errores: list[str] = []
    avisos: list[str] = []

    resumen = datos["resumen"]
    embudo = datos["embudo"]
    cohortes = datos["cohortes"]
    usuarios = datos["usuarios"]

    if not resumen:
        errores.append("La consulta de resumen no devolvió filas.")
    if not usuarios:
        errores.append("La consulta de usuarios no devolvió filas.")
    if not embudo:
        errores.append("La consulta del embudo no devolvió filas.")

    eventos = sum(int(fila.get("eventos") or 0) for fila in resumen)
    compras_filas = datos.get("compras") or []
    if compras_filas:
        compras = len(compras_filas)
        compradores = len(
            {
                str(fila.get("user_pseudo_id"))
                for fila in compras_filas
                if fila.get("user_pseudo_id")
            }
        )
        ingresos = sum(float(fila.get("ingresos_usd") or 0) for fila in compras_filas)
        sin_importe = sum(
            1 for fila in compras_filas if not float(fila.get("ingresos_usd") or 0)
        )
    else:
        # Exportaciones anteriores que traían las compras agregadas por día.
        compras = sum(int(fila.get("compras") or 0) for fila in resumen)
        compradores = sum(int(fila.get("compradores") or 0) for fila in resumen)
        ingresos = sum(float(fila.get("ingresos_usd") or 0) for fila in resumen)
        sin_importe = sum(int(fila.get("compras_sin_importe") or 0) for fila in resumen)

    if compradores > compras:
        errores.append(
            f"Hay más compradores ({compradores}) que compras ({compras}): la "
            "deduplicación de compras está mal."
        )
    usuarios_distintos = len(
        {str(fila.get("user_pseudo_id")) for fila in usuarios if fila.get("user_pseudo_id")}
    )
    if compradores > usuarios_distintos:
        errores.append(
            f"Hay más compradores ({compradores}) que usuarios ({usuarios_distintos}): "
            "los compradores son un subconjunto de los usuarios."
        )
    if ingresos > 0 and compras == 0:
        errores.append("Hay ingresos sin compras: los ingresos deben venir solo de compras.")
    if sin_importe:
        avisos.append(
            f"{sin_importe} compras no traen importe; los ingresos las excluyen y así se informa."
        )

    sin_sesion = sum(int(fila.get("eventos_sin_sesion") or 0) for fila in resumen)
    if eventos and sin_sesion / eventos > 0.05:
        avisos.append(
            f"{sin_sesion} de {eventos} eventos no traen «ga_session_id» "
            f"({100 * sin_sesion / eventos:.1f} %): las métricas de sesión excluyen esos eventos."
        )

    # Embudo: debe ser monótono decreciente y con la misma unidad.
    por_paso: dict[int, int] = {}
    for fila in embudo:
        paso = int(fila.get("paso") or 0)
        por_paso[paso] = por_paso.get(paso, 0) + int(fila.get("sesiones") or 0)
    pasos = sorted(por_paso)
    for anterior, siguiente in zip(pasos, pasos[1:]):
        if por_paso[siguiente] > por_paso[anterior]:
            errores.append(
                f"El embudo crece del paso {anterior} ({por_paso[anterior]}) al "
                f"{siguiente} ({por_paso[siguiente]}): el orden o la ventana temporal "
                "no se están aplicando."
            )
    if pasos and pasos[0] != 1:
        errores.append("El embudo no empieza en el paso 1.")

    # Cohortes: índices no negativos y compradores acotados por usuarios.
    for fila in cohortes:
        if int(fila.get("indice") or 0) < 0:
            errores.append("Hay índices de semana negativos en las cohortes.")
            break
        if int(fila.get("compradores") or 0) > int(fila.get("usuarios") or 0):
            errores.append("En una celda de cohorte hay más compradores que usuarios.")
            break

    fechas = sorted({str(fila.get("fecha")) for fila in resumen if fila.get("fecha")})
    periodo = {"desde": fechas[0] if fechas else None, "hasta": fechas[-1] if fechas else None}
    documentado = esquema.PERIODO_DOCUMENTADO
    if periodo["desde"] and periodo["desde"] < documentado["desde"]:
        avisos.append(f"El conjunto trae fechas anteriores al periodo documentado ({periodo['desde']}).")
    if periodo["hasta"] and periodo["hasta"] > documentado["hasta"]:
        avisos.append(f"El conjunto trae fechas posteriores al periodo documentado ({periodo['hasta']}).")

    if not capacidades.get("categoria_producto") and datos.get("categorias"):
        avisos.append("Se recibieron categorías aunque el esquema no las declara: se ignoran.")

    return {
        "valida": not errores,
        "errores": errores,
        "avisos": avisos,
        "periodo": periodo,
        "cifras": {
            "eventos": eventos,
            "compras": compras,
            "compradores": compradores,
            "ingresos_usd": round(ingresos, 2),
            "compras_sin_importe": sin_importe,
            "eventos_sin_sesion": sin_sesion,
            "filas_resumen": len(resumen),
            "filas_usuarios": len(usuarios),
            "filas_compras": len(compras_filas),
            "filas_embudo": len(embudo),
            "filas_cohortes": len(cohortes),
            "sesiones_paso_1": por_paso.get(1, 0),
            "sesiones_paso_final": por_paso.get(max(pasos), 0) if pasos else 0,
        },
    }


# ----------------------------------------------------------- construcción
def agregados_vacios() -> dict[str, Any]:
    """Acumuladores que se llenan mientras se leen las consultas."""
    return {
        "filas_resumen": 0,
        "filas_usuarios": 0,
        "filas_compras": 0,
        "filas_sesiones": 0,
        "filas_articulos": 0,
        "filas_embudo": 0,
        "filas_cohortes": 0,
        "eventos": 0,
        "compras": 0,
        "compradores": 0,
        "usuarios_compradores": set(),
        "ingresos_usd": 0.0,
        "compras_sin_importe": 0,
        "eventos_sin_sesion": 0,
        "por_paso": {},
        "fechas": set(),
        "indices_negativos": 0,
        "compradores_mayores": 0,
        # Sesiones reconstruidas: la escalera de pasos debe ser monótona.
        "sesiones_pasos": [0, 0, 0, 0],
        "sesiones_sin_inicio": 0,
        "sesiones_compro": 0,
        "sesiones_dispositivo_distinto": 0,
        "sesiones_varios_dispositivos": 0,
        "articulos_por_compra": {},
        "unidades": 0,
        "ingresos_linea_usd": 0.0,
        "articulos_sin_categoria": 0,
    }


def acumular_sesion(agregados: dict[str, Any], fila: dict[str, Any]) -> None:
    agregados["filas_sesiones"] += 1
    pasos = [fila.get("t1"), fila.get("t2"), fila.get("t3"), fila.get("t4")]
    # No todas las sesiones ven un artículo: lo que no puede faltar es el inicio.
    if not fila.get("inicio"):
        agregados["sesiones_sin_inicio"] += 1
    for indice, paso in enumerate(pasos):
        if paso:
            agregados["sesiones_pasos"][indice] += 1
    if pasos[3]:
        agregados["sesiones_compro"] += 1
    dispositivo_sesion = esquema.normalizar(fila.get("dispositivo_sesion"))
    if dispositivo_sesion != esquema.normalizar(fila.get("dispositivo")):
        agregados["sesiones_dispositivo_distinto"] += 1
    if _entero(fila.get("dispositivos_distintos")) > 1:
        agregados["sesiones_varios_dispositivos"] += 1
    fecha = _fecha(fila.get("fecha"))
    if fecha:
        agregados["fechas"].add(fecha)


def acumular_articulo(agregados: dict[str, Any], fila: dict[str, Any]) -> None:
    agregados["filas_articulos"] += 1
    clave = str(fila.get("clave_compra") or "")
    if clave:
        agregados["articulos_por_compra"][clave] = (
            agregados["articulos_por_compra"].get(clave, 0) + 1
        )
    agregados["unidades"] += _entero(fila.get("unidades"))
    agregados["ingresos_linea_usd"] += _decimal(fila.get("ingresos_linea_usd"))
    if esquema.normalizar(fila.get("categoria")) == esquema.SIN_INFORMACION:
        agregados["articulos_sin_categoria"] += 1


def acumular_resumen(agregados: dict[str, Any], fila: dict[str, Any]) -> None:
    agregados["filas_resumen"] += 1
    agregados["eventos"] += _entero(fila.get("eventos"))
    agregados["eventos_sin_sesion"] += _entero(fila.get("eventos_sin_sesion"))
    fecha = _fecha(fila.get("fecha"))
    if fecha:
        agregados["fechas"].add(fecha)


def acumular_compra(agregados: dict[str, Any], fila: dict[str, Any]) -> None:
    """Cada fila es ya una compra deduplicada: solo hay que sumar y contar gente."""
    agregados["filas_compras"] += 1
    agregados["compras"] += 1
    ingresos = _decimal(fila.get("ingresos_usd"))
    agregados["ingresos_usd"] += ingresos
    if ingresos <= 0:
        agregados["compras_sin_importe"] += 1
    usuario = fila.get("user_pseudo_id")
    if usuario:
        agregados["usuarios_compradores"].add(str(usuario))
    fecha = _fecha(fila.get("fecha"))
    if fecha:
        agregados["fechas"].add(fecha)


def acumular_embudo(agregados: dict[str, Any], fila: dict[str, Any]) -> None:
    agregados["filas_embudo"] += 1
    paso = _entero(fila.get("paso"))
    agregados["por_paso"][paso] = agregados["por_paso"].get(paso, 0) + _entero(fila.get("sesiones"))


def acumular_cohorte(agregados: dict[str, Any], fila: dict[str, Any]) -> None:
    agregados["filas_cohortes"] += 1
    if _entero(fila.get("indice")) < 0:
        agregados["indices_negativos"] += 1
    if _entero(fila.get("compradores")) > _entero(fila.get("usuarios")):
        agregados["compradores_mayores"] += 1


def validar_agregados(
    agregados: dict[str, Any], capacidades: dict[str, Any]
) -> dict[str, Any]:
    """Coherencias mínimas antes de publicar, sobre los acumuladores."""
    errores: list[str] = []
    avisos: list[str] = []

    if not agregados["filas_resumen"]:
        errores.append("La consulta de resumen no devolvió filas.")
    if not agregados["filas_usuarios"]:
        errores.append("La consulta de usuarios no devolvió filas.")
    if not agregados["filas_embudo"]:
        errores.append("La consulta del embudo no devolvió filas.")

    eventos = agregados["eventos"]
    compras = agregados["compras"]
    compradores = len(agregados["usuarios_compradores"])
    agregados["compradores"] = compradores
    ingresos = agregados["ingresos_usd"]

    if compradores > compras:
        errores.append(
            f"Hay más compradores ({compradores}) que compras ({compras}): la "
            "deduplicación de compras está mal."
        )
    if ingresos > 0 and compras == 0:
        errores.append("Hay ingresos sin compras: los ingresos deben venir solo de compras.")
    if agregados["compras_sin_importe"]:
        avisos.append(
            f"{agregados['compras_sin_importe']} compras no traen importe; los ingresos "
            "las excluyen y así se informa."
        )

    sin_sesion = agregados["eventos_sin_sesion"]
    if eventos and sin_sesion / eventos > 0.05:
        avisos.append(
            f"{sin_sesion} de {eventos} eventos no traen «ga_session_id» "
            f"({100 * sin_sesion / eventos:.1f} %): las métricas de sesión excluyen esos eventos."
        )

    por_paso = dict(sorted(agregados["por_paso"].items()))
    for anterior, siguiente in zip(por_paso, list(por_paso)[1:]):
        if por_paso[siguiente] > por_paso[anterior]:
            errores.append(
                f"El embudo crece del paso {anterior} ({por_paso[anterior]}) al "
                f"{siguiente} ({por_paso[siguiente]}): el orden o la ventana temporal "
                "no se están aplicando."
            )
    if por_paso and next(iter(por_paso)) != 1:
        errores.append("El embudo no empieza en el paso 1.")

    if agregados["indices_negativos"]:
        errores.append("Hay índices de semana negativos en las cohortes.")
    if agregados["compradores_mayores"]:
        errores.append("En alguna celda de cohorte hay más compradores que usuarios.")

    # Sesiones reconstruidas: la escalera de pasos no puede crecer hacia abajo y
    # quien compra tiene que haber registrado los pasos anteriores en su sesión.
    escalera = list(agregados["sesiones_pasos"])
    for anterior, siguiente in zip(escalera, escalera[1:]):
        if siguiente > anterior:
            errores.append(
                "El embudo por sesión crece en algún paso "
                f"({escalera}): los pasos no se están exigiendo en orden."
            )
            break
    if agregados["sesiones_compro"] > escalera[0]:
        errores.append(
            "Hay más sesiones con compra que sesiones con el primer paso: el embudo "
            "se está armando con sesiones distintas."
        )
    if not agregados["filas_sesiones"]:
        errores.append("La consulta de sesiones no devolvió filas.")
    if agregados["sesiones_sin_inicio"]:
        errores.append("Hay sesiones reconstruidas sin marca de inicio.")

    # Artículos: cada línea pertenece a una compra deduplicada.
    compras_con_articulos = len(agregados["articulos_por_compra"])
    if compras_con_articulos > compras:
        errores.append(
            f"Hay artículos de {compras_con_articulos} compras distintas cuando solo "
            f"hay {compras} compras: las líneas no corresponden a las compras."
        )
    if agregados["sesiones_dispositivo_distinto"]:
        avisos.append(
            f"{agregados['sesiones_dispositivo_distinto']} sesiones usaron un dispositivo "
            "distinto del de adquisición del usuario; se atribuyen al de adquisición y "
            "así se informa."
        )
    if agregados["sesiones_varios_dispositivos"]:
        avisos.append(
            f"{agregados['sesiones_varios_dispositivos']} sesiones registraron más de un "
            "dispositivo en sus eventos."
        )
    if agregados["articulos_sin_categoria"]:
        avisos.append(
            f"{agregados['articulos_sin_categoria']} líneas de artículo no traen "
            "categoría; se conservan como «Sin información»."
        )

    fechas = sorted(agregados["fechas"])
    periodo = {"desde": fechas[0] if fechas else None, "hasta": fechas[-1] if fechas else None}
    documentado = esquema.PERIODO_DOCUMENTADO
    if periodo["desde"] and periodo["desde"] < documentado["desde"]:
        avisos.append(f"El conjunto trae fechas anteriores al periodo documentado ({periodo['desde']}).")
    if periodo["hasta"] and periodo["hasta"] > documentado["hasta"]:
        avisos.append(f"El conjunto trae fechas posteriores al periodo documentado ({periodo['hasta']}).")

    return {
        "valida": not errores,
        "errores": errores,
        "avisos": avisos,
        "periodo": periodo,
        "cifras": {
            "eventos": eventos,
            "compras": compras,
            "compradores": compradores,
            "ingresos_usd": round(ingresos, 2),
            "compras_sin_importe": agregados["compras_sin_importe"],
            "eventos_sin_sesion": sin_sesion,
            "filas_resumen": agregados["filas_resumen"],
            "filas_usuarios": agregados["filas_usuarios"],
            "filas_compras": agregados["filas_compras"],
            "filas_sesiones": agregados["filas_sesiones"],
            "filas_articulos": agregados["filas_articulos"],
            "filas_embudo": agregados["filas_embudo"],
            "filas_cohortes": agregados["filas_cohortes"],
            "sesiones_paso_1": por_paso.get(1, 0),
            "sesiones_paso_final": por_paso[max(por_paso)] if por_paso else 0,
            "sesiones_por_paso": escalera,
            "sesiones_con_compra": agregados["sesiones_compro"],
            "sesiones_dispositivo_distinto": agregados["sesiones_dispositivo_distinto"],
            "sesiones_varios_dispositivos": agregados["sesiones_varios_dispositivos"],
            "articulos": agregados["filas_articulos"],
            "unidades": agregados["unidades"],
            "ingresos_linea_usd": round(agregados["ingresos_linea_usd"], 2),
        },
    }


def _hash_usuario(valor: str, sal: str) -> str:
    """Hash con sal por versión: permite contar distintos sin guardar el id."""
    return hashlib.sha256(f"{sal}:{valor}".encode("utf-8")).hexdigest()[:32]


def _canal(fuente: Any, medio: Any) -> str:
    """Canal legible y filtrable: «fuente / medio» con los marcadores agrupados."""
    return f"{esquema.normalizar(fuente)} / {esquema.normalizar(medio)}"


def _fecha(valor: Any) -> str | None:
    if valor is None:
        return None
    if hasattr(valor, "isoformat"):
        return valor.isoformat()[:10]
    texto = str(valor).strip()
    if len(texto) == 8 and texto.isdigit():
        return f"{texto[0:4]}-{texto[4:6]}-{texto[6:8]}"
    return texto[:10] or None


def _marca(valor: Any) -> str | None:
    """Marca de tiempo ISO 8601 en UTC a partir de los microsegundos de GA4.

    Se conserva la precisión de la fuente (microsegundos) y se fija la zona en
    UTC, así el orden lexicográfico coincide con el orden temporal y las
    diferencias entre pasos se calculan sin ambigüedad.
    """
    if valor is None or valor == "":
        return None
    try:
        micros = int(float(valor))
    except (TypeError, ValueError):
        texto = str(valor).strip()
        return texto[:32] or None
    if micros <= 0:
        return None
    momento = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
    return momento.isoformat(timespec="microseconds")


def _segundos(desde: Any, hasta: Any) -> float | None:
    """Duración en segundos entre dos marcas ISO (``None`` si falta alguna)."""
    try:
        inicio = datetime.fromisoformat(str(desde))
        fin = datetime.fromisoformat(str(hasta))
    except (TypeError, ValueError):
        return None
    return round((fin - inicio).total_seconds(), 3)


def _entero(valor: Any) -> int:
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return 0


def _entero_o_nulo(valor: Any) -> int | None:
    """Entero o ``None`` cuando la fuente no trae el campo."""
    if valor is None or valor == "":
        return None
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return None


def _decimal_o_nulo(valor: Any) -> float | None:
    """Decimal o ``None`` cuando la fuente no trae el campo."""
    if valor is None or valor == "":
        return None
    try:
        return round(float(valor), 2)
    except (TypeError, ValueError):
        return None


def _decimal(valor: Any) -> float:
    try:
        return round(float(valor), 2)
    except (TypeError, ValueError):
        return 0.0


def construir_documentos(
    crudos: dict[str, list[dict[str, Any]]], version: str
) -> dict[str, list[dict[str, Any]]]:
    """Normaliza las filas de BigQuery a documentos de MongoDB."""
    sal = version
    documentos: dict[str, list[dict[str, Any]]] = {clave: [] for clave in COLECCIONES_DATOS}

    for fila in crudos["resumen"]:
        documentos[MARKETING_RESUMEN].append(
            {
                "version": version,
                "fecha": _fecha(fila.get("fecha")),
                "dispositivo": esquema.normalizar(fila.get("dispositivo")),
                "pais": esquema.normalizar(fila.get("pais")),
                "region": esquema.normalizar(fila.get("region")),
                "fuente": esquema.normalizar(fila.get("fuente")),
                "medio": esquema.normalizar(fila.get("medio")),
                "canal": _canal(fila.get("fuente"), fila.get("medio")),
                "eventos": _entero(fila.get("eventos")),
                "usuarios": _entero(fila.get("usuarios")),
                "sesiones": _entero(fila.get("sesiones")),
                "eventos_sin_sesion": _entero(fila.get("eventos_sin_sesion")),
            }
        )

    for fila in crudos.get("compras", []):
        identificador = fila.get("user_pseudo_id")
        if not identificador:
            continue
        documentos[MARKETING_COMPRAS].append(
            {
                "version": version,
                "fecha": _fecha(fila.get("fecha")),
                "usuario": _hash_usuario(str(identificador), sal),
                "dispositivo": esquema.normalizar(fila.get("dispositivo")),
                "pais": esquema.normalizar(fila.get("pais")),
                "region": esquema.normalizar(fila.get("region")),
                "fuente": esquema.normalizar(fila.get("fuente")),
                "medio": esquema.normalizar(fila.get("medio")),
                "canal": _canal(fila.get("fuente"), fila.get("medio")),
                "ingresos_usd": _decimal(fila.get("ingresos_usd")),
            }
        )

    for fila in crudos.get("usuarios", []):
        identificador = fila.get("user_pseudo_id")
        if not identificador:
            continue
        documentos[MARKETING_USUARIOS].append(
            {
                "version": version,
                "fecha": _fecha(fila.get("fecha")),
                "dispositivo": esquema.normalizar(fila.get("dispositivo")),
                "pais": esquema.normalizar(fila.get("pais")),
                "region": esquema.normalizar(fila.get("region")),
                "fuente": esquema.normalizar(fila.get("fuente")),
                "medio": esquema.normalizar(fila.get("medio")),
                "canal": _canal(fila.get("fuente"), fila.get("medio")),
                # Nunca se guarda el identificador: solo un hash con sal.
                "usuario": _hash_usuario(str(identificador), sal),
                "compro": 1 if _entero(fila.get("compro")) else 0,
            }
        )

    for fila in crudos.get("embudo", []):
        paso = _entero(fila.get("paso"))
        etiqueta = next(
            (item["etiqueta"] for item in esquema.EMBUDO if esquema.EMBUDO.index(item) + 1 == paso),
            f"Paso {paso}",
        )
        documentos[MARKETING_EMBUDO].append(
            {
                "version": version,
                "fecha": _fecha(fila.get("fecha")),
                "dispositivo": esquema.normalizar(fila.get("dispositivo")),
                "pais": esquema.normalizar(fila.get("pais")),
                "fuente": esquema.normalizar(fila.get("fuente")),
                "medio": esquema.normalizar(fila.get("medio")),
                "canal": _canal(fila.get("fuente"), fila.get("medio")),
                "paso": paso,
                "etiqueta": etiqueta,
                "unidad": esquema.UNIDAD_EMBUDO,
                "sesiones": _entero(fila.get("sesiones")),
                "sesiones_evento": _entero(fila.get("sesiones_evento")),
                "eventos": _entero(fila.get("eventos")),
            }
        )

    for fila in crudos.get("cohortes", []):
        documentos[MARKETING_COHORTES].append(
            {
                "version": version,
                "cohorte": _fecha(fila.get("cohorte")),
                "indice": _entero(fila.get("indice")),
                "dimension": str(fila.get("dimension") or ""),
                "valor": esquema.normalizar(fila.get("valor")),
                "usuarios": _entero(fila.get("usuarios")),
                "compradores": _entero(fila.get("compradores")),
            }
        )

    for fila in crudos.get("categorias", []):
        documentos[MARKETING_CATEGORIAS].append(
            {
                "version": version,
                "categoria": esquema.normalizar(fila.get("categoria")),
                "dispositivo": esquema.normalizar(fila.get("dispositivo")),
                "pais": esquema.normalizar(fila.get("pais")),
                "fuente": esquema.normalizar(fila.get("fuente")),
                "medio": esquema.normalizar(fila.get("medio")),
                "canal": _canal(fila.get("fuente"), fila.get("medio")),
                "eventos": _entero(fila.get("eventos")),
                "compras": _entero(fila.get("compras")),
            }
        )

    for indice, fila in enumerate(crudos.get("eventos", [])):
        fecha = _fecha(fila.get("fecha"))
        nombre = str(fila.get("event_name") or "")
        documentos[MARKETING_EVENTOS].append(
            {
                "version": version,
                # Identificador de fila para la tabla y la exportación: no
                # identifica a ninguna persona.
                "id": f"{fecha or 'sf'}-{indice:07d}",
                "fecha": fecha,
                "hora": _entero(fila.get("hora")) if fila.get("hora") is not None else None,
                "evento": nombre,
                "evento_etiqueta": next(
                    (item["etiqueta"] for item in esquema.EMBUDO if item["clave"] == nombre), nombre
                ),
                "dispositivo": esquema.normalizar(fila.get("dispositivo")),
                "pais": esquema.normalizar(fila.get("pais")),
                "fuente": esquema.normalizar(fila.get("fuente")),
                "medio": esquema.normalizar(fila.get("medio")),
                "canal": _canal(fila.get("fuente"), fila.get("medio")),
                "articulo": esquema.normalizar(fila.get("articulo")),
                "categoria": esquema.normalizar(fila.get("categoria")),
                "ingresos_usd": _decimal(fila.get("ingresos_usd")) if nombre == "purchase" else 0.0,
            }
        )

    return documentos


# ------------------------------------------------------------------ consultas
def sufijo_tabla(valor: str | Any) -> str:
    """Convierte una fecha (ISO o ``AAAAMMDD``) al sufijo de tabla de BigQuery.

    Las consultas filtran con ``_TABLE_SUFFIX``, que en este conjunto es
    ``AAAAMMDD`` (por ejemplo ``20201101``). La configuración y el periodo
    documentado se expresan en ISO, así que hay que normalizar aquí, en un solo
    sitio, para que ninguna consulta use un sufijo con guiones.
    """
    texto = str(valor or "").strip()
    limpio = texto.replace("-", "").replace("/", "")
    if len(limpio) == 8 and limpio.isdigit():
        return limpio
    raise ValueError(f"Fecha no utilizable como sufijo de tabla: {valor!r}")


# ------------------------------------------- copia original y manifiesto
#: Entidades que se extraen y se cargan, en el orden en que se procesan.
ENTIDADES = [
    "resumen",
    "usuarios",
    "sesiones",
    "compras",
    "articulos",
    "embudo",
    "cohortes",
    "categorias",
    "eventos",
]

#: Transformaciones aplicadas a la copia original. Se escriben en el manifiesto.
TRANSFORMACIONES = [
    "Fechas de evento convertidas de AAAAMMDD a ISO 8601 (AAAA-MM-DD), sin cambiar el día.",
    "Marcas de tiempo en microsegundos convertidas a ISO 8601 en UTC, con la precisión de la fuente.",
    "Identificadores personales sustituidos por hash SHA-256 con sal por versión (32 caracteres).",
    "Valores de marcador de la fuente («(not set)», «(none)», vacíos…) llevados a «Sin información»; no se convierten en tráfico directo.",
    "Fuente y medio normalizados y canal calculado como «fuente / medio».",
    "Importes en USD conservados tal cual; las compras sin importe se cuentan aparte y no se rellenan con cero.",
    "Compras deduplicadas por usuario + transacción (respaldo: usuario + marca de tiempo) sobre todo el periodo.",
    "Una misma compra vista en dos días se atribuye al primer día observado.",
    "Sesiones reconstruidas por usuario + ga_session_id; los pasos del embudo guardan su primera marca de tiempo.",
    "Líneas de artículo tomadas del primer evento de compra de cada transacción, sin repartir el ingreso de la compra.",
    "Dimensiones de persona (dispositivo, país, región, fuente, medio) asignadas por la primera actividad observada del usuario.",
]

#: Archivos por entidad dentro de la carpeta de una extracción.
ARCHIVO = "{}.csv.gz"


def carpeta_nueva(base: Path) -> Path:
    """Carpeta de una extracción nueva, ordenable por fecha y sin colisiones."""
    marca = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    carpeta = base / f"extraccion-{marca}-{uuid.uuid4().hex[:6]}"
    carpeta.mkdir(parents=True, exist_ok=False)
    return carpeta


def _sha256(ruta: Path) -> str:
    digest = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1 << 20), b""):
            digest.update(bloque)
    return digest.hexdigest()


def _archivar(
    nombre: str, filas: Iterator[dict[str, Any]], carpeta: Path, registro: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """Escribe la fila cruda en ``CSV.gz`` y la devuelve para cargarla.

    La copia original se guarda **antes** de cualquier transformación: si algo
    falla, se puede recargar desde el archivo sin volver a consultar BigQuery.
    """
    ruta = carpeta / ARCHIVO.format(nombre)
    cuenta = 0
    with gzip.open(ruta, "wt", encoding="utf-8", newline="") as archivo:
        escritor: csv.DictWriter | None = None
        for fila in filas:
            if escritor is None:
                escritor = csv.DictWriter(archivo, fieldnames=list(fila.keys()))
                escritor.writeheader()
            escritor.writerow(fila)
            cuenta += 1
            yield fila
    registro[nombre] = {
        "archivo": ruta.name,
        "filas": cuenta,
        "bytes": ruta.stat().st_size,
        "sha256": _sha256(ruta),
    }


def inventario(carpeta: Path) -> dict[str, Any]:
    """Conteos y hashes de la copia original, para el manifiesto.

    Se calculan al cargar desde disco: así el manifiesto de una versión cargada
    sin volver a consultar BigQuery sigue documentando qué archivos la produjeron.
    """
    archivos: dict[str, Any] = {}
    conteos: dict[str, int] = {}
    for nombre in ENTIDADES:
        ruta = carpeta / ARCHIVO.format(nombre)
        if not ruta.exists():
            continue
        filas = 0
        with gzip.open(ruta, "rt", encoding="utf-8", newline="") as archivo:
            lector = csv.reader(archivo)
            next(lector, None)
            for _ in lector:
                filas += 1
        archivos[nombre] = {
            "archivo": ruta.name,
            "filas": filas,
            "bytes": ruta.stat().st_size,
            "sha256": _sha256(ruta),
        }
        conteos[nombre] = filas
    return {"archivos": archivos, "conteos": conteos}


def _leer_archivo(nombre: str, carpeta: Path) -> Iterator[dict[str, Any]]:
    """Relée la copia original (carga reanudable, sin tocar BigQuery)."""
    ruta = carpeta / ARCHIVO.format(nombre)
    if not ruta.exists():
        raise FileNotFoundError(f"Falta la copia original de «{nombre}»: {ruta}")
    with gzip.open(ruta, "rt", encoding="utf-8", newline="") as archivo:
        for fila in csv.DictReader(archivo):
            yield dict(fila)


# ----------------------------------------------------------------- publicación
def _publicar(
    db: Database, version: str, documentos: dict[str, list[dict[str, Any]]], tamano: int = 5_000
) -> int:
    """Inserta los documentos de una versión y devuelve el total insertado."""
    insertados = 0
    for coleccion, filas in documentos.items():
        if not filas:
            continue
        db[coleccion].delete_many({"version": version})
        for inicio in range(0, len(filas), tamano):
            lote = filas[inicio : inicio + tamano]
            if lote:
                db[coleccion].insert_many(lote)
                insertados += len(lote)
    return insertados


def _registrar_fallo(db: Database, version: str | None, motivo: str, validacion: dict | None) -> None:
    marca = version or f"fallida-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    db[MARKETING_IMPORTACIONES].update_one(
        {"version": marca},
        {
            "$set": {
                "version": marca,
                "estado": "rechazada",
                "motivo": motivo,
                "validacion": validacion or {},
                "creado_en": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


#: Tamaño de lote al insertar en MongoDB.
LOTE = 4_000
#: Hilos de escritura en paralelo (la latencia domina en un clúster remoto).
HILOS_ESCRITURA = 4


def _iterar(fuente: Any, sql: str) -> Iterator[dict[str, Any]]:
    """Itera el resultado de una consulta, con o sin soporte de streaming."""
    iterador = getattr(fuente, "iterar", None)
    if callable(iterador):
        return iterador(sql)
    return iter(fuente.ejecutar(sql))


def _documento_sesion(fila: dict[str, Any], version: str) -> dict[str, Any] | None:
    identificador = fila.get("user_pseudo_id")
    clave = fila.get("clave_sesion")
    if not identificador or not clave:
        return None
    pasos = [
        _marca(fila.get("t1")),
        _marca(fila.get("t2")),
        _marca(fila.get("t3")),
        _marca(fila.get("t4")),
    ]
    inicio = _marca(fila.get("inicio"))
    fin = _marca(fila.get("fin"))
    return {
        "version": version,
        # Hash de la clave de sesión: identifica la sesión sin guardar el id real.
        "sesion": _hash_usuario(str(clave), version),
        "usuario": _hash_usuario(str(identificador), version),
        "fecha": _fecha(fila.get("fecha")),
        "dispositivo": esquema.normalizar(fila.get("dispositivo")),
        "pais": esquema.normalizar(fila.get("pais")),
        "region": esquema.normalizar(fila.get("region")),
        "fuente": esquema.normalizar(fila.get("fuente")),
        "medio": esquema.normalizar(fila.get("medio")),
        "canal": _canal(fila.get("fuente"), fila.get("medio")),
        "inicio": inicio,
        "fin": fin,
        "duracion_s": _segundos(inicio, fin),
        "eventos": _entero(fila.get("eventos")),
        # Dispositivo observado en la sesión frente al de adquisición del usuario:
        # si difieren, la sesión se atribuye al de adquisición y se cuenta aparte.
        "dispositivo_sesion": esquema.normalizar(fila.get("dispositivo_sesion")),
        "dispositivos_distintos": _entero(fila.get("dispositivos_distintos")),
        # Arreglo de longitud fija: la primera vez que ocurrió cada paso del embudo
        # (4 marcas, una por paso) o None. No crece con el número de eventos.
        "pasos": pasos,
        "compro": 1 if pasos[-1] else 0,
        "ingresos_usd": _decimal(fila.get("ingresos_usd")),
    }


def _documento_articulo(fila: dict[str, Any], version: str) -> dict[str, Any] | None:
    clave = fila.get("clave_compra")
    identificador = fila.get("user_pseudo_id")
    if not clave or not identificador:
        return None
    return {
        "version": version,
        "compra": _hash_usuario(str(clave), version),
        "usuario": _hash_usuario(str(identificador), version),
        "fecha": _fecha(fila.get("fecha")),
        "dispositivo": esquema.normalizar(fila.get("dispositivo")),
        "pais": esquema.normalizar(fila.get("pais")),
        "fuente": esquema.normalizar(fila.get("fuente")),
        "medio": esquema.normalizar(fila.get("medio")),
        "canal": _canal(fila.get("fuente"), fila.get("medio")),
        "articulo": esquema.normalizar(fila.get("articulo")),
        "categoria": esquema.normalizar(fila.get("categoria")),
        "unidades": _entero(fila.get("unidades")),
        "precio_unitario_usd": _decimal(fila.get("precio_unitario_usd")),
        "ingresos_linea_usd": _decimal(fila.get("ingresos_linea_usd")),
    }


def _documento_resumen(fila: dict[str, Any], version: str) -> dict[str, Any]:
    fuente = esquema.normalizar(fila.get("fuente"))
    medio = esquema.normalizar(fila.get("medio"))
    return {
        "version": version,
        "fecha": _fecha(fila.get("fecha")),
        "dispositivo": esquema.normalizar(fila.get("dispositivo")),
        "pais": esquema.normalizar(fila.get("pais")),
        "region": esquema.normalizar(fila.get("region")),
        "fuente": fuente,
        "medio": medio,
        "canal": _canal(fila.get("fuente"), fila.get("medio")),
        "eventos": _entero(fila.get("eventos")),
        "usuarios": _entero(fila.get("usuarios")),
        "sesiones": _entero(fila.get("sesiones")),
        "eventos_sin_sesion": _entero(fila.get("eventos_sin_sesion")),
    }


def _documento_compra(fila: dict[str, Any], version: str) -> dict[str, Any] | None:
    identificador = fila.get("user_pseudo_id")
    if not identificador:
        return None
    return {
        "version": version,
        "fecha": _fecha(fila.get("fecha")),
        # Igual que en usuarios: solo el hash con sal, nunca el identificador.
        "usuario": _hash_usuario(str(identificador), version),
        "dispositivo": esquema.normalizar(fila.get("dispositivo")),
        "pais": esquema.normalizar(fila.get("pais")),
        "region": esquema.normalizar(fila.get("region")),
        "fuente": esquema.normalizar(fila.get("fuente")),
        "medio": esquema.normalizar(fila.get("medio")),
        "canal": _canal(fila.get("fuente"), fila.get("medio")),
        "ingresos_usd": _decimal(fila.get("ingresos_usd")),
    }


def _documento_usuario(fila: dict[str, Any], version: str) -> dict[str, Any] | None:
    identificador = fila.get("user_pseudo_id")
    if not identificador:
        return None
    fuente = esquema.normalizar(fila.get("fuente"))
    medio = esquema.normalizar(fila.get("medio"))
    return {
        "version": version,
        "fecha": _fecha(fila.get("fecha")),
        "dispositivo": esquema.normalizar(fila.get("dispositivo")),
        "pais": esquema.normalizar(fila.get("pais")),
        "region": esquema.normalizar(fila.get("region")),
        "fuente": fuente,
        "medio": medio,
        "canal": _canal(fila.get("fuente"), fila.get("medio")),
        # Nunca se guarda el identificador: solo un hash con sal por versión.
        "usuario": _hash_usuario(str(identificador), version),
        "compro": 1 if _entero(fila.get("compro")) else 0,
    }


def _documento_embudo(fila: dict[str, Any], version: str) -> dict[str, Any]:
    paso = _entero(fila.get("paso"))
    indice = paso - 1
    etiqueta = (
        esquema.EMBUDO[indice]["etiqueta"] if 0 <= indice < len(esquema.EMBUDO) else f"Paso {paso}"
    )
    return {
        "version": version,
        "fecha": _fecha(fila.get("fecha")),
        "dispositivo": esquema.normalizar(fila.get("dispositivo")),
        "pais": esquema.normalizar(fila.get("pais")),
        "fuente": esquema.normalizar(fila.get("fuente")),
        "medio": esquema.normalizar(fila.get("medio")),
        "canal": _canal(fila.get("fuente"), fila.get("medio")),
        "paso": paso,
        "etiqueta": etiqueta,
        "unidad": esquema.UNIDAD_EMBUDO,
        "sesiones": _entero(fila.get("sesiones")),
        "sesiones_evento": _entero(fila.get("sesiones_evento")),
        "eventos": _entero(fila.get("eventos")),
    }


def _documento_cohorte(fila: dict[str, Any], version: str) -> dict[str, Any]:
    return {
        "version": version,
        "cohorte": _fecha(fila.get("cohorte")),
        "indice": _entero(fila.get("indice")),
        "dimension": str(fila.get("dimension") or ""),
        "valor": esquema.normalizar(fila.get("valor")),
        "usuarios": _entero(fila.get("usuarios")),
        "compradores": _entero(fila.get("compradores")),
    }


def _documento_categoria(fila: dict[str, Any], version: str) -> dict[str, Any]:
    """Categoría de producto con sus medidas de línea.

    ``articulos``, ``unidades`` e ``ingresos_linea_usd`` pueden faltar en la
    fuente; en ese caso se guardan como ``None`` (no disponibles) y **no** como
    cero: un cero significa «medido y vale cero».
    """
    return {
        "version": version,
        "fecha": _fecha(fila.get("fecha")),
        "categoria": esquema.normalizar(fila.get("categoria")),
        "dispositivo": esquema.normalizar(fila.get("dispositivo")),
        "pais": esquema.normalizar(fila.get("pais")),
        "fuente": esquema.normalizar(fila.get("fuente")),
        "medio": esquema.normalizar(fila.get("medio")),
        "canal": _canal(fila.get("fuente"), fila.get("medio")),
        "eventos": _entero(fila.get("eventos")),
        "compras": _entero(fila.get("compras")),
        "articulos": _entero(fila.get("articulos")),
        "unidades": _entero_o_nulo(fila.get("unidades")),
        "ingresos_linea_usd": _decimal_o_nulo(fila.get("ingresos_linea_usd")),
    }


def _documento_evento(fila: dict[str, Any], version: str, indice: int) -> dict[str, Any]:
    fecha = _fecha(fila.get("fecha"))
    nombre = str(fila.get("event_name") or "")
    etiqueta = next(
        (paso["etiqueta"] for paso in esquema.EMBUDO if paso["clave"] == nombre), nombre
    )
    return {
        "version": version,
        "id": f"{fecha or 'sf'}-{indice:07d}",
        "fecha": fecha,
        "hora": _entero(fila.get("hora")) if fila.get("hora") not in (None, "") else None,
        "evento": nombre,
        "evento_etiqueta": etiqueta,
        "dispositivo": esquema.normalizar(fila.get("dispositivo")),
        "pais": esquema.normalizar(fila.get("pais")),
        "fuente": esquema.normalizar(fila.get("fuente")),
        "medio": esquema.normalizar(fila.get("medio")),
        "canal": _canal(fila.get("fuente"), fila.get("medio")),
        "articulo": esquema.normalizar(fila.get("articulo")),
        "categoria": esquema.normalizar(fila.get("categoria")),
        "ingresos_usd": _decimal(fila.get("ingresos_usd")) if nombre == "purchase" else 0.0,
    }


def _insertar(
    db: Database,
    coleccion: str,
    filas: Iterator[dict[str, Any]],
    constructor,
    version: str,
    acumulador=None,
) -> int:
    """Normaliza e inserta por lotes, sin cargar todo en memoria.

    Los lotes se escriben con varios hilos: sobre un clúster remoto (Atlas) la
    latencia de cada ``insert_many`` domina el tiempo y la escritura concurrente
    multiplica el caudal (medido ~3× con cuatro hilos). El cliente de PyMongo es
    seguro entre hilos y agrupa las conexiones.
    """
    lote: list[dict[str, Any]] = []
    insertados = 0
    contador = 0
    pendientes = []
    coleccion_db = db[coleccion]
    coleccion_db.delete_many({"version": version})

    def enviar(bloque: list[dict[str, Any]]):
        coleccion_db.insert_many(bloque, ordered=False)

    with ThreadPoolExecutor(max_workers=HILOS_ESCRITURA) as pool:
        for fila in filas:
            contador += 1
            if acumulador is not None:
                acumulador(fila)
            documento = (
                constructor(fila, version, contador)
                if constructor is _documento_evento
                else constructor(fila, version)
            )
            if documento is None:
                continue
            lote.append(documento)
            if len(lote) >= LOTE:
                pendientes.append(pool.submit(enviar, lote))
                insertados += len(lote)
                lote = []
                # Contrapresión: no se acumulan lotes en vuelo sin límite.
                if len(pendientes) >= HILOS_ESCRITURA * 3:
                    pendientes.pop(0).result()
        if lote:
            pendientes.append(pool.submit(enviar, lote))
            insertados += len(lote)
        for futuro in pendientes:
            futuro.result()
    return insertados



#: Constructor de documentos y acumulador de cada entidad.
ENTIDAD_A_DOCUMENTO = {
    "resumen": _documento_resumen,
    "usuarios": _documento_usuario,
    "sesiones": _documento_sesion,
    "compras": _documento_compra,
    "articulos": _documento_articulo,
    "embudo": _documento_embudo,
    "cohortes": _documento_cohorte,
    "categorias": _documento_categoria,
    "eventos": _documento_evento,
}

ENTIDAD_A_COLECCION = {
    "resumen": MARKETING_RESUMEN,
    "usuarios": MARKETING_USUARIOS,
    "sesiones": MARKETING_SESIONES,
    "compras": MARKETING_COMPRAS,
    "articulos": MARKETING_ARTICULOS,
    "embudo": MARKETING_EMBUDO,
    "cohortes": MARKETING_COHORTES,
    "categorias": MARKETING_CATEGORIAS,
    "eventos": MARKETING_EVENTOS,
}


def consultas_de(desde: str, hasta: str, por_dia: int) -> dict[str, str]:
    """SQL de cada entidad, en un solo lugar (se guarda su hash en el manifiesto)."""
    return {
        "resumen": consultas.sql_resumen_dias(desde, hasta),
        "usuarios": consultas.sql_usuarios(desde, hasta),
        "sesiones": consultas.sql_sesiones(desde, hasta),
        "compras": consultas.sql_compras(desde, hasta),
        "articulos": consultas.sql_articulos(desde, hasta),
        "embudo": consultas.sql_embudo(desde, hasta),
        "cohortes": consultas.sql_cohortes(desde, hasta),
        "categorias": consultas.sql_categorias(desde, hasta),
        "eventos": consultas.sql_eventos(desde, hasta, por_dia),
    }


def _acumulador(nombre: str):
    return {
        "resumen": acumular_resumen,
        "sesiones": acumular_sesion,
        "compras": acumular_compra,
        "articulos": acumular_articulo,
        "embudo": acumular_embudo,
        "cohortes": acumular_cohorte,
    }.get(nombre)


def _escribir_manifiesto(carpeta: Path, manifiesto: dict[str, Any]) -> Path:
    ruta = carpeta / "manifiesto.json"
    ruta.write_text(
        json.dumps(manifiesto, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return ruta


def extraer(
    fuente: Any,
    desde: str,
    hasta: str,
    carpeta: Path,
    por_dia: int,
    reanudar: bool = False,
) -> dict[str, Any]:
    """Extrae todas las entidades a ``carpeta`` **sin** escribir en MongoDB.

    Deja la copia original (``CSV.gz`` por entidad) y el registro de conteos y
    hashes. Es la etapa que habla con BigQuery; se puede repetir o reutilizar.
    Con ``reanudar`` no vuelve a consultar las entidades que ya tienen archivo:
    una extracción interrumpida continúa donde se quedó.
    """
    registro: dict[str, Any] = {}
    conteos: dict[str, int] = {}
    consultas_hash: dict[str, str] = {}
    desde = sufijo_tabla(desde)
    hasta = sufijo_tabla(hasta)
    for nombre, sql in consultas_de(desde, hasta, por_dia).items():
        consultas_hash[nombre] = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        ruta = carpeta / ARCHIVO.format(nombre)
        if reanudar and ruta.exists() and ruta.stat().st_size > 0:
            filas = 0
            with gzip.open(ruta, "rt", encoding="utf-8", newline="") as archivo:
                lector = csv.reader(archivo)
                next(lector, None)
                for _ in lector:
                    filas += 1
            if filas:
                registro[nombre] = {
                    "archivo": ruta.name,
                    "filas": filas,
                    "bytes": ruta.stat().st_size,
                    "sha256": _sha256(ruta),
                    "reanudado": True,
                }
                conteos[nombre] = filas
                logger.info("Marketing: %s ya estaba extraído (%s filas)", nombre, filas)
                continue
            ruta.unlink(missing_ok=True)
        for _ in _archivar(nombre, _iterar(fuente, sql), carpeta, registro):
            pass
        conteos[nombre] = registro[nombre]["filas"]
        logger.info("Marketing: %s extraído (%s filas)", nombre, conteos[nombre])
    return {"archivos": registro, "conteos": conteos, "consultas": consultas_hash}


def sincronizar(
    db: Database,
    *,
    forzar: bool = False,
    fuente: Any | None = None,
    desde_local: Path | None = None,
    carpeta: Path | None = None,
    solo_extraer: bool = False,
    reanudar: bool = False,
    muestra_porcentaje: float | None = None,
    muestra_semilla: str | None = None,
) -> Resultado:
    """Ciclo completo: acceso → esquema → extracción → copia original → Atlas.

    ``desde_local`` carga una copia original ya extraída (no consulta BigQuery);
    ``solo_extraer`` deja la copia preparada y no publica ninguna versión.
    """
    inicio = monotonic()
    ajustes = get_settings()
    resultado = Resultado()

    desde_iso = ajustes.marketing_desde or esquema.PERIODO_DOCUMENTADO["desde"]
    hasta_iso = ajustes.marketing_hasta or esquema.PERIODO_DOCUMENTADO["hasta"]
    por_dia = ajustes.marketing_eventos_por_dia
    consultas.fijar_muestra(
        muestra_semilla or ajustes.marketing_muestra_semilla,
        ajustes.marketing_muestra_porcentaje if muestra_porcentaje is None else muestra_porcentaje,
    )

    if solo_extraer:
        # La extracción solo escribe la copia original: no necesita MongoDB, así
        # que tampoco puede fallar porque la base no esté disponible.
        try:
            origen = fuente or acceso.resolver(ajustes.marketing_gcp_project)
            revision = esquema.validar(acceso.leer_esquema(origen))
            resultado.validacion = revision
            if not revision["valida"]:
                resultado.motivo = "El esquema real no cumple lo imprescindible; no se extrae nada."
                resultado.duracion_s = monotonic() - inicio
                return resultado
            destino = Path(carpeta) if carpeta else carpeta_nueva(ajustes.marketing_dir())
            destino.mkdir(parents=True, exist_ok=True)
            extraccion = extraer(origen, desde_iso, hasta_iso, destino, por_dia, reanudar)
            resultado.exito = True
            resultado.motivo = (
                f"Copia original extraída en {destino} "
                f"({sum(extraccion['conteos'].values())} filas)."
            )
            resultado.cifras = {"carpeta": str(destino), **extraccion["conteos"]}
        except acceso.SinAccesoBigQuery as error:
            resultado.motivo = error.motivo
            resultado.validacion = {"acceso": error.publico()}
        resultado.duracion_s = monotonic() - inicio
        return resultado

    token = adquirir_bloqueo(db)
    if token is None:
        resultado.motivo = "Ya hay una ingesta en marcha; no se ejecuta una segunda."
        resultado.duracion_s = monotonic() - inicio
        return resultado

    temporal = f"tmp-{uuid.uuid4().hex[:12]}"
    try:
        capacidades: dict[str, Any] = {
            "adquisicion": True,
            "conversion": True,
            "cohortes": True,
            "ingresos": True,
            "categoria_producto": True,
            "ciudad": False,
            "sistema_operativo": False,
        }
        esquema_detectado = esquema.Esquema(columnas={})
        desde = sufijo_tabla(desde_iso)
        hasta = sufijo_tabla(hasta_iso)

        try:
            if desde_local is not None:
                carpeta = Path(desde_local)
                if not carpeta.is_dir():
                    raise FileNotFoundError(f"No existe la carpeta: {carpeta}")
                origen_nombre = f"copia original: {carpeta.name}"
                extraccion = inventario(carpeta)
            else:
                origen = fuente or acceso.resolver(ajustes.marketing_gcp_project)
                esquema_detectado = acceso.leer_esquema(origen)
                revision = esquema.validar(esquema_detectado)
                resultado.validacion = revision
                if not revision["valida"]:
                    resultado.motivo = (
                        "El esquema real no cumple lo imprescindible; no se publica nada."
                    )
                    _registrar_fallo(db, None, resultado.motivo, revision)
                    resultado.duracion_s = monotonic() - inicio
                    return resultado
                capacidades = revision["capacidades"]
                origen_nombre = origen.nombre
                carpeta = Path(carpeta) if carpeta else carpeta_nueva(ajustes.marketing_dir())
                extraccion = extraer(origen, desde, hasta, carpeta, por_dia, reanudar)

            agregados = agregados_vacios()
            for nombre in ENTIDADES:
                filas: Iterator[dict[str, Any]] = _leer_archivo(nombre, carpeta)
                acumulador = _acumulador(nombre)
                _insertar(
                    db,
                    ENTIDAD_A_COLECCION[nombre],
                    filas,
                    ENTIDAD_A_DOCUMENTO[nombre],
                    temporal,
                    (lambda fila, f=acumulador: f(agregados, fila)) if acumulador else None,
                )
            agregados["filas_usuarios"] = db[MARKETING_USUARIOS].count_documents(
                {"version": temporal}
            )

            validacion = validar_agregados(agregados, capacidades)
            resultado.validacion = validacion
            resultado.cifras = validacion["cifras"]

            if not validacion["valida"]:
                resultado.motivo = "La validación falló; se conserva la versión publicada."
                _registrar_fallo(db, None, resultado.motivo, validacion)
                for coleccion in COLECCIONES_DATOS:
                    db[coleccion].delete_many({"version": temporal})
                resultado.duracion_s = monotonic() - inicio
                return resultado

            huella = hashlib.sha256(
                json.dumps(
                    {
                        "modelo": MODELO,
                        "muestra": consultas.muestra(),
                        "cifras": validacion["cifras"],
                        "periodo": validacion["periodo"],
                        "esquema": sorted(esquema_detectado.columnas),
                        "origen": origen_nombre,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()[:10]
            version = f"ga4-{huella}"

            activa = version_activa(db)
            if activa and activa.get("version") == version and not forzar:
                for coleccion in COLECCIONES_DATOS:
                    db[coleccion].delete_many({"version": temporal})
                resultado.exito = True
                resultado.version = version
                resultado.sin_cambios = True
                resultado.motivo = "El contenido no cambió; se conserva la versión publicada."
                resultado.duracion_s = monotonic() - inicio
                return resultado

            insertados = 0
            documentos_por_coleccion: dict[str, int] = {}
            for coleccion in COLECCIONES_DATOS:
                db[coleccion].delete_many({"version": version})
                actualizados = db[coleccion].update_many(
                    {"version": temporal}, {"$set": {"version": version}}
                )
                insertados += actualizados.modified_count
                documentos_por_coleccion[coleccion] = db[coleccion].count_documents(
                    {"version": version}
                )

            usuarios_muestra = len(db[MARKETING_USUARIOS].distinct("usuario", {"version": version}))
            muestra_documentada = {
                **consultas.muestra(),
                "unidad": "usuario",
                "usuarios": usuarios_muestra,
            }
            ahora = datetime.now(timezone.utc)
            manifiesto = {
                "version": version,
                "modelo": MODELO,
                "fuente": {
                    "conjunto": esquema.TABLAS,
                    "documentacion": esquema.DOCUMENTACION,
                    "condicion": esquema.CONDICION,
                    "licencia": esquema.LICENCIA,
                },
                "periodo": validacion["periodo"],
                "periodo_solicitado": {"desde": desde_iso, "hasta": hasta_iso},
                "extraido_en": extraccion.get("extraido_en") or ahora.isoformat(),
                "cargado_en": ahora.isoformat(),
                "origen": origen_nombre,
                "consultas": extraccion.get("consultas", {}),
                "archivos": extraccion.get("archivos", {}),
                "conteos": extraccion.get("conteos", {}),
                "colecciones": documentos_por_coleccion,
                "esquema": esquema_detectado.resumen(),
                "muestra": muestra_documentada,
                "transformaciones": TRANSFORMACIONES,
                "validacion": {
                    "valida": validacion["valida"],
                    "errores": validacion["errores"],
                    "avisos": validacion["avisos"],
                    "cifras": validacion["cifras"],
                },
            }
            try:
                _escribir_manifiesto(carpeta, manifiesto)
                manifiesto_ruta = str(carpeta / "manifiesto.json")
            except OSError as error:  # pragma: no cover - disco no disponible
                logger.warning("No se pudo escribir el manifiesto: %s", error)
                manifiesto_ruta = None

            db[MARKETING_IMPORTACIONES].update_one(
                {"version": version},
                {
                    "$set": {
                        "version": version,
                        "modelo": MODELO,
                        "estado": "publicada",
                        "origen": origen_nombre,
                        "periodo": validacion["periodo"],
                        "cifras": {**validacion["cifras"], "muestra": muestra_documentada},
                        "muestra": muestra_documentada,
                        "capacidades": capacidades,
                        "validacion": {
                            "errores": validacion["errores"],
                            "avisos": validacion["avisos"],
                        },
                        "esquema": esquema_detectado.resumen(),
                        "manifiesto": manifiesto_ruta,
                        "archivos": extraccion.get("archivos", {}),
                        "documentos": documentos_por_coleccion,
                        "documentos_insertados": insertados,
                        "creado_en": ahora,
                        "publicado_en": ahora,
                    }
                },
                upsert=True,
            )
            db[MARKETING_ESTADO].update_one(
                {"_id": "activo"}, {"$set": {"version": version, "actualizado_en": ahora}}, upsert=True
            )

            resultado.exito = True
            resultado.version = version
            resultado.publicada = True
            resultado.motivo = f"Versión {version} publicada con {insertados} documentos."
            resultado.cifras = {**validacion["cifras"], "carpeta": str(carpeta)}
            resultado.duracion_s = monotonic() - inicio
            logger.info("Marketing: %s", resultado.motivo)
            return resultado

        except acceso.SinAccesoBigQuery as error:
            resultado.motivo = error.motivo
            resultado.validacion = {"acceso": error.publico()}
            _registrar_fallo(db, None, error.motivo, resultado.validacion)
            resultado.duracion_s = monotonic() - inicio
            return resultado
        except (FileNotFoundError, ValueError) as error:
            resultado.motivo = f"Copia original ilegible: {error}"
            _registrar_fallo(db, None, resultado.motivo, None)
            resultado.duracion_s = monotonic() - inicio
            return resultado
    finally:
        for coleccion in COLECCIONES_DATOS:
            db[coleccion].delete_many({"version": temporal})
        liberar_bloqueo(db, token)


