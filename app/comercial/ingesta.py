"""Importación del conjunto M5 a MongoDB local.

Características que exige el encargo y cómo se cumplen:

- **Idempotente por versión**: la versión sale del hash de los archivos, así que
  volver a importar el mismo contenido no crea una versión nueva ni duplica
  documentos (los ``_id`` incluyen la versión).
- **Por lotes**: los bloques de series se insertan en lotes de ``LOTE_INSERCION``
  y los agregados se escriben al final, ya validados.
- **Reejecutable sin duplicar**: antes de publicar se borran los documentos de esa
  misma versión (por si una ejecución anterior quedó a medias).
- **Validada antes de activar**: el puntero ``m5_estado`` solo se mueve si las
  validaciones de reconciliación pasan.
- **Fuera de HTTP**: es un comando de consola (``scripts/importar_m5.py``).

Los CSV originales y los artefactos de entrenamiento **no** se guardan en
MongoDB: se registran su ruta, tamaño y hash en el manifiesto.
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from pymongo import UpdateOne
from pymongo.database import Database

from app.comercial import ESQUEMA_VERSION
from app.comercial import fuentes
from app.comercial.esquema import (
    ErrorEsquema,
    documento_agregado,
    fecha_comercial,
    texto_requerido,
    unidades_validas,
)
from app.db.mongo import (
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_CALENDARIO,
    M5_CATALOGO,
    M5_DERIVADOS,
    M5_ESTADO,
    M5_EVALUACION,
    M5_IMPORTACIONES,
    M5_PRONOSTICOS,
)

logger = logging.getLogger(__name__)

COLECCIONES_PROYECTO: tuple[str, ...] = (
    M5_IMPORTACIONES,
    M5_ESTADO,
    M5_CATALOGO,
    M5_CALENDARIO,
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_PRONOSTICOS,
    M5_EVALUACION,
    M5_DERIVADOS,
)
"""Colecciones que pertenecen al proyecto; el arranque solo crea índices de estas."""

LOTE_INSERCION = 2_000
"""Documentos por operación de escritura."""


class ErrorImportacion(RuntimeError):
    """Fallo de validación que impide publicar la versión."""


def _ruta_ventas(carpeta: Path) -> Path:
    """Archivo de ventas disponible (evaluación o, si falta, validación)."""
    completa = carpeta / fuentes.ARCHIVO_VENTAS
    if completa.exists():
        return completa
    alternativa = carpeta / fuentes.ARCHIVO_VENTAS_ALT
    if alternativa.exists():
        return alternativa
    raise ErrorImportacion(
        f"Falta {fuentes.ARCHIVO_VENTAS} (o {fuentes.ARCHIVO_VENTAS_ALT}) en {carpeta}"
    )


def validar_archivos(carpeta: Path) -> dict:
    """Comprueba archivos, cabeceras y hashes sin escribir en MongoDB."""
    carpeta = Path(carpeta)
    if not carpeta.is_dir():
        raise ErrorImportacion(f"La carpeta de datos no existe: {carpeta}")
    ventas = _ruta_ventas(carpeta)
    calendario = carpeta / fuentes.ARCHIVO_CALENDARIO
    if not calendario.exists():
        raise ErrorImportacion(f"Falta {fuentes.ARCHIVO_CALENDARIO} en {carpeta}")

    with ventas.open("r", encoding="utf-8", newline="") as manejador:
        cabecera = next(csv.reader(manejador))
    faltan = [c for c in fuentes.COLUMNAS_VENTAS_FIJAS if c not in cabecera]
    if faltan:
        raise ErrorImportacion(f"Faltan columnas en {ventas.name}: {', '.join(faltan)}")
    dias = fuentes.cuenta_dias_ventas(cabecera)

    with calendario.open("r", encoding="utf-8", newline="") as manejador:
        cab_cal = next(csv.reader(manejador))
    faltan_cal = [c for c in fuentes.COLUMNAS_CALENDARIO if c not in cab_cal]
    if faltan_cal:
        raise ErrorImportacion(f"Faltan columnas en {calendario.name}: {', '.join(faltan_cal)}")

    hash_ventas = fuentes.hash_archivo(ventas)
    hash_cal = fuentes.hash_archivo(calendario)
    _, fechas = _leer_calendario(carpeta)
    faltan_dias = [d for d in range(1, dias + 1) if d not in fechas]
    if faltan_dias:
        raise ErrorImportacion(
            f"El calendario no cubre d_{faltan_dias[0]}: las ventas traen {dias} días."
        )
    contenido = validar_contenido(carpeta, dias, fechas)
    return {
        "carpeta": str(carpeta),
        "archivo_ventas": ventas.name,
        "archivo_calendario": calendario.name,
        "dias": dias,
        "hash_ventas": hash_ventas,
        "hash_calendario": hash_cal,
        "bytes_ventas": ventas.stat().st_size,
        "bytes_calendario": calendario.stat().st_size,
        "version": fuentes.crear_version(ventas.name, dias, hash_ventas, hash_cal),
        "columnas": len(cabecera),
        "contenido": contenido,
        "validado": True,
    }


def _leer_calendario(carpeta: Path) -> tuple[list[dict], dict[int, date]]:
    """Lee **todo** el calendario: documentos por día y mapa ``d_N`` → fecha.

    M5 publica 1969 días de calendario, pero el archivo de ventas de evaluación
    solo cubre 1941. Los días que sobran no se descartan en silencio: el
    importador los cuenta y los informa como «calendario sin ventas».
    """
    documentos: list[dict] = []
    fechas: dict[int, date] = {}
    vistas: set[date] = set()
    with (carpeta / fuentes.ARCHIVO_CALENDARIO).open("r", encoding="utf-8", newline="") as manejador:
        lector = csv.DictReader(manejador)
        for fila in lector:
            dia = int(str(fila["d"]).lstrip("dD_") or 0)
            if dia < 1:
                raise ErrorImportacion(f"El calendario trae un día no válido: {fila['d']!r}")
            if dia in fechas:
                raise ErrorImportacion(f"Día repetido en el calendario: d_{dia}")
            fecha = fecha_comercial(fila["date"])
            if fecha in vistas:
                raise ErrorImportacion(f"Fecha repetida en el calendario: {fecha.isoformat()}")
            vistas.add(fecha)
            documentos.append(
                {
                    "_id": dia,
                    "d": dia,
                    "fecha": datetime(fecha.year, fecha.month, fecha.day),
                    "wm_yr_wk": int(fila["wm_yr_wk"]) if fila.get("wm_yr_wk") else None,
                    "weekday": fila.get("weekday"),
                    "wday": int(fila["wday"]) if fila.get("wday") else None,
                    "month": int(fila["month"]) if fila.get("month") else fecha.month,
                    "year": int(fila["year"]) if fila.get("year") else fecha.year,
                    "evento_1": fila.get("event_name_1") or None,
                    "tipo_1": fila.get("event_type_1") or None,
                    "evento_2": fila.get("event_name_2") or None,
                    "tipo_2": fila.get("event_type_2") or None,
                    "snap_ca": int(fila["snap_CA"]) if fila.get("snap_CA") else 0,
                    "snap_tx": int(fila["snap_TX"]) if fila.get("snap_TX") else 0,
                    "snap_wi": int(fila["snap_WI"]) if fila.get("snap_WI") else 0,
                }
            )
            fechas[dia] = fecha
    if not fechas:
        raise ErrorImportacion("El calendario está vacío.")
    faltan = [d for d in range(1, max(fechas) + 1) if d not in fechas]
    if faltan:
        raise ErrorImportacion(f"El calendario no es consecutivo: faltan d_{faltan[0]} y otros {len(faltan) - 1}")
    return documentos, fechas


def _calendario_compatible(existente: dict[int, date], nuevo: dict[int, date], dias: int) -> bool:
    """¿El calendario ya guardado sirve para esta importación?

    Solo se comparan los días que usan las ventas (1…dias). Si coinciden, el
    calendario se **comparte** entre versiones sin reescribirlo.
    """
    if not existente:
        return False
    for dia in range(1, dias + 1):
        if existente.get(dia) != nuevo.get(dia):
            return False
    return all(existente.get(d) == nuevo.get(d) for d in nuevo if d <= dias)


def validar_contenido(carpeta: Path, dias: int, fechas: dict[int, date]) -> dict:
    """Valida el contenido completo del archivo de ventas sin escribir nada.

    Comprueba, en una sola pasada y sin descartar registros:

    - identificadores únicos (``item_id × store_id``);
    - cantidades numéricas, finitas, enteras y no negativas (aborta en el primer
      valor inválido, indicando la serie y el día);
    - que las columnas ``d_*`` coincidan con el calendario importado;
    - tiendas, categorías y departamentos observados y su cobertura.

    Devuelve conteos y la cobertura efectiva de las **ventas** (que puede ser menor
    que la del calendario).
    """
    ventas = _ruta_ventas(carpeta)
    series = 0
    suma_total = 0
    ids: set[tuple[str, str]] = set()
    duplicados: list[str] = []
    tiendas: set[str] = set()
    estados: set[str] = set()
    categorias: set[str] = set()
    departamentos: set[tuple[str, str]] = set()
    productos: set[str] = set()
    primeros = ultimos = 0

    with ventas.open("r", encoding="utf-8", newline="") as manejador:
        lector = csv.DictReader(manejador)
        columnas_dias = [c for c in lector.fieldnames or [] if c.startswith("d_")]
        if len(columnas_dias) != dias:
            raise ErrorImportacion(
                f"El archivo trae {len(columnas_dias)} columnas d_* y el calendario cubre {dias}."
            )
        for indice, fila in enumerate(lector):
            item_id = texto_requerido(fila["item_id"], "item_id")
            store_id = texto_requerido(fila["store_id"], "store_id")
            clave = (item_id, store_id)
            if clave in ids:
                duplicados.append(f"{item_id}/{store_id}")
            ids.add(clave)
            tiendas.add(store_id)
            estados.add(texto_requerido(fila["state_id"], "state_id"))
            categorias.add(texto_requerido(fila["cat_id"], "cat_id"))
            departamentos.add(
                (texto_requerido(fila["cat_id"], "cat_id"), texto_requerido(fila["dept_id"], "dept_id"))
            )
            productos.add(item_id)
            for numero_dia, columna in enumerate(columnas_dias, start=1):
                if numero_dia not in fechas:
                    raise ErrorImportacion(
                        f"La columna {columna} no tiene fecha: el calendario no cubre d_{numero_dia}."
                    )
                valor = unidades_validas(fila[columna], f"{item_id}/{store_id} d_{numero_dia}")
                suma_total += valor
                primeros += 1 if valor > 0 else 0
                if indice == 0:
                    ultimos += valor
            series += 1

    if duplicados:
        raise ErrorImportacion(
            f"Identificadores repetidos ({len(duplicados)}): {', '.join(duplicados[:3])}"
        )
    return {
        "series": series,
        "productos": len(productos),
        "tiendas": len(tiendas),
        "estados": len(estados),
        "categorias": len(categorias),
        "departamentos": len(departamentos),
        "suma_unidades": suma_total,
        "dias_de_ventas": dias,
        "dias_de_calendario": len(fechas),
        "dias_calendario_sin_ventas": max(0, len(fechas) - dias),
        "cobertura_ventas": {
            "desde": fechas[min(fechas)].isoformat(),
            "hasta": fechas[dias].isoformat(),
            "dias": dias,
        },
        "celdas_positivas": primeros,
        "suma_primera_serie": ultimos,
    }


def _escribir_lotes(coleccion, documentos: list[dict]) -> int:
    """Reemplaza por ``_id`` en lotes; devuelve los documentos escritos."""
    escritos = 0
    for inicio in range(0, len(documentos), LOTE_INSERCION):
        lote = documentos[inicio : inicio + LOTE_INSERCION]
        operaciones = [
            UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True) for doc in lote
        ]
        if operaciones:
            coleccion.bulk_write(operaciones, ordered=False)
            escritos += len(operaciones)
    return escritos


def importar(
    db: Database,
    carpeta: Path,
    *,
    forzar: bool = False,
    limite_filas: int | None = None,
    publicar: bool = True,
) -> dict:
    """Importa (o reutiliza) una versión del conjunto M5.

    ``limite_filas`` solo existe para las pruebas: importa las primeras N series y
    marca el manifiesto como parcial. La publicación pública exige el archivo
    completo.
    """
    info = validar_archivos(carpeta)
    version = info["version"]
    carpeta = Path(carpeta)
    ventas = _ruta_ventas(carpeta)
    if limite_filas is not None:
        version = f"{version}-parcial{limite_filas}"
        info["version"] = version
        info["parcial"] = True

    previo = db[M5_IMPORTACIONES].find_one({"_id": version})
    if previo and previo.get("estado") == "publicada" and not forzar:
        logger.info("La versión %s ya está publicada: no se reimporta.", version)
        return previo

    calendario_todo, fechas_todas = _leer_calendario(carpeta)

    # El calendario se comparte entre versiones: se verifica su compatibilidad en
    # lugar de vaciar la colección. Nada de borrados generales.
    dias = info["dias"]
    existente = {
        int(documento["d"]): (
            documento["fecha"].date() if isinstance(documento["fecha"], datetime) else documento["fecha"]
        )
        for documento in db[M5_CALENDARIO].find({}, {"d": 1, "fecha": 1})
    }
    if existente and not _calendario_compatible(existente, fechas_todas, dias):
        raise ErrorImportacion(
            "El calendario guardado no coincide con el archivo para los días de ventas: "
            "no se sobrescribe porque otra versión publicada depende de él."
        )
    if not existente:
        _escribir_lotes(db[M5_CALENDARIO], calendario_todo)
    calendario_compartido = bool(existente)

    # Reejecución: solo se limpian los documentos de esta versión si **no** es la
    # activa. Si lo es, se reescribe por upsert con los mismos _id deterministas y
    # la versión publicada nunca queda incompleta.
    es_activa = version_actual(db) == version
    if not es_activa:
        patron_version = {"_id": {"$regex": f"^{version}:"}}
        for nombre in (M5_AGREGADOS, M5_BLOQUES, M5_CATALOGO):
            db[nombre].delete_many(patron_version)

    totales: list[int] = [0] * dias
    por_dimension: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * dias)
    tiendas: dict[str, dict] = {}
    categorias: set[str] = set()
    departamentos: dict[str, set[str]] = defaultdict(set)
    productos: dict[str, dict] = {}
    bloques: dict[tuple[str, str, int], list[int]] = {}
    series = 0
    suma_series = 0
    suma_negativos = 0

    with ventas.open("r", encoding="utf-8", newline="") as manejador:
        lector = csv.DictReader(manejador)
        columnas_dias = [c for c in lector.fieldnames or [] if c.startswith("d_")]
        for indice, fila in enumerate(lector):
            if limite_filas is not None and indice >= limite_filas:
                break
            item_id = texto_requerido(fila["item_id"], "item_id")
            dept_id = texto_requerido(fila["dept_id"], "dept_id")
            cat_id = texto_requerido(fila["cat_id"], "cat_id")
            store_id = texto_requerido(fila["store_id"], "store_id")
            state_id = texto_requerido(fila["state_id"], "state_id")

            tiendas.setdefault(
                store_id,
                {"_id": f"{version}:tienda:{store_id}", "esquema": ESQUEMA_VERSION, "version": version,
                 "tipo": "tienda", "store_id": store_id, "state_id": state_id},
            )
            categorias.add(cat_id)
            departamentos[cat_id].add(dept_id)
            productos.setdefault(
                item_id,
                {"_id": f"{version}:producto:{item_id}", "esquema": ESQUEMA_VERSION, "version": version,
                 "tipo": "producto", "item_id": item_id, "dept_id": dept_id, "cat_id": cat_id},
            )

            serie = [unidades_validas(valor, f"{item_id}/{store_id}") for valor in (fila[c] for c in columnas_dias)]
            if any(valor < 0 for valor in serie):  # pragma: no cover - unidades_validas ya lo impide
                suma_negativos += 1
            series += 1
            suma_series += sum(serie)

            for dia, valor in enumerate(serie, start=1):
                totales[dia - 1] += valor
            clave_tienda = ("tienda", store_id)
            clave_cat = ("categoria", cat_id)
            clave_estado = ("estado", state_id)
            clave_dept = ("departamento", f"{cat_id}|{dept_id}")
            clave_cat_tienda = ("categoria_tienda", f"{cat_id}|{store_id}")
            for clave in (clave_tienda, clave_cat, clave_estado, clave_dept, clave_cat_tienda):
                acumulado = por_dimension[clave]
                for dia, valor in enumerate(serie):
                    acumulado[dia] += valor

            tamano = fuentes.TAMANO_BLOQUE
            for inicio in range(0, dias, tamano):
                indice_bloque = inicio // tamano
                trozo = serie[inicio : inicio + tamano]
                bloques[(item_id, store_id, indice_bloque)] = trozo

            if len(bloques) >= LOTE_INSERCION:
                _escribir_lotes(db[M5_BLOQUES], [_doc_bloque(version, k, v, dias) for k, v in bloques.items()])
                bloques.clear()

    if bloques:
        _escribir_lotes(db[M5_BLOQUES], [_doc_bloque(version, k, v, dias) for k, v in bloques.items()])

    documentos_agregados = [
        documento_agregado(version=version, dimension="total", clave="todas", d_inicio=1, unidades=totales)
    ]
    for (dimension, clave), valores in por_dimension.items():
        documentos_agregados.append(
            documento_agregado(version=version, dimension=dimension, clave=clave, d_inicio=1, unidades=valores)
        )
    agregados_escritos = _escribir_lotes(db[M5_AGREGADOS], documentos_agregados)

    catalogo = [doc for doc in tiendas.values()]
    catalogo += [
        {"_id": f"{version}:estado:{estado}", "esquema": ESQUEMA_VERSION, "version": version,
         "tipo": "estado", "state_id": estado, "nombre": estado}
        for estado in sorted({t["state_id"] for t in tiendas.values()})
    ]
    catalogo += [
        {"_id": f"{version}:categoria:{cat}", "esquema": ESQUEMA_VERSION, "version": version,
         "tipo": "categoria", "cat_id": cat, "nombre": cat}
        for cat in sorted(categorias)
    ]
    catalogo += [
        {"_id": f"{version}:departamento:{cat}|{dep}", "esquema": ESQUEMA_VERSION, "version": version,
         "tipo": "departamento", "cat_id": cat, "dept_id": dep, "nombre": dep}
        for cat in sorted(departamentos)
        for dep in sorted(departamentos[cat])
    ]
    catalogo += list(productos.values())
    _escribir_lotes(db[M5_CATALOGO], catalogo)

    # --- Validaciones antes de publicar ---------------------------------------

    total_tienda = sum(
        sum(valores) for clave, valores in por_dimension.items() if clave[0] == "tienda"
    )
    validaciones = {
        "series_leidas": series,
        "suma_series": suma_series,
        "suma_agregado_total": sum(totales),
        "suma_agregado_tienda": total_tienda,
        "reconciliacion_total": suma_series == sum(totales),
        "reconciliacion_tiendas": suma_series == total_tienda,
        "dias_cubiertos": len([d for d in fechas_todas if d <= dias]),
        "dias_declarados": dias,
        "dias_de_calendario": len(fechas_todas),
        "dias_calendario_sin_ventas": max(0, len(fechas_todas) - dias),
        "calendario_completo": all(d in fechas_todas for d in range(1, dias + 1)),
        "ninguna_cantidad_negativa": suma_negativos == 0,
        "primer_dia": fechas_todas[min(fechas_todas)].isoformat(),
        "ultimo_dia": fechas_todas[dias].isoformat(),
        "bloques": db[M5_BLOQUES].count_documents({"_id": {"$regex": f"^{version}:"}}),
        "agregados": agregados_escritos,
        "catalogo": len(catalogo),
    }
    if not (validaciones["reconciliacion_total"] and validaciones["reconciliacion_tiendas"]):
        raise ErrorImportacion(
            "Los totales no reconcilian (series ≠ agregados): no se publica la versión."
        )
    if not validaciones["calendario_completo"]:
        raise ErrorImportacion("El calendario no cubre todos los días de ventas.")

    contenido = info.get("contenido", {})
    manifiesto = {
        "_id": version,
        "esquema": ESQUEMA_VERSION,
        "version": version,
        "estado": "validada",
        "publicada": False,
        "importada_en": datetime.now(timezone.utc),
        "archivo_ventas": info["archivo_ventas"],
        "archivo_calendario": info["archivo_calendario"],
        "hash_ventas": info["hash_ventas"],
        "hash_calendario": info["hash_calendario"],
        "bytes_ventas": info["bytes_ventas"],
        "bytes_calendario": info["bytes_calendario"],
        "carpeta_origen": info["carpeta"],
        "dias": dias,
        "columnas": info["columnas"],
        "contenido_validado": {
            k: contenido.get(k)
            for k in ("series", "productos", "tiendas", "estados", "categorias",
                      "departamentos", "suma_unidades", "celdas_positivas", "cobertura_ventas")
        },
        "cobertura": {
            "desde": validaciones["primer_dia"],
            "hasta": validaciones["ultimo_dia"],
            "dias": dias,
        },
        "conteos": {
            "series": series,
            "productos": len(productos),
            "tiendas": len(tiendas),
            "categorias": len(categorias),
            "departamentos": sum(len(v) for v in departamentos.values()),
        },
        "validaciones": validaciones,
        "calendario": {
            "compartido": calendario_compartido,
            "dias_de_calendario": len(fechas_todas),
            "dias_con_ventas": dias,
            "dias_calendario_sin_ventas": max(0, len(fechas_todas) - dias),
        },
        "parcial": bool(info.get("parcial")),
        "unidad": "unidades vendidas",
        "fuente": "Walmart M5 (Kaggle): sales_train_* + calendar",
    }
    db[M5_IMPORTACIONES].replace_one({"_id": version}, manifiesto, upsert=True)

    if publicar:
        if manifiesto["parcial"]:
            logger.warning("Versión parcial: no se publica como versión activa.")
        else:
            manifiesto["estado"] = "publicada"
            manifiesto["publicada"] = True
            db[M5_IMPORTACIONES].replace_one({"_id": version}, manifiesto, upsert=True)
            db[M5_ESTADO].replace_one(
                {"_id": "actual"},
                {
                    "_id": "actual",
                    "version": version,
                    "esquema": ESQUEMA_VERSION,
                    "actualizado": datetime.now(timezone.utc),
                    "cobertura": manifiesto["cobertura"],
                },
                upsert=True,
            )
    logger.info(
        "Importación %s: %s series, %s días, %s bloques (%s)",
        version,
        series,
        dias,
        validaciones["bloques"],
        "publicada" if manifiesto["publicada"] else "sin publicar",
    )
    return manifiesto


def _doc_bloque(version: str, clave: tuple[str, str, int], unidades: list[int], dias: int) -> dict:
    """Documento de ``m5_bloques``: tramo acotado de una serie producto × tienda."""
    item_id, store_id, indice = clave
    d_inicio = indice * fuentes.TAMANO_BLOQUE + 1
    d_fin = min(d_inicio + len(unidades) - 1, dias)
    return {
        "_id": f"{version}:{item_id}:{store_id}:{indice}",
        "esquema": ESQUEMA_VERSION,
        "version": version,
        "item_id": item_id,
        "store_id": store_id,
        "bloque": indice,
        "d_inicio": d_inicio,
        "d_fin": d_fin,
        "unidades": unidades,
        "total": sum(unidades),
    }


def version_actual(db: Database) -> str | None:
    """Versión publicada, o ``None`` si todavía no hay importación."""
    puntero = db[M5_ESTADO].find_one({"_id": "actual"})
    return None if not puntero else str(puntero.get("version"))


def _destino(db: Database) -> str | None:
    """``host:puerto/base`` del destino real, sin usuario ni contraseña."""
    try:
        direccion = db.client.address
        if not direccion:
            return None
        from app.comercial.conexion import describir

        return describir(f"mongodb://{direccion[0]}:{direccion[1]}/{db.name}")
    except Exception:  # pragma: no cover - informativo
        return None


def estado_publico(db: Database) -> dict:
    """Estado legible: versión activa, cobertura, conteos y si hay datos."""
    version = version_actual(db)
    if not version:
        return {
            "version": None,
            "con_datos": False,
            "estado": "pendiente",
            "mensaje": "Datos pendientes de importar",
            "detalle": (
                "Descarga sales_train_evaluation.csv y calendar.csv desde Kaggle "
                "(competición m5-forecasting-accuracy) en backend/data/m5 y ejecuta "
                "python -m scripts.importar_m5"
            ),
            "destino": _destino(db),
            "base": db.name,
        }
    manifiesto = db[M5_IMPORTACIONES].find_one({"_id": version}) or {}
    destino = _destino(db)
    return {
        "version": version,
        "con_datos": True,
        "estado": manifiesto.get("estado"),
        "cobertura": manifiesto.get("cobertura"),
        "conteos": manifiesto.get("conteos"),
        "validaciones": manifiesto.get("validaciones"),
        "importada_en": manifiesto.get("importada_en"),
        "unidad": manifiesto.get("unidad"),
        "esquema": manifiesto.get("esquema"),
        "calendario": manifiesto.get("calendario"),
        "destino": destino,
        "base": db.name,
    }


def colecciones_datos() -> tuple[str, ...]:
    """Colecciones que pertenecen a este proyecto (para inventario y limpieza)."""
    return (M5_CALENDARIO, M5_CATALOGO, M5_AGREGADOS, M5_BLOQUES, M5_PRONOSTICOS, M5_EVALUACION)


def limpiar_version(db: Database, version: str) -> None:
    """Borra los documentos de una versión concreta (no toca otras)."""
    patron = {"_id": {"$regex": f"^{version}:"}}
    for nombre in (M5_AGREGADOS, M5_BLOQUES, M5_CATALOGO):
        db[nombre].delete_many(patron)
    db[M5_PRONOSTICOS].delete_many({"version": version})
    db[M5_EVALUACION].delete_many({"version": version})
    db[M5_DERIVADOS].delete_many({"version": version})
    db[M5_IMPORTACIONES].delete_one({"_id": version})
