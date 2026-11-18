"""Importador idempotente del boletín de la CNBV.

Garantías
---------
* **Idempotente**: cada hecho e indicador se escribe con
  ``ReplaceOne(..., upsert=True)`` sobre su clave natural, así que volver a
  importar el mismo archivo no duplica nada y el reporte lo dice.
* **Revisiones sin sobrescritura silenciosa**: si el hash del archivo cambia, se
  crea una revisión nueva; las anteriores se conservan y solo se marcan como no
  vigentes.
* **No borra nada**: nunca se eliminan colecciones, bases ni documentos de otras
  fuentes; solo se escriben documentos con ``dataset_id`` = CNBV.
* **Ausentes ≠ cero**: las celdas vacías no generan hechos y se cuentan aparte.
* **Solo lo que el tablero usa**: hechos normalizados (``cnbv_hechos``),
  indicadores publicados (``cnbv_indicadores``) y metadatos de importación
  (``cnbv_boletines``). Las filas tal como vienen del archivo se cuentan y quedan
  en la ficha del boletín; el .xlsx original se conserva fuera de Atlas.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pymongo import ReplaceOne
from pymongo.database import Database

from app.cnbv import sources
from app.cnbv.parser import ResultadoBoletin, parsear_boletin
from app.db.mongo import CNBV_BOLETINES, CNBV_HECHOS, CNBV_INDICADORES

logger = logging.getLogger(__name__)

TAMANO_LOTE = 1_000
AGENTE = "hugo-portfolio-cnbv-importer/1.0 (portafolio local)"


@dataclass
class ReporteImportacion:
    periodo: str
    archivo: str
    url: str
    sha256: str
    bytes: int
    revision: int = 0
    estado: str = "importado"  # importado | sin_cambios | error
    hechos_nuevos: int = 0
    hechos_actualizados: int = 0
    duplicados: int = 0
    filas_originales: int = 0
    indicadores: int = 0
    ausentes: int = 0
    rechazados: list[dict] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    instituciones: int = 0
    conceptos: int = 0

    def resumen(self) -> dict:
        return {
            "periodo": self.periodo,
            "archivo": self.archivo,
            "url": self.url,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "revision": self.revision,
            "estado": self.estado,
            "hechos_nuevos": self.hechos_nuevos,
            "hechos_actualizados": self.hechos_actualizados,
            "duplicados": self.duplicados,
            "filas_originales": self.filas_originales,
            "indicadores": self.indicadores,
            "celdas_ausentes": self.ausentes,
            "rechazados": len(self.rechazados),
            "rechazados_detalle": self.rechazados[:20],
            "instituciones": self.instituciones,
            "conceptos": self.conceptos,
            "avisos": self.avisos,
        }


def _fecha_bson(valor):
    """BSON no admite ``date``: se guarda como ``datetime`` a medianoche."""
    if isinstance(valor, datetime):
        return valor
    return datetime(valor.year, valor.month, valor.day)


def sha256_archivo(ruta: Path) -> str:
    digest = hashlib.sha256()
    with ruta.open("rb") as manejador:
        for bloque in iter(lambda: manejador.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


def descargar_boletin(periodo: str, destino: Path) -> Path:
    """Descarga el boletín del periodo indicado (periodo ya verificado)."""
    fuente = sources.archivo_fuente(periodo)
    destino.mkdir(parents=True, exist_ok=True)
    ruta = destino / fuente.nombre
    if ruta.exists() and ruta.stat().st_size > 100_000:
        logger.info("Ya existe %s; se reutiliza el archivo local", ruta.name)
        return ruta
    peticion = urllib.request.Request(fuente.url, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(peticion, timeout=180) as respuesta:
        if respuesta.status != 200:
            raise RuntimeError(f"HTTP {respuesta.status} al descargar {fuente.url}")
        ruta.write_bytes(respuesta.read())
    logger.info("Descargado %s (%s bytes)", ruta.name, ruta.stat().st_size)
    return ruta


def _clave_hecho(documento: dict) -> dict:
    return {
        "dataset_id": documento["dataset_id"],
        "periodo": documento["periodo"],
        "revision": documento["revision"],
        "institucion_id": documento["institucion_id"],
        "concepto_id": documento["concepto_id"],
    }


def _clave_fila(documento: dict) -> dict:
    return {
        "dataset_id": documento["dataset_id"],
        "periodo": documento["periodo"],
        "revision": documento["revision"],
        "hoja": documento["hoja"],
        "fila": documento["fila"],
    }


def _clave_indicador(documento: dict) -> dict:
    """Clave de un indicador: boletín + periodo del valor + institución.

    ``periodo`` es la fecha del valor (el boletín publica tres columnas: mismo mes
    del año anterior, mes previo y mes de referencia), mientras que
    ``periodo_boletin`` identifica la publicación. Un mismo dato puede aparecer en
    dos boletines: en ese caso se conservan las dos vintage, cada una con su
    ``vigente`` calculado por boletín.
    """
    return {
        "dataset_id": documento["dataset_id"],
        "periodo_boletin": documento["periodo_boletin"],
        "periodo": documento["periodo"],
        "revision": documento["revision"],
        "institucion_id": documento["institucion_id"],
        "indicador_id": documento["indicador_id"],
    }


def _volcar(coleccion, documentos: list[dict], clave) -> tuple[int, int]:
    """Upsert por lotes. Devuelve (nuevos, duplicados)."""
    nuevos = duplicados = 0
    for inicio in range(0, len(documentos), TAMANO_LOTE):
        parte = documentos[inicio : inicio + TAMANO_LOTE]
        operaciones = [ReplaceOne(clave(doc), doc, upsert=True) for doc in parte]
        resultado = coleccion.bulk_write(operaciones, ordered=False)
        nuevos += resultado.upserted_count
        duplicados += resultado.matched_count
    return nuevos, duplicados


def _datos_hechos(resultado: ResultadoBoletin, dataset_id: str, revision: int) -> list[dict]:
    documentos = []
    for hecho in resultado.hechos:
        documento = dict(hecho)
        documento.update(
            {
                "fecha": _fecha_bson(hecho["fecha"]),
                "dataset_id": dataset_id,
                "revision": revision,
                "vigente": True,
                "moneda": sources.MONEDA,
                "archivo": resultado.archivo,
                "origen": "cnbv",
            }
        )
        documentos.append(documento)
    return documentos


def _datos_filas(resultado: ResultadoBoletin, dataset_id: str, revision: int) -> list[dict]:
    documentos = []
    for fila in resultado.filas_crudas:
        documento = dict(fila)
        documento.update(
            {
                "dataset_id": dataset_id,
                "periodo": resultado.periodo,
                "fecha": _fecha_bson(resultado.fecha),
                "revision": revision,
                "vigente": True,
                "archivo": resultado.archivo,
                "unidad_declarada": resultado.unidad_declarada,
                "titulo_hoja": resultado.titulo_hoja,
            }
        )
        documentos.append(documento)
    return documentos


def _datos_indicadores(
    resultado: ResultadoBoletin, dataset_id: str, revision: int
) -> list[dict]:
    documentos = []
    for indicador in resultado.indicadores:
        documento = dict(indicador)
        documento.update(
            {
                "fecha": _fecha_bson(indicador["fecha"]),
                "fecha_boletin": _fecha_bson(indicador["fecha_boletin"]),
                "dataset_id": dataset_id,
                "revision": revision,
                "vigente": True,
                "moneda": sources.MONEDA,
                "archivo": resultado.archivo,
                "origen": "cnbv",
            }
        )
        documentos.append(documento)
    return documentos


def importar_boletin(
    db: Database,
    ruta: Path,
    periodo: str | None = None,
    *,
    dataset_id: str = sources.DATASET_ID,
    url: str | None = None,
    forzar: bool = False,
) -> ReporteImportacion:
    """Importa un boletín local de forma idempotente y con control de revisiones."""
    if periodo is None:
        coincidencia = ruta.stem.split("_")[-1]
        periodo = coincidencia if coincidencia.isdigit() else ""
    if not periodo:
        raise ValueError(
            "no se pudo determinar el periodo; indícalo explícitamente (AAAAMM)"
        )
    iso = sources.periodo_a_iso(periodo)

    digest = sha256_archivo(ruta)
    tamano = ruta.stat().st_size
    reporte = ReporteImportacion(
        periodo=iso,
        archivo=ruta.name,
        url=url or f"{sources.URL_BASE_ARCHIVOS}/{ruta.name}",
        sha256=digest,
        bytes=tamano,
    )

    boletines = db[CNBV_BOLETINES]
    previos = list(
        boletines.find({"dataset_id": dataset_id, "periodo": iso}, {"_id": 0}).sort("revision", -1)
    )
    ultimo = previos[0] if previos else None

    if ultimo and ultimo.get("sha256") == digest and ultimo.get("vigente") and not forzar:
        reporte.estado = "sin_cambios"
        reporte.revision = int(ultimo["revision"])
        reporte.hechos_nuevos = 0
        reporte.duplicados = int(ultimo.get("hechos", 0))
        reporte.filas_originales = int(ultimo.get("filas_originales", 0))
        reporte.indicadores = int(ultimo.get("indicadores", 0))
        reporte.instituciones = int(ultimo.get("instituciones", 0))
        reporte.conceptos = int(ultimo.get("conceptos", 0))
        reporte.avisos.append(
            "El archivo ya estaba importado con el mismo hash: no se volvió a escribir."
        )
        return reporte

    revision = int(ultimo["revision"]) + 1 if ultimo else 1
    reporte.revision = revision

    if ultimo and ultimo.get("sha256") != digest:
        reporte.avisos.append(
            f"El archivo cambió respecto de la revisión {ultimo['revision']} "
            f"(hash distinto): se registra la revisión {revision} y la anterior se "
            "conserva como no vigente."
        )

    resultado = parsear_boletin(ruta, periodo)
    reporte.avisos.extend(resultado.avisos)
    reporte.rechazados = resultado.rechazados
    reporte.ausentes = resultado.ausentes
    reporte.instituciones = len(resultado.instituciones)
    reporte.conceptos = len(resultado.conceptos)

    hechos = _datos_hechos(resultado, dataset_id, revision)
    filas = _datos_filas(resultado, dataset_id, revision)
    indicadores = _datos_indicadores(resultado, dataset_id, revision)

    # Al aparecer una revisión nueva, las anteriores dejan de ser vigentes.
    # Los hechos se identifican por el periodo del boletín; los indicadores, por el
    # boletín del que provienen (``periodo_boletin``), porque sus valores traen tres
    # fechas distintas en el mismo archivo.
    #
    # Las filas tal como vienen del archivo **no se guardan**: se cuentan y quedan
    # en la ficha del boletín. El tablero solo necesita los hechos (una cifra por
    # institución, concepto y periodo) y el original se conserva fuera de Atlas.
    for coleccion, campo in (
        (db[CNBV_HECHOS], "periodo"),
        (db[CNBV_INDICADORES], "periodo_boletin"),
    ):
        coleccion.update_many(
            {"dataset_id": dataset_id, campo: iso, "vigente": True},
            {"$set": {"vigente": False}},
        )

    reporte.hechos_nuevos, duplicados_hechos = _volcar(db[CNBV_HECHOS], hechos, _clave_hecho)
    _, duplicados_ind = _volcar(db[CNBV_INDICADORES], indicadores, _clave_indicador)
    reporte.duplicados = duplicados_hechos + duplicados_ind
    reporte.hechos_actualizados = duplicados_hechos
    reporte.filas_originales = len(filas)
    reporte.indicadores = len(indicadores)

    boletines.update_many(
        {"dataset_id": dataset_id, "periodo": iso, "vigente": True},
        {"$set": {"vigente": False}},
    )
    boletines.replace_one(
        {"dataset_id": dataset_id, "periodo": iso, "revision": revision},
        {
            "dataset_id": dataset_id,
            "periodo": iso,
            "fecha": _fecha_bson(resultado.fecha),
            "revision": revision,
            "vigente": True,
            "archivo": ruta.name,
            "url": reporte.url,
            "sha256": digest,
            "bytes": tamano,
            "titulo_hoja": resultado.titulo_hoja,
            "unidad_declarada": resultado.unidad_declarada,
            "unidad_saldos": sources.UNIDAD_SALDOS,
            "unidad_indicadores": sources.UNIDAD_INDICADORES,
            "moneda": sources.MONEDA,
            "instituciones": len(resultado.instituciones),
            "conceptos": len(resultado.conceptos),
            "hechos": len(hechos),
            "filas_originales": len(filas),
            "indicadores": len(indicadores),
            "celdas_ausentes": resultado.ausentes,
            "rechazados": len(resultado.rechazados),
            "importado_en": datetime.now(timezone.utc),
            "origen": "cnbv",
            "fuente": sources.NOMBRE_FUENTE,
            "atribucion": sources.ATRIBUCION,
        },
        upsert=True,
    )

    reporte.estado = "importado"
    logger.info(
        "Importado %s rev %s: %s hechos nuevos, %s duplicados, %s ausentes",
        ruta.name,
        revision,
        reporte.hechos_nuevos,
        reporte.duplicados,
        reporte.ausentes,
    )
    return reporte
