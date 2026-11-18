"""Resolución y descarga de los recursos oficiales del catálogo de la CDMX.

No se construyen rutas de descarga por suposición: la URL de cada recurso se lee
del **catálogo oficial** (API CKAN del portal de datos abiertos de la Ciudad de
México) y se registra junto con sus metadatos. Se distingue siempre entre:

* ``catalogo_modificado``: cuándo se modificó el conjunto en el catálogo;
* ``recurso_modificado``: cuándo se modificó el archivo (nivel de recurso);
* ``descargado_en``: cuándo lo descargamos nosotros;
* ``periodo``: qué fechas tienen los eventos **dentro** del archivo. Un catálogo
  actualizado en 2026 no implica hechos de 2026.

Las descargas usan ``urllib`` de la biblioteca estándar (sin dependencias
nuevas), con reintentos y validación por tamaño y SHA-256. Cuando el servidor
ofrece ``ETag`` o ``Last-Modified`` se usan para pedar condicionalmente
(``If-None-Match`` / ``If-Modified-Since``): un 304 significa «sin cambios».
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Identificadores de los cinco recursos del conjunto «serie ampliada».
RECURSOS: dict[str, dict[str, str]] = {
    "hechos": {
        "uid": "27b88c58-e741-4526-bfd0-5200fe272f8d",
        "nombre": "Hechos de tránsito registrados por la SSC",
        "etiqueta": "Hechos de tránsito",
    },
    "sexo": {
        "uid": "273ac0c3-01f0-46e9-9000-a2d4ddb556de",
        "nombre": "Personas lesionadas o fallecidas (por sexo)",
        "etiqueta": "Personas por sexo",
    },
    "edad": {
        "uid": "6eafd074-df59-4f9c-86f3-57e2fc5094c4",
        "nombre": "Personas lesionadas o fallecidas (por edad)",
        "etiqueta": "Personas por edad",
    },
    "usuario": {
        "uid": "e152d68d-eda7-4ac9-972b-b926c116d4ab",
        "nombre": "Personas lesionadas o fallecidas (por tipo de persona)",
        "etiqueta": "Personas por tipo de usuario de la vía",
    },
    "vehiculos": {
        "uid": "d377236d-d2b7-4a14-aa65-88dca057af3a",
        "nombre": "Vehículos involucrados en hechos de tránsito",
        "etiqueta": "Vehículos involucrados",
    },
}


@dataclass
class RecursoRemoto:
    clave: str
    uid: str
    nombre: str
    url: str
    formato: str
    bytes: int | None
    creado: str | None
    modificado: str | None
    metadatos: dict[str, Any] = field(default_factory=dict)


@dataclass
class CatalogoRemoto:
    nombre: str
    titulo: str
    notas: str
    licencia: str
    portal: str
    catalogo_modificado: str | None
    recursos: dict[str, RecursoRemoto]

    def publico(self) -> dict[str, Any]:
        return {
            "conjunto": self.nombre,
            "titulo": self.titulo,
            "notas": self.notas,
            "licencia": self.licencia,
            "portal": self.portal,
            "catalogo_modificado": self.catalogo_modificado,
            "recursos": {
                clave: {
                    "uid": recurso.uid,
                    "nombre": recurso.nombre,
                    "url": recurso.url,
                    "formato": recurso.formato,
                    "bytes": recurso.bytes,
                    "creado": recurso.creado,
                    "modificado": recurso.modificado,
                }
                for clave, recurso in self.recursos.items()
            },
        }


class ErrorCatalogo(RuntimeError):
    """El catálogo no respondió o no contiene lo que se espera."""


def _leer_json(url: str, timeout: int) -> dict[str, Any]:
    peticion = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "portafolio-atlas-vial/1.0"},
    )
    with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:  # noqa: S310 - URL del catálogo oficial
        return json.loads(respuesta.read().decode("utf-8"))


def resolver_catalogo(intentos: int | None = None) -> CatalogoRemoto:
    """Consulta el catálogo oficial y devuelve los cinco recursos con sus metadatos."""
    ajustes = get_settings()
    intentos = intentos or ajustes.atlas_max_intentos
    url = f"{ajustes.atlas_catalogo_url}?id={ajustes.atlas_dataset_id}"

    ultimo_error: Exception | None = None
    for intento in range(1, intentos + 1):
        try:
            datos = _leer_json(url, ajustes.atlas_timeout_s)
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
            ultimo_error = error
            logger.warning("Catálogo no disponible (intento %s/%s): %s", intento, intentos, error)
            time.sleep(min(2 * intento, 5))
    else:  # pragma: no cover - depende de la red
        raise ErrorCatalogo(f"No se pudo leer el catálogo oficial: {ultimo_error}")

    resultado = datos.get("result") or {}
    if resultado.get("name") != ajustes.atlas_dataset_id:
        raise ErrorCatalogo("El catálogo devolvió un conjunto distinto al solicitado.")

    remotos = {recurso.get("id"): recurso for recurso in resultado.get("resources", [])}
    recursos: dict[str, RecursoRemoto] = {}
    for clave, definicion in RECURSOS.items():
        remoto = remotos.get(definicion["uid"])
        if remoto is None:
            raise ErrorCatalogo(
                f"El catálogo no publica el recurso {definicion['uid']} ({definicion['nombre']})."
            )
        recursos[clave] = RecursoRemoto(
            clave=clave,
            uid=definicion["uid"],
            nombre=remoto.get("name") or definicion["nombre"],
            url=remoto.get("url") or "",
            formato=(remoto.get("format") or "CSV").upper(),
            bytes=remoto.get("size"),
            creado=remoto.get("created"),
            modificado=remoto.get("last_modified") or remoto.get("metadata_modified"),
            metadatos={
                "descripcion": remoto.get("description"),
                "hash": remoto.get("hash"),
                "mimetype": remoto.get("mimetype"),
            },
        )
        if not recursos[clave].url:
            raise ErrorCatalogo(f"El recurso {definicion['uid']} no trae URL de descarga.")

    return CatalogoRemoto(
        nombre=resultado.get("name") or ajustes.atlas_dataset_id,
        titulo=resultado.get("title") or "",
        notas=resultado.get("notes") or "",
        licencia=resultado.get("license_title") or "",
        portal=f"https://datos.cdmx.gob.mx/dataset/{resultado.get('name') or ajustes.atlas_dataset_id}",
        catalogo_modificado=resultado.get("metadata_modified"),
        recursos=recursos,
    )


def sha256_de(ruta: Path) -> str:
    digest = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


def sha256_de_bytes(texto: str) -> str:
    """Huella de una cadena (se usa para combinar los hashes de los recursos)."""
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def descargar(
    recurso: RecursoRemoto,
    destino: Path,
    *,
    etag: str | None = None,
    modificado: str | None = None,
    intentos: int | None = None,
) -> dict[str, Any]:
    """Descarga un recurso a ``destino``.

    Devuelve ``estado = 'sin_cambios'`` si el servidor responde 304 a una
    petición condicional, y ``estado = 'descargado'`` tras guardar el archivo.
    """
    ajustes = get_settings()
    intentos = intentos or ajustes.atlas_max_intentos
    destino.parent.mkdir(parents=True, exist_ok=True)
    cabeceras = {"User-Agent": "portafolio-atlas-vial/1.0", "Accept": "text/csv,*/*"}
    if etag:
        cabeceras["If-None-Match"] = etag
    if modificado:
        cabeceras["If-Modified-Since"] = modificado

    ultimo_error: Exception | None = None
    for intento in range(1, intentos + 1):
        peticion = urllib.request.Request(recurso.url, headers=cabeceras)
        try:
            with urllib.request.urlopen(peticion, timeout=ajustes.atlas_timeout_s) as respuesta:  # noqa: S310
                nuevo_etag = respuesta.headers.get("ETag")
                nuevo_modificado = respuesta.headers.get("Last-Modified")
                with destino.open("wb") as archivo:
                    shutil.copyfileobj(respuesta, archivo)
            return {
                "estado": "descargado",
                "bytes": destino.stat().st_size,
                "sha256": sha256_de(destino),
                "etag": nuevo_etag,
                "last_modified": nuevo_modificado,
                "url_final": respuesta.geturl(),
            }
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return {
                    "estado": "sin_cambios",
                    "bytes": destino.stat().st_size if destino.exists() else None,
                    "sha256": sha256_de(destino) if destino.exists() else None,
                    "etag": etag,
                    "last_modified": modificado,
                    "url_final": recurso.url,
                }
            ultimo_error = error
            logger.warning("Descarga de %s falló (%s): HTTP %s", recurso.clave, intento, error.code)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            ultimo_error = error
            logger.warning("Descarga de %s falló (%s): %s", recurso.clave, intento, error)
        time.sleep(min(2 * intento, 6))

    raise ErrorCatalogo(f"No se pudo descargar {recurso.nombre}: {ultimo_error}")
