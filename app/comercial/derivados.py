"""Derivados locales del Atlas comercial (M5).

El conjunto original trae el detalle **producto × tienda** (``m5_bloques``) y unos
pocos agregados publicados con la importación (``total``, ``tienda``, ``estado``,
``categoria``, ``departamento`` y ``categoria_tienda``). La exploración por
departamentos dentro de una tienda necesita la intersección
**tienda × departamento**, que M5 no publica y que no puede deducirse de los
marginales: sumar «departamento» y «tienda» por separado inventaría una
distribución que los datos no tienen.

Este módulo calcula esa intersección desde los bloques locales y el catálogo, la
guarda como un agregado más (``dimension="departamento_tienda"``) y controla su
**estado de construcción y publicación**:

``construyendo``  se están escribiendo documentos; la API no los usa.
``fallo``         la reconciliación no cuadró; no se publica nada.
``publicado``     reconcilia con ``total``, ``tienda`` y ``departamento``.

Reglas:

- Idempotente: si el derivado de esa versión ya está publicado con la misma
  huella, se reutiliza sin recalcular.
- No toca los bloques de ventas, ni los agregados de la importación, ni el
  puntero de la versión activa.
- Nunca se sirve un derivado a medio construir: mientras el estado no sea
  ``publicado``, la API calcula la serie al vuelo desde los bloques.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from pymongo.database import Database

from app.comercial.esquema import documento_agregado
from app.db.mongo import M5_AGREGADOS, M5_BLOQUES, M5_CATALOGO, M5_DERIVADOS

NOMBRE_DERIVADO = "departamento_tienda"
"""Nombre del derivado y, a la vez, la dimensión con la que se guarda."""

CLAVE_DERIVADO = "departamento_tienda"
ESQUEMA_DERIVADO = "m5-derivado-1"
SEPARADOR = "|"

ESTADO_CONSTRUYENDO = "construyendo"
ESTADO_PUBLICADO = "publicado"
ESTADO_FALLO = "fallo"


class ErrorDerivado(RuntimeError):
    """El derivado no puede construirse ni publicarse."""


# --------------------------------------------------------------------------- #
# Estado
# --------------------------------------------------------------------------- #


def _id_estado(version: str) -> str:
    return f"{version}:{NOMBRE_DERIVADO}"


def estado(db: Database, version: str) -> dict | None:
    """Documento de estado del derivado para esa versión (o ``None``)."""
    return db[M5_DERIVADOS].find_one({"_id": _id_estado(version)})


def publicado(db: Database, version: str) -> bool:
    """¿Se puede leer el derivado sin exponer resultados parciales?"""
    documento = estado(db, version)
    return bool(documento and documento.get("estado") == ESTADO_PUBLICADO)


def info(db: Database, version: str) -> dict | None:
    """Resumen del derivado para publicarlo en la respuesta de la API."""
    documento = estado(db, version)
    if not documento:
        return None
    return {
        "nombre": NOMBRE_DERIVADO,
        "estado": documento.get("estado"),
        "publicado": documento.get("estado") == ESTADO_PUBLICADO,
        "version_datos": version,
        "huella": documento.get("huella"),
        "documentos": documento.get("documentos"),
        "dias": documento.get("dias"),
        "construido_en": documento.get("construido_en"),
        "duracion_s": documento.get("duracion_s"),
        "reconciliacion": documento.get("reconciliacion"),
        "reutilizado": documento.get("reutilizado", False),
    }


# --------------------------------------------------------------------------- #
# Cálculo
# --------------------------------------------------------------------------- #


def _huella(version: str, dias: int, bloques: int, claves: list[str]) -> str:
    """Huella estable del derivado (determina si se puede reutilizar)."""
    texto = SEPARADOR.join(
        [version, NOMBRE_DERIVADO, str(dias), str(bloques), *sorted(claves)]
    )
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16]


def _referencia(db: Database, version: str) -> tuple[int, list[int | None]]:
    """``d_inicio`` y vector del agregado total: define el dominio del derivado."""
    documento = db[M5_AGREGADOS].find_one(
        {"version": version, "dimension": "total"}, {"d_inicio": 1, "unidades": 1}
    )
    if not documento:
        raise ErrorDerivado("No hay agregados de la versión: no puede derivarse nada")
    return int(documento["d_inicio"]), list(documento["unidades"])


def _mapa_productos(db: Database, version: str) -> dict[str, tuple[str, str]]:
    """``item_id`` → (``dept_id``, ``cat_id``) desde el catálogo real."""
    mapa: dict[str, tuple[str, str]] = {}
    for documento in db[M5_CATALOGO].find(
        {"version": version, "tipo": "producto"}, {"item_id": 1, "dept_id": 1, "cat_id": 1}
    ):
        mapa[documento["item_id"]] = (documento["dept_id"], documento["cat_id"])
    if not mapa:
        raise ErrorDerivado("El catálogo de la versión no tiene productos")
    return mapa


def claves_esperadas(db: Database, version: str, tiendas: list[str] | None = None) -> list[str]:
    """Combinaciones ``tienda|departamento`` que existen en el catálogo."""
    tiendas_version = sorted(
        d["store_id"] for d in db[M5_CATALOGO].find({"version": version, "tipo": "tienda"}, {"store_id": 1})
    )
    departamentos = sorted(
        {d["dept_id"] for d in db[M5_CATALOGO].find({"version": version, "tipo": "departamento"}, {"dept_id": 1})}
    )
    elegidas = [t for t in tiendas_version if not tiendas or t in tiendas]
    return [f"{tienda}{SEPARADOR}{dept}" for tienda in elegidas for dept in departamentos]


def calcular(db: Database, version: str, *, tiendas: list[str] | None = None) -> dict:
    """Suma los bloques producto × tienda por ``tienda|departamento``.

    Un solo recorrido de ``m5_bloques`` por tienda. No lee agregados marginales:
    la intersección se calcula desde el detalle.
    """
    d_inicio, vector_total = _referencia(db, version)
    dias = len(vector_total)
    mapa = _mapa_productos(db, version)
    claves = set(claves_esperadas(db, version, tiendas))
    series: dict[str, list[int]] = {clave: [0] * dias for clave in claves}

    consulta: dict = {"version": version}
    if tiendas:
        consulta["store_id"] = {"$in": list(tiendas)}
    bloques = 0
    for documento in db[M5_BLOQUES].find(
        consulta, {"store_id": 1, "item_id": 1, "d_inicio": 1, "unidades": 1}
    ):
        departamento = mapa.get(documento["item_id"], ("?", ""))[0]
        destino = series.get(f"{documento['store_id']}{SEPARADOR}{departamento}")
        if destino is None:
            continue
        bloques += 1
        inicio = int(documento["d_inicio"]) - d_inicio
        unidades = documento["unidades"]
        fin = min(dias, inicio + len(unidades))
        if inicio < 0 or fin <= inicio:
            continue
        destino[inicio:fin] = [
            acumulado + valor for acumulado, valor in zip(destino[inicio:fin], unidades[: fin - inicio])
        ]

    return {
        "d_inicio": d_inicio,
        "dias": dias,
        "series": series,
        "bloques_leidos": bloques,
        "huella": _huella(version, dias, bloques, list(series)),
    }


# --------------------------------------------------------------------------- #
# Reconciliación y publicación
# --------------------------------------------------------------------------- #


def _vector_agregado(db: Database, version: str, dimension: str, clave: str | None) -> list[int] | None:
    consulta: dict = {"version": version, "dimension": dimension}
    if clave is not None:
        consulta["clave"] = clave
    documento = db[M5_AGREGADOS].find_one(consulta, {"d_inicio": 1, "unidades": 1})
    if not documento:
        return None
    return [0 if valor is None else int(valor) for valor in documento["unidades"]]


def reconciliar(db: Database, version: str, series: dict[str, list[int]], *, d_inicio: int) -> dict:
    """Comprueba el derivado contra ``total``, ``tienda`` y ``departamento``.

    Tres comprobaciones, por suma y día a día:

    - la suma de todo el derivado es el total de la versión;
    - cada tienda suma lo mismo que su agregado ``tienda``;
    - cada departamento suma lo mismo que su agregado ``departamento`` (cuya clave
      en ``m5_agregados`` es ``categoría|departamento``).
    """
    total = _vector_agregado(db, version, "total", None)
    if total is None:
        raise ErrorDerivado("Falta el agregado total: no hay contra qué reconciliar")
    dias = len(total)
    categoria_de = {
        documento["dept_id"]: documento["cat_id"]
        for documento in db[M5_CATALOGO].find(
            {"version": version, "tipo": "departamento"}, {"dept_id": 1, "cat_id": 1}
        )
    }

    suma_total = [0] * dias
    por_tienda: dict[str, list[int]] = {}
    por_departamento: dict[str, list[int]] = {}
    for clave, valores in series.items():
        tienda, departamento = clave.split(SEPARADOR, 1)
        destino_t = por_tienda.setdefault(tienda, [0] * dias)
        destino_d = por_departamento.setdefault(departamento, [0] * dias)
        for indice, valor in enumerate(valores[:dias]):
            suma_total[indice] += valor
            destino_t[indice] += valor
            destino_d[indice] += valor

    diferencias: list[str] = []
    if suma_total != total:
        diferencias.append("la suma del derivado no coincide con el agregado total")

    tiendas_revisadas = 0
    for tienda, valores in sorted(por_tienda.items()):
        esperado = _vector_agregado(db, version, "tienda", tienda)
        if esperado is None:
            diferencias.append(f"falta el agregado de la tienda {tienda}")
            continue
        tiendas_revisadas += 1
        if valores != esperado:
            diferencias.append(f"la tienda {tienda} no coincide con su agregado")

    departamentos_revisados = 0
    for departamento, valores in sorted(por_departamento.items()):
        clave = f"{categoria_de.get(departamento, departamento)}{SEPARADOR}{departamento}"
        esperado = _vector_agregado(db, version, "departamento", clave)
        if esperado is None:
            diferencias.append(f"falta el agregado del departamento {departamento}")
            continue
        departamentos_revisados += 1
        if valores != esperado:
            diferencias.append(f"el departamento {departamento} no coincide con su agregado")

    return {
        "ok": not diferencias,
        "diferencias": diferencias,
        "total_unidades": sum(suma_total),
        "tiendas": tiendas_revisadas,
        "departamentos": departamentos_revisados,
        "claves": len(series),
        "dias": dias,
        "d_inicio": d_inicio,
    }


def _stats(db: Database) -> dict:
    try:
        datos = db.command("collStats", M5_AGREGADOS)
    except Exception:  # pragma: no cover - entorno sin permiso de stats
        return {"bytes_datos": None, "bytes_almacenamiento": None, "documentos": None}
    return {
        "bytes_datos": datos.get("size"),
        "bytes_almacenamiento": datos.get("storageSize"),
        "documentos": datos.get("count"),
    }


def construir(
    db: Database,
    version: str,
    *,
    forzar: bool = False,
    tiendas: list[str] | None = None,
    solo_tiendas_parciales: bool = False,
) -> dict:
    """Calcula, reconcilia y publica el derivado (idempotente).

    ``solo_tiendas_parciales`` no publica: sirve para el respaldo de la API cuando
    el derivado aún no existe.
    """
    if not forzar and not solo_tiendas_parciales:
        existente = estado(db, version)
        if existente and existente.get("estado") == ESTADO_PUBLICADO:
            return {**info(db, version), "reutilizado": True}

    inicio = time.perf_counter()
    db[M5_DERIVADOS].replace_one(
        {"_id": _id_estado(version)},
        {
            "_id": _id_estado(version),
            "esquema": ESQUEMA_DERIVADO,
            "version": version,
            "derivado": NOMBRE_DERIVADO,
            "estado": ESTADO_CONSTRUYENDO,
            "iniciado_en": datetime.now(timezone.utc),
        },
        upsert=True,
    )
    antes = _stats(db)
    try:
        calculo = calcular(db, version, tiendas=tiendas)
        reconciliacion = reconciliar(db, version, calculo["series"], d_inicio=calculo["d_inicio"])
        if not reconciliacion["ok"]:
            raise ErrorDerivado(
                "El derivado no reconcilia: " + "; ".join(reconciliacion["diferencias"][:5])
            )
        if solo_tiendas_parciales:
            return {
                "derivado": NOMBRE_DERIVADO,
                "version": version,
                "estado": ESTADO_CONSTRUYENDO,
                "publicado": False,
                "series": calculo["series"],
                "reconciliacion": reconciliacion,
                "duracion_s": round(time.perf_counter() - inicio, 3),
                "huella": calculo["huella"],
            }

        # Se reescriben solo los documentos del derivado de esta versión.
        db[M5_AGREGADOS].delete_many({"version": version, "dimension": NOMBRE_DERIVADO})
        documentos = []
        for clave, valores in sorted(calculo["series"].items()):
            tienda, departamento = clave.split(SEPARADOR, 1)
            documentos.append(
                documento_agregado(
                    version=version,
                    dimension=NOMBRE_DERIVADO,
                    clave=clave,
                    d_inicio=calculo["d_inicio"],
                    unidades=valores,
                    extra={
                        "derivado": NOMBRE_DERIVADO,
                        "store_id": tienda,
                        "dept_id": departamento,
                    },
                )
            )
        if documentos:
            db[M5_AGREGADOS].insert_many(documentos, ordered=False)
        despues = _stats(db)
        duracion = round(time.perf_counter() - inicio, 3)
        publicado_en = datetime.now(timezone.utc)
        db[M5_DERIVADOS].replace_one(
            {"_id": _id_estado(version)},
            {
                "_id": _id_estado(version),
                "esquema": ESQUEMA_DERIVADO,
                "version": version,
                "derivado": NOMBRE_DERIVADO,
                "estado": ESTADO_PUBLICADO,
                "huella": calculo["huella"],
                "dias": calculo["dias"],
                "d_inicio": calculo["d_inicio"],
                "documentos": len(documentos),
                "bloques_leidos": calculo["bloques_leidos"],
                "duracion_s": duracion,
                "construido_en": publicado_en,
                "reconciliacion": reconciliacion,
                "almacenamiento": {
                    "agregados_documentos_antes": (antes or {}).get("documentos"),
                    "agregados_documentos_despues": (despues or {}).get("documentos"),
                    "bytes_datos_antes": (antes or {}).get("bytes_datos"),
                    "bytes_datos_despues": (despues or {}).get("bytes_datos"),
                    "bytes_almacenamiento_antes": (antes or {}).get("bytes_almacenamiento"),
                    "bytes_almacenamiento_despues": (despues or {}).get("bytes_almacenamiento"),
                },
            },
            upsert=True,
        )
        return {**info(db, version), "reutilizado": False}
    except Exception as exc:
        db[M5_DERIVADOS].replace_one(
            {"_id": _id_estado(version)},
            {
                "_id": _id_estado(version),
                "esquema": ESQUEMA_DERIVADO,
                "version": version,
                "derivado": NOMBRE_DERIVADO,
                "estado": ESTADO_FALLO,
                "detalle": str(exc)[:500],
                "fallo_en": datetime.now(timezone.utc),
            },
            upsert=True,
        )
        raise


# --------------------------------------------------------------------------- #
# Lectura para la API
# --------------------------------------------------------------------------- #


def series(
    db: Database, version: str, *, tienda: str, departamentos: list[str]
) -> dict[str, list[int | None]]:
    """Serie diaria por departamento de una tienda.

    Si el derivado está **publicado**, se lee de ``m5_agregados`` (una lectura por
    clave). Si no, se calcula al vuelo desde los bloques de esa tienda: así nunca
    se sirve un derivado a medio construir.
    """
    claves = [f"{tienda}{SEPARADOR}{departamento}" for departamento in departamentos]
    if publicado(db, version):
        documentos = {
            documento["clave"]: documento
            for documento in db[M5_AGREGADOS].find(
                {"version": version, "dimension": NOMBRE_DERIVADO, "clave": {"$in": claves}},
                {"clave": 1, "d_inicio": 1, "unidades": 1},
            )
        }
        d_inicio, _ = _referencia(db, version)
        salida: dict[str, list[int | None]] = {}
        for clave, departamento in zip(claves, departamentos):
            documento = documentos.get(clave)
            if documento is None:
                salida[departamento] = []
                continue
            desplazamiento = int(documento["d_inicio"]) - d_inicio
            relleno: list[int | None] = [None] * desplazamiento
            salida[departamento] = relleno + list(documento["unidades"])
        return salida

    calculo = calcular(db, version, tiendas=[tienda])
    return {
        departamento: calculo["series"].get(f"{tienda}{SEPARADOR}{departamento}", [])
        for departamento in departamentos
    }
