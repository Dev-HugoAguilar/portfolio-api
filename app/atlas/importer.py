"""Ingesta de los hechos de tránsito de la SSC: descarga, validación y publicación.

Garantías que implementa este módulo:

* **Una sola ejecución a la vez** (cerrojo con caducidad en ``atlas_bloqueos``).
* **Descarga condicional** con ``ETag``/``Last-Modified`` y **SHA-256** de cada
  archivo, para detectar cambios de contenido aunque los metadatos no cambien.
* **Zona temporal**: se descarga y procesa en ``data/atlas/tmp/<uuid>/`` y solo
  al final se mueve a ``data/atlas/versiones/<version>/``.
* **Publicación atómica**: los documentos se escriben con la nueva ``version`` y
  el puntero ``atlas_estado`` se cambia al final en una sola operación; si algo
  falla antes, la versión anterior sigue siendo la que sirve la API.
* **Idempotencia**: si el contenido no cambió (misma huella), no se reescribe
  nada; volver a ejecutar el proceso deja el mismo estado.
* **Última versión válida**: cualquier error de descarga o de validación se
  registra y no toca la versión publicada.
* **Trazabilidad**: cada ejecución queda en ``atlas_importaciones`` con los
  metadatos del catálogo, los hashes, el informe de validación y los errores.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.atlas import parser as atlas_parser
from app.atlas import sources
from app.atlas.geometry import obtener_geometria
from app.config import get_settings
from app.db.mongo import ATLAS_BLOQUEOS, ATLAS_ESTADO, ATLAS_HECHOS, ATLAS_IMPORTACIONES
from app.db.indexes import ensure_indexes

logger = logging.getLogger(__name__)

NOMBRE_BLOQUEO = "atlas_vial"
ESTADO_ACTIVO = "activo"

#: Columnas mínimas que deben existir en cada recurso.
COLUMNAS_ESPERADAS: dict[str, tuple[str, ...]] = {
    "hechos": (
        "fecha_evento", "hora_evento", "tipo_evento", "folio", "latitud", "longitud",
        "alcaldia", "personas_lesionadas", "personas_fallecidas",
    ),
    "sexo": ("folio", "sexo", "condicion_persona", "total"),
    "edad": ("folio", "sexo", "condicion_persona", "edad"),
    "usuario": ("folio", "consecuencia", "tipo_usuario", "total"),
    "vehiculos": ("folio", "tipo_vehiculo"),
}


@dataclass
class Resultado:
    """Resumen de una ejecución de sincronización."""

    exito: bool
    version: str | None = None
    publicada: bool = False
    sin_cambios: bool = False
    motivo: str | None = None
    validacion: dict[str, Any] = field(default_factory=dict)
    recursos: dict[str, Any] = field(default_factory=dict)
    duracion_s: float = 0.0

    def publico(self) -> dict[str, Any]:
        return {
            "exito": self.exito,
            "version": self.version,
            "publicada": self.publicada,
            "sin_cambios": self.sin_cambios,
            "motivo": self.motivo,
            "validacion": self.validacion,
            "recursos": self.recursos,
            "duracion_s": round(self.duracion_s, 2),
        }


# ------------------------------------------------------------------ cerrojo
def adquirir_bloqueo(db: Database, minutos: int) -> str | None:
    """Reserva la ejecución. Devuelve el token o ``None`` si ya hay una activa."""
    ahora = datetime.now(timezone.utc)
    caduca = ahora.timestamp() - minutos * 60
    coleccion = db[ATLAS_BLOQUEOS]
    token = uuid.uuid4().hex
    try:
        documento = coleccion.find_one_and_update(
            {
                "nombre": NOMBRE_BLOQUEO,
                "$or": [
                    {"token": {"$exists": False}},
                    {"expira_ts": {"$lt": caduca}},
                ],
            },
            {"$set": {"nombre": NOMBRE_BLOQUEO, "token": token, "expira_ts": ahora.timestamp(), "desde": ahora}},
            upsert=True,
            return_document=True,
        )
    except PyMongoError as error:
        # Un upsert simultáneo puede violar el índice único del nombre: significa
        # que otro proceso ganó el cerrojo.
        logger.info("Cerrojo no adquirido: %s", type(error).__name__)
        return None
    return token if documento and documento.get("token") == token else None


def liberar_bloqueo(db: Database, token: str) -> None:
    db[ATLAS_BLOQUEOS].delete_one({"nombre": NOMBRE_BLOQUEO, "token": token})


def version_activa(db: Database) -> dict[str, Any] | None:
    estado = db[ATLAS_ESTADO].find_one({"_id": ESTADO_ACTIVO})
    if not estado:
        return None
    return db[ATLAS_IMPORTACIONES].find_one({"version": estado.get("version")}, {"_id": 0})


def estado_publico(db: Database) -> dict[str, Any]:
    """Lo que la API muestra sobre la versión publicada y la última sincronización."""
    activa = version_activa(db)
    ultimo_intento = db[ATLAS_IMPORTACIONES].find_one(
        {}, {"_id": 0, "version": 1, "estado": 1, "creado_en": 1, "motivo": 1},
        sort=[("creado_en", -1)],
    )
    if not activa:
        return {
            "publicada": False,
            "version": None,
            "ultima_sincronizacion": None,
            "ultimo_intento": ultimo_intento,
            "mensaje": "Todavía no se ha importado ninguna versión del conjunto oficial.",
        }
    return {
        "publicada": True,
        "version": activa.get("version"),
        "ultima_sincronizacion": activa.get("publicado_en"),
        "ultimo_intento": ultimo_intento,
        "periodo": activa.get("periodo"),
        "cifras": activa.get("cifras"),
        "fuente": activa.get("fuente"),
        "recursos": activa.get("recursos"),
        "validacion": activa.get("validacion"),
    }


# ------------------------------------------------------------------ validación
def validar(tablas: dict[str, atlas_parser.Tabla], hechos: list[dict], incidencias: dict) -> dict[str, Any]:
    """Reglas de aceptación. Cualquier error impide publicar la versión."""
    errores: list[str] = []
    avisos: list[str] = []

    for clave, columnas in COLUMNAS_ESPERADAS.items():
        tabla = tablas.get(clave)
        if tabla is None:
            errores.append(f"Falta el recurso «{clave}».")
            continue
        faltantes = [columna for columna in columnas if columna not in tabla.claves]
        if faltantes:
            errores.append(f"El recurso «{clave}» no trae las columnas: {', '.join(faltantes)}.")
        if not tabla.filas:
            errores.append(f"El recurso «{clave}» está vacío.")

    if not hechos:
        errores.append("No se pudo construir ningún hecho.")

    for clave, tabla in tablas.items():
        if "folio" not in tabla.claves:
            continue
        folios = [fila.get("folio", "").strip() for fila in tabla.filas]
        if len(set(folios)) != len(folios):
            duplicados = len(folios) - len(set(folios))
            errores.append(f"El recurso «{clave}» repite {duplicados} folios: la relación 1:1 no se cumple.")

    if hechos:
        folios_hechos = {hecho["folio"] for hecho in hechos}
        for clave, tabla in tablas.items():
            if clave == "hechos" or "folio" not in tabla.claves:
                continue
            folios = {fila.get("folio", "").strip() for fila in tabla.filas}
            if folios != folios_hechos:
                errores.append(
                    f"El recurso «{clave}» no cubre los mismos folios que el archivo de hechos "
                    f"(faltan {len(folios_hechos - folios)}, sobran {len(folios - folios_hechos)})."
                )

        sin_fecha = sum(1 for hecho in hechos if not hecho.get("fecha"))
        if sin_fecha:
            avisos.append(f"{sin_fecha} hechos sin fecha interpretable.")

        totales_distintos = incidencias.get("total_distinto_de_lesionadas", 0)
        if totales_distintos:
            avisos.append(
                f"{totales_distintos} filas de los recursos de personas cuyo «total» no coincide con "
                "«personas_lesionadas» del archivo de hechos."
            )

        coordenadas = incidencias.get("coordenadas", {})
        if coordenadas.get("fuera_de_cdmx"):
            avisos.append(f"{coordenadas['fuera_de_cdmx']} coordenadas caen fuera de la Ciudad de México.")
        if coordenadas.get("ausente") or coordenadas.get("no_numerica"):
            avisos.append(
                f"{coordenadas.get('ausente', 0) + coordenadas.get('no_numerica', 0)} hechos sin coordenadas "
                "utilizables: se conservan y se informan, no se descartan."
            )
        if incidencias.get("alcaldia_texto_no_reconocida"):
            avisos.append(
                f"{incidencias['alcaldia_texto_no_reconocida']} hechos con una alcaldía de texto no reconocida."
            )
        if incidencias.get("desajuste_alcaldia"):
            avisos.append(
                f"{incidencias['desajuste_alcaldia']} hechos cuya alcaldía de texto no coincide con la geometría."
            )
        if incidencias.get("edad_sin_informacion"):
            avisos.append(f"{incidencias['edad_sin_informacion']} registros de edad sin información («SD»/«NA»).")

    return {"errores": errores, "avisos": avisos, "valida": not errores}


# ------------------------------------------------------------------ importación
def _limpiar_version(db: Database, version: str) -> None:
    db[ATLAS_HECHOS].delete_many({"version": version})


def _publicar_hechos(db: Database, version: str, hechos: list[dict], tamano: int = 2_000) -> int:
    coleccion = db[ATLAS_HECHOS]
    insertados = 0
    for inicio in range(0, len(hechos), tamano):
        lote = hechos[inicio : inicio + tamano]
        for documento in lote:
            documento["version"] = version
        coleccion.insert_many(lote, ordered=False)
        insertados += len(lote)
    return insertados


def sincronizar(
    db: Database,
    *,
    forzar: bool = False,
    directorio: Path | None = None,
) -> Resultado:
    """Ejecuta el ciclo completo: catálogo → descarga → validación → publicación."""
    from time import monotonic

    inicio = monotonic()
    ajustes = get_settings()
    directorio = directorio or ajustes.atlas_dir()
    directorio.mkdir(parents=True, exist_ok=True)

    token = adquirir_bloqueo(db, ajustes.atlas_bloqueo_min)
    if token is None:
        return Resultado(
            exito=False,
            motivo="Ya hay una sincronización en curso; no se ejecuta una segunda.",
        )

    tmp = directorio / "tmp" / uuid.uuid4().hex
    tmp.mkdir(parents=True, exist_ok=True)
    resultado = Resultado(exito=False)
    try:
        ensure_indexes(db)
        activa = version_activa(db)
        catalogo = sources.resolver_catalogo()
        resultado.recursos = {}
        etiquetas_previas = {
            clave: (recurso.get("etag"), recurso.get("last_modified"))
            for clave, recurso in ((activa or {}).get("recursos") or {}).items()
        }

        rutas: dict[str, Path] = {}
        hashes: dict[str, str] = {}
        metadatos_recursos: dict[str, dict] = {}
        for clave, remoto in catalogo.recursos.items():
            destino = tmp / f"{clave}.csv"
            etag_previo, modificado_previo = etiquetas_previas.get(clave, (None, None))
            descarga = sources.descargar(
                remoto, destino, etag=etag_previo, modificado=modificado_previo
            )
            if descarga["estado"] == "sin_cambios" and activa:
                # El servidor confirma que no cambió: se reutiliza el archivo ya
                # validado de la versión publicada.
                previo = directorio / "versiones" / str(activa["version"]) / f"{clave}.csv"
                if previo.exists():
                    shutil.copyfile(previo, destino)
            rutas[clave] = destino
            hashes[clave] = sources.sha256_de(destino)
            metadatos_recursos[clave] = {
                "uid": remoto.uid,
                "nombre": remoto.nombre,
                "url": remoto.url,
                "formato": remoto.formato,
                "bytes": descarga.get("bytes") or destino.stat().st_size,
                "sha256": hashes[clave],
                "etag": descarga.get("etag"),
                "last_modified": descarga.get("last_modified") or remoto.modificado,
                "modificado_en_catalogo": remoto.modificado,
                "estado_descarga": descarga["estado"],
            }
            resultado.recursos[clave] = metadatos_recursos[clave]

        huella = sources.sha256_de_bytes("".join(hashes[clave] for clave in sources.RECURSOS))
        # Identificador legible y estable: año del conjunto + huella del contenido.
        import re

        anio = re.search(r"(\d{4})", catalogo.nombre)
        version = f"ssc-{anio.group(1) if anio else 'serie'}-{huella[:10]}"

        if activa and activa.get("version") == version and not forzar:
            resultado.exito = True
            resultado.version = version
            resultado.sin_cambios = True
            resultado.motivo = "El contenido de los cinco recursos no cambió; se conserva la versión publicada."
            resultado.duracion_s = monotonic() - inicio
            return resultado

        # --- lectura completa y construcción de hechos ----------------------
        tablas = {clave: atlas_parser.leer_tabla(ruta, clave) for clave, ruta in rutas.items()}
        geometria = obtener_geometria()
        hechos, incidencias = atlas_parser.construir_hechos(tablas, geometria)
        cifras = atlas_parser.resumen_legible(hechos, incidencias)
        validacion = validar(tablas, hechos, incidencias)
        validacion["cifras"] = cifras
        validacion["tablas"] = {
            clave: {
                "archivo": f"{clave}.csv",
                "filas": len(tabla.filas),
                "columnas": tabla.cabecera,
                "encabezados_duplicados": tabla.duplicados,
                "separador": tabla.separador,
            }
            for clave, tabla in tablas.items()
        }
        resultado.validacion = validacion

        if not validacion["valida"]:
            resultado.motivo = "La versión no pasó la validación; se conserva la última publicada."
            _registrar_fallo(db, version, catalogo, metadatos_recursos, validacion, resultado.motivo)
            resultado.duracion_s = monotonic() - inicio
            return resultado

        # --- publicación ----------------------------------------------------
        _limpiar_version(db, version)
        insertados = _publicar_hechos(db, version, hechos)

        carpeta = directorio / "versiones" / version
        if carpeta.exists():
            shutil.rmtree(carpeta)
        carpeta.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(carpeta))
        (carpeta / "metadatos.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "catalogo": catalogo.publico(),
                    "recursos": metadatos_recursos,
                    "validacion": validacion,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

        ahora = datetime.now(timezone.utc)
        documento = {
            "version": version,
            "estado": "publicada",
            "fuente": catalogo.publico(),
            "recursos": metadatos_recursos,
            "periodo": cifras["periodo"],
            "cifras": {k: v for k, v in cifras.items() if k != "incidencias"},
            "incidencias": incidencias,
            "validacion": {"errores": validacion["errores"], "avisos": validacion["avisos"]},
            "hechos_insertados": insertados,
            "creado_en": ahora,
            "publicado_en": ahora,
        }
        db[ATLAS_IMPORTACIONES].update_one({"version": version}, {"$set": documento}, upsert=True)
        # Cambio de puntero en una sola operación: publicación atómica.
        db[ATLAS_ESTADO].update_one(
            {"_id": ESTADO_ACTIVO},
            {"$set": {"version": version, "actualizado_en": ahora}},
            upsert=True,
        )
        db[ATLAS_BLOQUEOS].update_one({"nombre": NOMBRE_BLOQUEO}, {"$set": {"ultima_version": version}})

        resultado.exito = True
        resultado.version = version
        resultado.publicada = True
        resultado.duracion_s = monotonic() - inicio
        logger.info("Atlas vial: versión %s publicada con %s hechos", version, insertados)
        return resultado
    except Exception as error:  # noqa: BLE001 - se registra y se conserva lo publicado
        logger.exception("La sincronización del Atlas vial falló")
        resultado.motivo = f"{type(error).__name__}: {error}"
        try:
            _registrar_fallo(db, resultado.version, None, resultado.recursos, resultado.validacion, resultado.motivo)
        except PyMongoError:
            logger.warning("No se pudo registrar el fallo de la sincronización")
        resultado.duracion_s = monotonic() - inicio
        return resultado
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        liberar_bloqueo(db, token)


def _registrar_fallo(
    db: Database,
    version: str | None,
    catalogo: sources.CatalogoRemoto | None,
    recursos: dict[str, Any],
    validacion: dict[str, Any],
    motivo: str,
) -> None:
    marca = version or f"fallida-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    db[ATLAS_IMPORTACIONES].update_one(
        {"version": marca},
        {
            "$set": {
                "version": marca,
                "estado": "rechazada",
                "fuente": catalogo.publico() if catalogo else None,
                "recursos": recursos,
                "validacion": {"errores": validacion.get("errores", []), "avisos": validacion.get("avisos", [])},
                "motivo": motivo,
                "creado_en": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


def marcar_publicada_desde_disco(db: Database, version: str) -> None:
    """Repunta la versión activa a una carpeta ya validada (recuperación manual)."""
    ajustes = get_settings()
    carpeta = ajustes.atlas_dir() / "versiones" / version
    if not carpeta.exists():
        raise FileNotFoundError(f"No existe la versión {version} en {carpeta}")
    db[ATLAS_ESTADO].update_one(
        {"_id": ESTADO_ACTIVO},
        {"$set": {"version": version, "actualizado_en": datetime.now(timezone.utc)}},
        upsert=True,
    )
