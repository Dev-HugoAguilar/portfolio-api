"""Baseline de pronóstico y evaluación fuera de muestra (Walmart M5).

Método (documentado, sin adornos)
---------------------------------
**Ingenuo estacional semanal.** Para un origen ``O`` (último día con observación
disponible) y un horizonte de 28 días::

    pred[O + h] = observado[O - 7 + ((h - 1) mod 7)]      h = 1..28

Es decir, se repite el patrón de los **últimos siete días observados**. El modelo
solo ve datos hasta ``O``: no se completa ningún día con valores reales futuros.

Esto **no** es una evaluación rolling de un día con actualización diaria: cada
ventana entrena una vez, pronostica 28 días y se evalúa contra lo observado. La
siguiente ventana empieza 28 días después (o antes, si se configura), y su origen
vuelve a ser el último día disponible **en ese momento**.

Qué se pronostica en esta entrega
---------------------------------
Series diarias de **unidades vendidas** en tres niveles, todos calculados de
verdad y verificados en las pruebas:

- ``total``     — suma de las diez tiendas.
- ``tienda``    — una serie por tienda (``CA_1``…``WI_3``).
- ``categoria`` — una serie por categoría (``FOODS``, ``HOBBIES``, ``HOUSEHOLD``).

Métricas
--------
- ``MAE``  — media de |error|.
- ``sesgo`` — media de (pronóstico − observado); positivo = sobrestimación.
- ``WAPE`` — Σ|error| / Σobservado × 100, solo si el denominador es > 0.

En los agregados las métricas se calculan **desde los errores y denominadores
sumados**, nunca como promedio de porcentajes.

No se declara WRMSSE: la jerarquía completa, el escalamiento y las ponderaciones
oficiales de M5 no están implementados. No hay intervalos de predicción porque no
se han calculado ni evaluado.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from pymongo.database import Database

from app.comercial import ESQUEMA_VERSION
from app.comercial import fuentes
from app.db.mongo import M5_AGREGADOS, M5_CALENDARIO, M5_EVALUACION, M5_PRONOSTICOS

logger = logging.getLogger(__name__)

NIVELES = ("total", "tienda", "categoria")
"""Niveles calculados en esta entrega."""

VENTANAS_POR_OMISION = 6
"""Orígenes evaluados. Se separan 28 días para no solapar el horizonte."""

MINIMO_HISTORIA = 8
"""Días observados necesarios antes del origen (7 del patrón + 1 de margen)."""


def _dt(fecha: date) -> datetime:
    """Fecha comercial → ``datetime`` ingenuo a medianoche (lo que guarda Mongo)."""
    return datetime(fecha.year, fecha.month, fecha.day)


def _series_de_agregados(db: Database, version: str) -> dict[tuple[str, str], list[int | None]]:
    """Series diarias por nivel desde ``m5_agregados`` (vector alineado)."""
    series: dict[tuple[str, str], list[int | None]] = {}
    for documento in db[M5_AGREGADOS].find(
        {"version": version, "dimension": {"$in": list(NIVELES)}}, {"dimension": 1, "clave": 1, "unidades": 1}
    ):
        series[(documento["dimension"], documento["clave"])] = list(documento.get("unidades") or [])
    return series


def _fechas(db: Database) -> dict[int, date]:
    """Mapa ``d_N`` → fecha desde el calendario importado."""
    mapa: dict[int, date] = {}
    for documento in db[M5_CALENDARIO].find({}, {"d": 1, "fecha": 1}):
        valor = documento["fecha"]
        mapa[int(documento["d"])] = valor.date() if isinstance(valor, datetime) else valor
    return mapa


def ventanas_disponibles(
    fechas: dict[int, date], horizonte: int, cantidad: int, ultimo_con_ventas: int | None = None
) -> list[int]:
    """Orígenes (``d``) evaluables, del más antiguo al más reciente.

    Cada origen necesita ``horizonte`` días posteriores **observados**; el último
    origen evaluable es, por tanto, ``último_d - horizonte``. ``último_d`` es el
    último día con ventas, no el último del calendario: M5 publica 1969 días de
    calendario y solo 1941 con ventas, y esos 28 días no se pueden evaluar.
    """
    if not fechas:
        return []
    ultimo = min(max(fechas), ultimo_con_ventas) if ultimo_con_ventas else max(fechas)
    primero = min(fechas)
    if ultimo_con_ventas:
        primero = min(primero, max(1, ultimo_con_ventas - horizonte))
    candidatos: list[int] = []
    origen = ultimo - horizonte
    while origen >= primero + MINIMO_HISTORIA - 1 and len(candidatos) < cantidad:
        candidatos.append(origen)
        origen -= horizonte
    return sorted(candidatos)


def predecir(serie: list[int | None], origen: int, horizonte: int) -> list[int]:
    """Pronóstico ingenuo estacional semanal a partir de ``origen`` (1-based).

    Solo usa ``serie[:origen]``: el futuro no interviene. Si falta algún día del
    patrón, la posición se marca como no calculable en lugar de inventar un valor.
    """
    if origen < fuentes.DIAS_SEMANA_PATRON or origen > len(serie):
        raise ValueError("Origen fuera de la serie")
    patron = serie[origen - fuentes.DIAS_SEMANA_PATRON : origen]
    if len(patron) != fuentes.DIAS_SEMANA_PATRON:
        raise ValueError("No hay siete días observados antes del origen")
    salida: list[int] = []
    for paso in range(horizonte):
        base = patron[paso % fuentes.DIAS_SEMANA_PATRON]
        if base is None:
            raise ValueError("El patrón contiene días sin observación")
        salida.append(max(0, int(round(base))))
    return salida


def metricas(pronostico: list[int], observado: list[int | None]) -> dict:
    """MAE, sesgo y WAPE calculados desde errores y denominadores.

    Los días sin observación se excluyen y se informan; no se imputan.
    """
    errores: list[int] = []
    absolutos: list[int] = []
    suma_observado = 0
    for pred, real in zip(pronostico, observado, strict=False):
        if real is None:
            continue
        error = pred - real
        errores.append(error)
        absolutos.append(abs(error))
        suma_observado += real
    n = len(errores)
    if n == 0:
        return {
            "n": 0,
            "mae": None,
            "sesgo": None,
            "wape": None,
            "denominador": 0,
            "denominador_cero": True,
            "nota": "Sin días observados en la ventana de evaluación.",
        }
    return {
        "n": n,
        "mae": round(sum(absolutos) / n, 3),
        "sesgo": round(sum(errores) / n, 3),
        "wape": round(100 * sum(absolutos) / suma_observado, 3) if suma_observado > 0 else None,
        "denominador": suma_observado,
        "denominador_cero": suma_observado == 0,
        "nota": (
            "WAPE no calculable: el denominador del periodo es cero."
            if suma_observado == 0
            else "MAE y sesgo en unidades/día; sesgo = pronóstico − observado."
        ),
    }


def agregar_metricas(ventanas: list[dict]) -> dict:
    """Métricas del conjunto de ventanas, desde errores sumados.

    No se promedian porcentajes: se suman los errores absolutos y los
    denominadores de todas las ventanas y se dividen una sola vez.
    """
    errores = sum(ventana["metricas"]["n"] for ventana in ventanas)
    absolutos = sum(abs(ventana["_suma_abs"]) for ventana in ventanas)
    con_signo = sum(ventana["_suma_error"] for ventana in ventanas)
    denominador = sum(ventana["metricas"]["denominador"] for ventana in ventanas)
    ventanas_sin_base = sum(1 for ventana in ventanas if ventana["metricas"]["denominador_cero"])
    if errores == 0:
        return {"n": 0, "mae": None, "sesgo": None, "wape": None, "denominador": 0,
                "ventanas_sin_base": ventanas_sin_base}
    return {
        "n": errores,
        "mae": round(absolutos / errores, 3),
        "sesgo": round(con_signo / errores, 3),
        "wape": round(100 * absolutos / denominador, 3) if denominador > 0 else None,
        "denominador": denominador,
        "ventanas": len(ventanas),
        "ventanas_sin_base": ventanas_sin_base,
    }


def calcular(
    db: Database,
    version: str,
    *,
    horizonte: int = fuentes.DIAS_HORIZONTE,
    ventanas: int = VENTANAS_POR_OMISION,
    publicar: bool = True,
) -> dict:
    """Calcula backtesting, pronóstico final y métricas; los guarda versionados."""
    series = _series_de_agregados(db, version)
    fechas = _fechas(db)
    if not series or not fechas:
        raise RuntimeError("No hay agregados ni calendario importados para esta versión.")

    # Los vectores de los agregados empiezan en d_1: su longitud es el último día
    # con ventas observadas (no el último del calendario, que trae 28 días más).
    ultimo = max((len(serie) for serie in series.values()), default=0)
    if ultimo < MINIMO_HISTORIA + horizonte:
        raise RuntimeError(
            "La cobertura importada no alcanza para una ventana de evaluación "
            f"(se necesitan {MINIMO_HISTORIA + horizonte} días con ventas y hay {ultimo})."
        )

    origenes = ventanas_disponibles(fechas, horizonte, ventanas, ultimo_con_ventas=ultimo)
    if not origenes:
        raise RuntimeError("La cobertura importada no permite ninguna ventana de evaluación.")

    documentos_pronostico: list[dict] = []
    evaluaciones: list[dict] = []

    for nivel in NIVELES:
        claves = sorted(clave for (dim, clave) in series if dim == nivel)
        ventanas_nivel: list[dict] = []
        for origen in origenes:
            detalle: list[dict] = []
            suma_abs = 0
            suma_error = 0
            for clave in claves:
                serie = series[(nivel, clave)]
                try:
                    pred = predecir(serie, origen, horizonte)
                except ValueError:
                    continue
                reales = serie[origen : origen + horizonte]
                metrica = metricas(pred, reales)
                for prediccion, real in zip(pred, reales, strict=False):
                    if real is None:
                        continue
                    suma_abs += abs(prediccion - real)
                    suma_error += prediccion - real
                detalle.append(
                    {
                        "clave": clave,
                        "pronostico": pred,
                        "observado": [None if v is None else int(v) for v in reales],
                        "metricas": metrica,
                    }
                )
            if not detalle:
                continue
            consolidado = {
                "nivel": nivel,
                "origen_d": origen,
                "origen_fecha": fechas[origen].isoformat(),
                "fin_fecha": fechas[min(origen + horizonte, max(fechas))].isoformat(),
                "horizonte": horizonte,
                "series": len(detalle),
                "metricas": {},
                "_suma_abs": suma_abs,
                "_suma_error": suma_error,
            }
            # Solo se consolidan las posiciones con pronóstico y observación completos;
            # si la ventana quedara corta, se recorta en vez de fallar.
            pasos = min([horizonte, *(len(d["observado"]) for d in detalle)])
            total_pred = [sum(d["pronostico"][i] for d in detalle) for i in range(pasos)]
            total_real = [
                sum(d["observado"][i] for d in detalle)
                if all(d["observado"][i] is not None for d in detalle)
                else None
                for i in range(pasos)
            ]
            consolidado["metricas"] = metricas(total_pred, total_real)
            ventanas_nivel.append(consolidado)

            documentos_pronostico.append(
                {
                    "_id": f"{version}:{nivel}:{origen}",
                    "esquema": ESQUEMA_VERSION,
                    "version": version,
                    "tipo": "backtesting",
                    "nivel": nivel,
                    "origen_d": origen,
                    "origen_fecha": _dt(fechas[origen]),
                    "horizonte": horizonte,
                    "unidad": "unidades vendidas",
                    "metodo": "ingenuo estacional semanal",
                    "entrenamiento": {
                        "desde": _dt(fechas[min(fechas)]),
                        "hasta": _dt(fechas[origen]),
                        "dias": origen,
                    },
                    "detalle": detalle,
                }
            )

        if ventanas_nivel:
            resumen = agregar_metricas(ventanas_nivel)
            evaluaciones.append(
                {
                    "_id": f"{version}:{nivel}",
                    "esquema": ESQUEMA_VERSION,
                    "version": version,
                    "nivel": nivel,
                    "horizonte": horizonte,
                    "metodo": "ingenuo estacional semanal",
                    "unidad": "unidades vendidas",
                    "resumen": resumen,
                    "definiciones": {
                        "mae": "media de |pronóstico − observado| en unidades por día",
                        "sesgo": "media de (pronóstico − observado); positivo = sobrestimación",
                        "wape": "100 × Σ|error| / Σobservado (solo si Σobservado > 0)",
                        "agregado": "calculado desde errores y denominadores sumados, no promedio de porcentajes",
                    },
                    "ventanas": [
                        {clave: valor for clave, valor in ventana.items() if not clave.startswith("_")}
                        for ventana in ventanas_nivel
                    ],
                    "calculada_en": datetime.now(timezone.utc),
                }
            )

    # --- Pronóstico desde la última fecha disponible (sin futuro conocido) -----
    for nivel in NIVELES:
        claves = sorted(clave for (dim, clave) in series if dim == nivel)
        detalle_final: list[dict] = []
        for clave in claves:
            serie = series[(nivel, clave)]
            try:
                pred = predecir(serie, ultimo, horizonte)
            except ValueError:
                continue
            detalle_final.append({"clave": clave, "pronostico": pred})
        if not detalle_final:
            continue
        dias = [
            {"d": ultimo + paso, "fecha": (fechas[ultimo] + timedelta(days=paso)).isoformat()}
            for paso in range(1, horizonte + 1)
        ]
        documentos_pronostico.append(
            {
                "_id": f"{version}:{nivel}:final",
                "esquema": ESQUEMA_VERSION,
                "version": version,
                "tipo": "futuro",
                "nivel": nivel,
                "origen_d": ultimo,
                "origen_fecha": _dt(fechas[ultimo]),
                "horizonte": horizonte,
                "unidad": "unidades vendidas",
                "metodo": "ingenuo estacional semanal",
                "nota": (
                    "Pronóstico desde el último día observado: no existen valores reales "
                    "posteriores en los datos importados."
                ),
                "dias": dias,
                "detalle": detalle_final,
            }
        )

    if publicar:
        db[M5_PRONOSTICOS].delete_many({"version": version})
        db[M5_EVALUACION].delete_many({"version": version})
        if documentos_pronostico:
            db[M5_PRONOSTICOS].insert_many(documentos_pronostico, ordered=False)
        if evaluaciones:
            db[M5_EVALUACION].insert_many(evaluaciones, ordered=False)

    resultado = {
        "version": version,
        "ventanas_evaluadas": origenes,
        "pronosticos": len(documentos_pronostico),
        "evaluaciones": [
            {"nivel": ev["nivel"], "resumen": ev["resumen"], "ventanas": len(ev["ventanas"])}
            for ev in evaluaciones
        ],
    }
    logger.info("Baseline calculado: %s", resultado["evaluaciones"])
    return resultado
