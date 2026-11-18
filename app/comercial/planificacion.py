"""Planificación de ventas a cuatro semanas y evaluación al horizonte de 28 días.

Todo sale de los derivados publicados (``m5_planificacion`` y ``m5_evaluacion_series``),
generados con ``scripts/derivar_planificacion.py`` a partir del modelo y las ventas ya
importados. Este módulo **no entrena** ni reimporta: ordena, pagina, agrega y explica.

Tres granos que nunca se mezclan:

- **diario por serie**: el error del pronóstico día a día (pantalla «Evaluación»);
- **horizonte de 28 días por serie**: se suman primero el observado y el pronóstico del
  horizonte y solo después se restan (aquí);
- **total agregado de todas las series**: suma de todas las series por día.

Reglas del cálculo al horizonte:

    observado_28  = Σ observado(día 1..28)
    pronostico_28 = Σ pronóstico(día 1..28)
    error_28      = pronostico_28 − observado_28
    error_abs_28  = |error_28|

Nunca se obtiene ``error_28`` sumando errores absolutos diarios.
"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database

from app.db.mongo import (
    M5_BLOQUES,
    M5_CALENDARIO,
    M5_CATALOGO,
    M5_EVALUACION_SERIES,
    M5_PLANIFICACION,
    M5_PREDICCIONES_SERIES,
    M5_PUBLICACION,
)

UNIDAD = "unidades vendidas"


def publicacion_activa(db: Database) -> dict | None:
    """Revisión de publicación activa (política de precisión incluida)."""
    return db[M5_PUBLICACION].find_one({"activa": True})


#: Texto único de la política de precisión. Lo usan todas las respuestas y pantallas.
POLITICA_PRECISION = (
    "decimal-v1: la serie canónica conserva decimales; el redondeo se aplica solo al presentar y "
    "los agregados suman la serie decimal, no cifras ya redondeadas."
)
POLITICA_ENTEROS = (
    "enteros-por-dia: la publicación guarda un entero por día; las semanas y los agregados suman "
    "cifras ya redondeadas."
)


def texto_politica(publicacion: dict | None) -> str:
    """Política de precisión declarada por la publicación activa."""
    if (publicacion or {}).get("politica") == "enteros-por-dia":
        return POLITICA_ENTEROS
    return POLITICA_PRECISION


def revision_para(db: Database, revision: str | None = None) -> str | None:
    """Revisión a consultar: la pedida, la activa, o ``None`` (documentos históricos)."""
    if revision:
        return revision
    activa = publicacion_activa(db)
    return activa["_id"] if activa else None


def _filtro_revision(base: dict, revision: str | None) -> dict:
    """Añade el filtro de revisión; sin revisión se leen los documentos históricos."""
    if revision:
        return {**base, "revision": revision}
    return {**base, "revision": {"$exists": False}}
HORIZONTE = 28
SEMANAS = ("Semana 1 · días 1–7", "Semana 2 · días 8–14", "Semana 3 · días 15–21", "Semana 4 · días 22–28")

#: Órdenes admitidos y su clave en Mongo. El desempate estable va al final.
ORDENES: dict[str, list[tuple[str, int]]] = {
    "volumen_previsto": [("pronostico_28", DESCENDING)],
    "aumento_absoluto": [("diferencia", DESCENDING)],
    "disminucion_absoluta": [("diferencia", ASCENDING)],
    "aumento_porcentual": [("variacion_pct", DESCENDING)],
    "disminucion_porcentual": [("variacion_pct", ASCENDING)],
    "volumen_observado": [("observado_28", DESCENDING)],
}
DESEMPATE = [("store_id", ASCENDING), ("item_id", ASCENDING)]

ESTADOS_EVALUACION = {
    "menor_error": "Menor error que el baseline en los cortes disponibles",
    "mayor_error": "Mayor error que el baseline",
    "mixto": "Resultado mixto entre cortes",
    "empate": "Empate con el baseline",
    "sin_evaluacion": "Sin evaluación suficiente para calcular la comparación",
}


def _consulta(
    *,
    tiendas: list[str] | None,
    categorias: list[str] | None,
    departamentos: list[str] | None,
    minimo_observado: float | None,
    texto: str | None,
) -> dict:
    consulta: dict[str, Any] = {}
    if tiendas:
        consulta["store_id"] = {"$in": list(tiendas)}
    if categorias:
        consulta["cat_id"] = {"$in": list(categorias)}
    if departamentos:
        consulta["dept_id"] = {"$in": list(departamentos)}
    if minimo_observado is not None:
        consulta["observado_28"] = {"$gte": float(minimo_observado)}
    if texto:
        patron = {"$regex": _escapar(texto.strip()), "$options": "i"}
        consulta["$or"] = [{"item_id": patron}, {"cat_id": patron}, {"dept_id": patron}, {"store_id": patron}]
    return consulta


def _escapar(texto: str) -> str:
    return "".join("\\" + caracter if caracter in "\\^$.|?*+()[]{}" else caracter for caracter in texto)


def _fila(documento: dict) -> dict:
    semanas = [float(valor or 0) for valor in documento.get("semanas") or [0, 0, 0, 0]]
    semanas_base = [float(valor or 0) for valor in documento.get("semanas_baseline") or [0, 0, 0, 0]]
    semanas_observado = [float(valor or 0) for valor in documento.get("semanas_observado") or [0, 0, 0, 0]]
    pronostico = float(documento.get("pronostico_28") or 0)
    observado = float(documento.get("observado_28") or 0)
    evaluacion = documento.get("evaluacion") or {}
    return {
        "item_id": documento["item_id"],
        "store_id": documento["store_id"],
        "cat_id": documento.get("cat_id"),
        "dept_id": documento.get("dept_id"),
        "observado_28": observado,
        "pronostico_28": pronostico,
        "baseline_28": float(documento.get("baseline_28") or 0),
        "diferencia": float(documento.get("diferencia") or 0),
        "variacion_pct": documento.get("variacion_pct"),
        "semanas": semanas,
        "semanas_baseline": semanas_base,
        "semanas_observado": semanas_observado,
        "suma_semanas": sum(semanas),
        "evaluacion": {
            "estado": evaluacion.get("estado", "sin_evaluacion"),
            "cortes": int(evaluacion.get("cortes") or 0),
            "mejoran": int(evaluacion.get("mejoran") or 0),
            "empeoran": int(evaluacion.get("empeoran") or 0),
            "empatan": int(evaluacion.get("empatan") or 0),
        },
    }


def _tiendas_del_conjunto(db: Database, base: dict, filtros: dict) -> list[str]:
    """Tiendas presentes en el **conjunto filtrado completo**, no en la página visible.

    Se calcula con una agregación sobre el mismo filtro que la tabla, así que con todas las
    tiendas devuelve las 10 aunque la página muestre 25 filas.
    """
    return sorted(
        str(valor)
        for valor in db[M5_PLANIFICACION].distinct("store_id", {**base, **filtros})
        if valor
    )


def _resumen(db: Database, base: dict, filtros: dict) -> dict:
    """Totales del **conjunto filtrado completo**, no de la página visible."""
    pipeline = [
        {"$match": {**base, **filtros}},
        {
            "$group": {
                "_id": None,
                "series": {"$sum": 1},
                "pronostico_28": {"$sum": "$pronostico_28"},
                "observado_28": {"$sum": "$observado_28"},
                "baseline_28": {"$sum": "$baseline_28"},
                "con_evaluacion": {
                    "$sum": {"$cond": [{"$gt": ["$evaluacion.cortes", 0]}, 1, 0]}
                },
                "sin_base": {
                    "$sum": {"$cond": [{"$lte": ["$observado_28", 0]}, 1, 0]}
                },
            }
        },
    ]
    resultado = next(iter(db[M5_PLANIFICACION].aggregate(pipeline)), None) or {
        "series": 0,
        "pronostico_28": 0.0,
        "observado_28": 0.0,
        "baseline_28": 0.0,
        "con_evaluacion": 0,
        "sin_base": 0,
    }
    pronostico = float(resultado.get("pronostico_28") or 0)
    observado = float(resultado.get("observado_28") or 0)
    return {
        "series": int(resultado.get("series") or 0),
        "pronostico_28": pronostico,
        "observado_28": observado,
        "baseline_28": float(resultado.get("baseline_28") or 0),
        "diferencia": pronostico - observado,
        "variacion_pct": None if observado <= 0 else 100 * (pronostico - observado) / observado,
        "series_con_evaluacion": int(resultado.get("con_evaluacion") or 0),
        "series_sin_base": int(resultado.get("sin_base") or 0),
    }


def planificacion(
    db: Database,
    version: str,
    run_id: str,
    *,
    tiendas: list[str] | None = None,
    categorias: list[str] | None = None,
    departamentos: list[str] | None = None,
    texto: str | None = None,
    orden: str = "volumen_previsto",
    minimo_observado: float | None = None,
    pagina: int = 1,
    tamano: int = 25,
    revision: str | None = None,
) -> dict:
    """Página de la tabla de planificación, con totales del conjunto filtrado."""
    revision = revision_para(db, revision)
    base = _filtro_revision({"version": version, "run_id": run_id}, revision)
    filtros = _consulta(
        tiendas=tiendas,
        categorias=categorias,
        departamentos=departamentos,
        minimo_observado=minimo_observado,
        texto=texto,
    )
    clave_orden = orden if orden in ORDENES else "volumen_previsto"

    # En los órdenes porcentuales los registros sin base van al final: se explican, no se ocultan.
    consulta = dict(filtros)
    if clave_orden in {"aumento_porcentual", "disminucion_porcentual"}:
        consulta["variacion_pct"] = {"$ne": None}

    orden_mongo = ORDENES[clave_orden] + DESEMPATE
    total = db[M5_PLANIFICACION].count_documents({**base, **filtros})
    con_orden = db[M5_PLANIFICACION].count_documents({**base, **consulta})
    documentos = list(
        db[M5_PLANIFICACION]
        .find({**base, **consulta}, {"_id": 0})
        .sort(orden_mongo)
        .skip(max(0, (pagina - 1) * tamano))
        .limit(tamano)
    )
    referencia = next(
        iter(
            db[M5_PLANIFICACION].find({**base, **filtros}, {"origen_fecha": 1, "horizonte_inicio": 1, "horizonte_fin": 1, "observado_desde": 1, "observado_hasta": 1, "_id": 0}).limit(1)
        ),
        {},
    )
    activa = publicacion_activa(db) or {}
    return {
        "version": version,
        "run_id": run_id,
        "revision": revision,
        "revision_politica": activa.get("politica"),
        "revision_anterior": activa.get("anterior"),
        "politica_precision": texto_politica(activa),
        "tiendas_contexto": _tiendas_del_conjunto(db, base, filtros),
        "unidad": UNIDAD,
        "origen_fecha": referencia.get("origen_fecha"),
        "horizonte_inicio": referencia.get("horizonte_inicio"),
        "horizonte_fin": referencia.get("horizonte_fin"),
        "observado_desde": referencia.get("observado_desde"),
        "observado_hasta": referencia.get("observado_hasta"),
        "orden": clave_orden,
        "orden_aplicado": [{"campo": campo, "sentido": "desc" if sentido < 0 else "asc"} for campo, sentido in ORDENES[clave_orden]],
        "desempate": "tienda y artículo ascendente",
        "filtros": {
            "tiendas": tiendas or [],
            "categorias": categorias or [],
            "departamentos": departamentos or [],
            "texto": texto or None,
            "minimo_observado": minimo_observado,
        },
        "pagina": pagina,
        "tamano": tamano,
        "total": total,
        "paginas": max(1, (max(total, 1) + tamano - 1) // tamano),
        "filas_ordenables": con_orden,
        "excluidas_por_orden": total - con_orden if clave_orden in {"aumento_porcentual", "disminucion_porcentual"} else 0,
        "semanas": list(SEMANAS),
        "semanas_fechas": _semanas_fechas(db, version, referencia),
        "resumen": _resumen(db, base, filtros),
        "filas": [_fila(documento) for documento in documentos],
        "nota": (
            "Las semanas son bloques consecutivos de siete días del horizonte (1–7, 8–14, 15–21, "
            "22–28), no semanas ISO. Los totales corresponden al conjunto filtrado completo. Las "
            "predicciones se suman sin redondear; la presentación redondea a entero."
        ),
    }


def planificacion_completa(
    db: Database,
    version: str,
    run_id: str,
    *,
    tiendas: list[str] | None = None,
    categorias: list[str] | None = None,
    departamentos: list[str] | None = None,
    texto: str | None = None,
    orden: str = "volumen_previsto",
    minimo_observado: float | None = None,
    limite: int = 5000,
    revision: str | None = None,
) -> dict:
    """Filas del filtro completo para la descarga (sin paginar), con su recuento."""
    revision = revision_para(db, revision)
    base = _filtro_revision({"version": version, "run_id": run_id}, revision)
    filtros = _consulta(
        tiendas=tiendas,
        categorias=categorias,
        departamentos=departamentos,
        minimo_observado=minimo_observado,
        texto=texto,
    )
    clave_orden = orden if orden in ORDENES else "volumen_previsto"
    total = db[M5_PLANIFICACION].count_documents({**base, **filtros})
    documentos = list(
        db[M5_PLANIFICACION]
        .find({**base, **filtros}, {"_id": 0})
        .sort(ORDENES[clave_orden] + DESEMPATE)
        .limit(min(limite, 20000))
    )
    recortado = total > len(documentos)
    activa = publicacion_activa(db) or {}
    return {
        "version": version,
        "run_id": run_id,
        "revision": revision,
        "revision_politica": activa.get("politica"),
        "politica_precision": activa.get("precision"),
        "unidad": UNIDAD,
        "orden": clave_orden,
        "semanas": list(SEMANAS),
        "total": total,
        "incluidas": len(documentos),
        "recortado": recortado,
        "resumen": _resumen(db, base, filtros),
        "filas": [_fila(documento) for documento in documentos],
        "nota": (
            "Descarga del conjunto filtrado completo; no incluye filas de totales para no "
            "confundirlas con artículos."
            if not recortado
            else f"El filtro tiene {total} series y la descarga incluye las primeras {len(documentos)} por el orden activo."
        ),
    }


def _ventas_serie(db: Database, version: str, tienda: str, item: str, *, dias: int, hasta_d: int | None = None) -> list[dict]:
    """Histórico diario de una serie desde los bloques de venta (sin traer todo al cliente)."""
    documentos = list(
        db[M5_BLOQUES].find(
            {"version": version, "store_id": tienda, "item_id": item},
            {"unidades": 1, "d_inicio": 1, "d_fin": 1, "_id": 0},
        )
    )
    if not documentos:
        return []
    dias_venta: dict[int, float] = {}
    for documento in documentos:
        inicio = int(documento["d_inicio"])
        for posicion, valor in enumerate(documento.get("unidades") or []):
            dias_venta[inicio + posicion] = float(valor)
    fechas = _mapa_fechas(db, version)
    total_dias = max(dias_venta) if dias_venta else 0
    tope = min(hasta_d or total_dias, total_dias)
    desde = max(1, tope - dias + 1)
    return [
        {"d": dia, "fecha": fechas.get(dia), "unidades": dias_venta.get(dia, 0.0)}
        for dia in range(desde, tope + 1)
        if fechas.get(dia)
    ]


def _semanas_fechas(db: Database, version: str, referencia: dict) -> list[dict]:
    """Fechas de los cuatro bloques de siete días del horizonte.

    Son bloques consecutivos del horizonte (días 1–7, 8–14, 15–21 y 22–28), no semanas ISO:
    cuando el corte no cae en domingo no coinciden con el calendario semanal.
    """
    fechas = _mapa_fechas(db, version)
    inicio = referencia.get("horizonte_inicio")
    if not inicio:
        return []
    dias = {fecha: numero for numero, fecha in fechas.items()}
    desde_d = dias.get(inicio)
    if not desde_d:
        return []
    return [
        {
            "etiqueta": SEMANAS[posicion],
            "desde": fechas.get(desde_d + posicion * 7),
            "hasta": fechas.get(desde_d + posicion * 7 + 6),
        }
        for posicion in range(4)
    ]


def _mapa_fechas(db: Database, version: str) -> dict[int, str]:
    """``d_N`` → fecha ISO del calendario compartido (no lleva `version`).

    Se toma solo el día (`AAMMDD` → `AAAA-MM-DD`) del campo `fecha`, sin pasar por zona
    horaria: así ninguna fecha se desplaza un día al presentarla.
    """
    del version  # el calendario es compartido entre versiones
    return {
        int(documento["d"]): documento["fecha"].date().isoformat()
        for documento in db[M5_CALENDARIO].find({}, {"d": 1, "fecha": 1, "_id": 0})
        if documento.get("fecha") is not None
    }


def detalle_serie(
    db: Database,
    version: str,
    run_id: str,
    *,
    tienda: str,
    item: str,
    dias_historico: int = 90,
    revision: str | None = None,
) -> dict | None:
    """Detalle de una serie: histórico reciente, pronóstico diario, semanas y evaluación a 28 días."""
    revision = revision_para(db, revision)
    plan = db[M5_PLANIFICACION].find_one(
        _filtro_revision(
            {"version": version, "run_id": run_id, "store_id": tienda, "item_id": item}, revision
        ),
        {"_id": 0},
    )
    if not plan:
        return None
    prediccion = db[M5_PREDICCIONES_SERIES].find_one(
        _filtro_revision(
            {"run_id": run_id, "store_id": tienda, "item_id": item}, revision
        ),
        {"_id": 0},
    )
    fechas = _mapa_fechas(db, version)
    origen_d = int(plan["origen_d"])
    historico = _ventas_serie(db, version, tienda, item, dias=dias_historico, hasta_d=origen_d)
    pronostico = (prediccion or {}).get("pronostico") or []
    baseline = (prediccion or {}).get("baseline") or []
    dias = [
        {
            "d": origen_d + paso + 1,
            "fecha": fechas.get(origen_d + paso + 1),
            "pronostico": float(pronostico[paso]) if paso < len(pronostico) else None,
            "baseline": float(baseline[paso]) if paso < len(baseline) else None,
            "semana": paso // 7 + 1,
        }
        for paso in range(HORIZONTE)
    ]
    # La revisión activa manda también aquí: los cortes de la publicación anterior siguen en la
    # base sin campo ``revision`` y, si se leyeran, cada ventana aparecería dos veces (una con
    # enteros y otra con decimales) y el detalle declararía el doble de cortes de los publicados.
    evaluacion = list(
        db[M5_EVALUACION_SERIES]
        .find(
            _filtro_revision(
                {"version": version, "run_id": run_id, "store_id": tienda, "item_id": item}, revision
            ),
            {"_id": 0},
        )
        .sort("origen_d", 1)
    )
    fila = _fila(plan)
    return {
        "version": version,
        "run_id": run_id,
        "revision": revision,
        "unidad": UNIDAD,
        "origen_fecha": plan["origen_fecha"],
        "horizonte_inicio": plan["horizonte_inicio"],
        "horizonte_fin": plan["horizonte_fin"],
        "observado_desde": plan["observado_desde"],
        "observado_hasta": plan["observado_hasta"],
        "serie": {
            "item_id": item,
            "store_id": tienda,
            "cat_id": plan.get("cat_id"),
            "dept_id": plan.get("dept_id"),
        },
        "fila": fila,
        "historico": historico,
        "pronostico_diario": dias,
        "semanas": [
            {
                "etiqueta": SEMANAS[posicion],
                "pronostico": fila["semanas"][posicion],
                "baseline": fila["semanas_baseline"][posicion],
                "observado_previo": fila["semanas_observado"][posicion],
                "desde": dias[posicion * 7]["fecha"],
                "hasta": dias[posicion * 7 + 6]["fecha"],
            }
            for posicion in range(4)
        ],
        "evaluacion_horizonte": [
            {
                "origen_d": registro["origen_d"],
                "origen_fecha": registro["origen_fecha"],
                "fin_fecha": registro["fin_fecha"],
                "observado_28": registro["observado_28"],
                "pronostico_28": registro["pronostico_28"],
                "error_28": registro["error_28"],
                "error_absoluto_28": registro["error_absoluto_28"],
                "baseline_28": registro["baseline_28"],
                "baseline_error_28": registro["baseline_error_28"],
                "mejor_que_baseline": abs(registro["error_28"]) < abs(registro["baseline_error_28"]),
            }
            for registro in evaluacion
        ],
        "evaluacion_resumen": fila["evaluacion"],
        "nota": (
            "Histórico y pronóstico son unidades vendidas de M5. El pronóstico es una simulación "
            "histórica: el corte es el fin del histórico publicado, no la fecha de hoy."
        ),
    }


def _resumen_evaluacion(documentos: list[dict]) -> dict:
    """MAE_28, WAPE_28 y sesgo con los errores del horizonte ya calculados por serie."""
    if not documentos:
        return {"series": 0, "cortes": 0, "mae_28": None, "wape_28": None, "sesgo_28": None, "denominador": 0.0, "no_calculable": True}
    absolutos = sum(abs(float(registro["error_28"])) for registro in documentos)
    baseline_absolutos = sum(abs(float(registro["baseline_error_28"])) for registro in documentos)
    observados = sum(float(registro["observado_28"]) for registro in documentos)
    sesgo = sum(float(registro["error_28"]) for registro in documentos)
    n = len(documentos)
    return {
        "series": len({(registro["store_id"], registro["item_id"]) for registro in documentos}),
        "registros": n,
        "cortes": len({registro["origen_d"] for registro in documentos}),
        "mae_28": absolutos / n,
        "wape_28": None if observados <= 0 else 100 * absolutos / observados,
        "sesgo_28": sesgo / n,
        "sesgo_total": sesgo,
        "denominador": observados,
        "mae_28_baseline": baseline_absolutos / n,
        "wape_28_baseline": None if observados <= 0 else 100 * baseline_absolutos / observados,
        "mejora_wape_pct": (
            None
            if observados <= 0 or baseline_absolutos <= 0
            else 100 * (baseline_absolutos - absolutos) / baseline_absolutos
        ),
        "no_calculable": observados <= 0,
    }


def evaluacion_horizonte(
    db: Database,
    version: str,
    run_id: str,
    *,
    tienda: str | None = None,
    categoria: str | None = None,
    departamento: str | None = None,
    revision: str | None = None,
) -> dict:
    """Evaluación al horizonte de 28 días con diagnóstico descriptivo por grupos."""
    revision = revision_para(db, revision)
    consulta: dict[str, Any] = _filtro_revision(
        {"version": version, "run_id": run_id}, revision
    )
    if tienda:
        consulta["store_id"] = tienda
    if categoria:
        consulta["cat_id"] = categoria
    if departamento:
        consulta["dept_id"] = departamento
    documentos = list(db[M5_EVALUACION_SERIES].find(consulta, {"_id": 0}))
    if not documentos:
        return {
            "version": version,
            "run_id": run_id,
            "unidad": UNIDAD,
            "disponible": False,
            "motivo": "Sin evaluación a 28 días derivada para esta versión y ejecución. Ejecuta scripts/derivar_planificacion.",
            "cortes": [],
            "consolidado": None,
            "por_estado": [],
            "por_categoria": [],
            "por_departamento": [],
            "por_volumen": [],
            "por_intermitencia": [],
            "estados": [],
            "cobertura": {"series": 0, "series_evaluadas": 0},
        }

    por_corte: dict[int, list[dict]] = {}
    for registro in documentos:
        por_corte.setdefault(int(registro["origen_d"]), []).append(registro)

    cortes = [
        {
            "origen_d": origen,
            "origen_fecha": registros[0]["origen_fecha"],
            "fin_fecha": registros[0]["fin_fecha"],
            "series": len(registros),
            **_resumen_evaluacion(registros),
        }
        for origen, registros in sorted(por_corte.items())
    ]

    def agrupar(campo: str) -> list[dict]:
        grupos: dict[str, list[dict]] = {}
        for registro in documentos:
            clave = registro.get(campo) or "—"
            grupos.setdefault(str(clave), []).append(registro)
        filas = []
        for clave, registros in sorted(grupos.items()):
            resumen = _resumen_evaluacion(registros)
            filas.append({"clave": clave, "series": resumen["series"], **resumen})
        return filas

    # Grupos descriptivos con información ANTERIOR al corte: volumen previo e intermitencia.
    def grupo_volumen(registro: dict) -> str:
        valor = float(registro.get("observado_previo_56") or 0)
        if valor <= 0:
            return "Sin ventas en las 8 semanas previas"
        if valor < 28:
            return "Menos de 1 unidad por día (8 semanas previas)"
        if valor < 140:
            return "1 a 5 unidades por día (8 semanas previas)"
        return "Más de 5 unidades por día (8 semanas previas)"

    def grupo_intermitencia(registro: dict) -> str:
        ceros = int(registro.get("ceros_previos_56") or 0)
        if ceros >= 49:
            return "Casi siempre en cero (≥49 de 56 días)"
        if ceros >= 28:
            return "Intermitente alto (28–48 de 56 días)"
        if ceros >= 7:
            return "Intermitente moderado (7–27 de 56 días)"
        return "Venta continua (≤6 de 56 días)"

    def agrupar_por(funcion) -> list[dict]:
        grupos: dict[str, list[dict]] = {}
        for registro in documentos:
            grupos.setdefault(funcion(registro), []).append(registro)
        return [
            {"clave": clave, "series": len(registros), **_resumen_evaluacion(registros)}
            for clave, registros in sorted(grupos.items())
        ]

    # Estado por serie: cuántos cortes la sustentan.
    estados: dict[tuple[str, str], dict] = {}
    for registro in documentos:
        clave = (registro["store_id"], registro["item_id"])
        actual = estados.setdefault(clave, {"mejoran": 0, "empeoran": 0, "empatan": 0, "cortes": 0})
        actual["cortes"] += 1
        if abs(registro["error_28"]) < abs(registro["baseline_error_28"]) - 1e-9:
            actual["mejoran"] += 1
        elif abs(registro["error_28"]) > abs(registro["baseline_error_28"]) + 1e-9:
            actual["empeoran"] += 1
        else:
            actual["empatan"] += 1
    recuento_estados: dict[str, int] = {clave: 0 for clave in ESTADOS_EVALUACION}
    for estado in estados.values():
        if estado["mejoran"] and not estado["empeoran"]:
            recuento_estados["menor_error"] += 1
        elif estado["empeoran"] and not estado["mejoran"]:
            recuento_estados["mayor_error"] += 1
        elif estado["mejoran"] and estado["empeoran"]:
            recuento_estados["mixto"] += 1
        elif estado["empatan"]:
            recuento_estados["empate"] += 1
        else:
            recuento_estados["sin_evaluacion"] += 1

    sobre = sum(1 for registro in documentos if float(registro["error_28"]) > 0)
    sub = sum(1 for registro in documentos if float(registro["error_28"]) < 0)
    exactos = sum(1 for registro in documentos if abs(float(registro["error_28"])) < 1e-9)
    total_series = int(
        db[M5_PLANIFICACION].count_documents(
            _filtro_revision({"version": version, "run_id": run_id}, revision)
        )
    )

    return {
        "version": version,
        "run_id": run_id,
        "revision": revision,
        "unidad": UNIDAD,
        "disponible": True,
        "granos": {
            "diario": "error del pronóstico día a día por serie (pantalla Evaluación, bloque diario)",
            "horizonte": "error del total de 28 días por serie y corte (este bloque)",
            "agregado": "error del total de todas las series por día (bloque de ventanas)",
        },
        "cortes": cortes,
        "consolidado": _resumen_evaluacion(documentos),
        "por_estado": agrupar("state_id") if "state_id" in documentos[0] else [],
        "por_categoria": agrupar("cat_id"),
        "por_departamento": agrupar("dept_id"),
        "por_volumen": agrupar_por(grupo_volumen),
        "por_intermitencia": agrupar_por(grupo_intermitencia),
        "sesgo": {"sobreestima": sobre, "subestima": sub, "exactos": exactos},
        "estados": [
            {
                "clave": clave,
                "etiqueta": etiqueta,
                "series": recuento_estados[clave],
            }
            for clave, etiqueta in ESTADOS_EVALUACION.items()
        ],
        "cobertura": {
            "series_universo": total_series,
            "series_evaluadas": len(estados),
            "pct_evaluadas": None if total_series <= 0 else 100 * len(estados) / total_series,
            "registros_usados": len(documentos),
        },
        "nota": (
            "El error del horizonte se calcula sumando primero observado y pronóstico de los 28 "
            "días y restando después; no se obtiene sumando errores diarios. Los grupos de volumen "
            "e intermitencia usan solo las 8 semanas anteriores a cada corte. Los estados son "
            "descriptivos: «menor error» no es una aprobación operativa ni una garantía futura."
        ),
    }
