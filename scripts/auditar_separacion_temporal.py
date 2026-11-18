"""Auditoría de la separación temporal entre entrenamiento y evaluación.

Reconstruye, para **cada ejecución y cada corte evaluado**, la evidencia disponible sobre qué
información se usó para ajustar el modelo y qué fechas se puntuaron. No entrena, no sustituye el
modelo publicado y no borra resultados: solo lee manifiestos, artefactos y registros de evaluación.

Qué comprueba (y de dónde sale cada dato):

* **Frontera de etiquetas**: el manifiesto declara los orígenes de entrenamiento (`m5_modelos.
  origenes_entrenamiento`) y el horizonte (`horizonte`). Una fila de entrenamiento con origen `t`
  usa como etiqueta el día `t + h` con `h = 1..H` (`app/comercial/modelo.py:431`), así que la
  última fecha de etiqueta es `max(origenes) + H`. Se comprueba esa fecha, no solo el origen.
* **Ventana evaluada**: el corte `c` se puntúa con los días `c+1 … c+H`
  (`scripts/entrenar_m5.py::_evaluar`), de modo que los objetivos deben ser posteriores a `c`.
* **Transformaciones**: rezagos, medias y desviaciones se calculan solo con días anteriores al
  origen de cada fila; las ventanas móviles no miran al futuro. El calendario del día objetivo sí
  se usa porque se conoce por adelantado. La variante con precios supone el último precio conocido.
* **Uso del corte**: los cortes de desarrollo comparan variantes y eligen con el menor WAPE
  (`scripts/entrenar_m5.py`, bloque «Variante recomendada»); los finales se consultaron para
  confirmar la publicación. Ninguno se usa para early stopping ni para hiperparámetros: el
  entrenamiento fija `num_boost_round` y no declara `callbacks` ni `valid_sets`
  (`app/comercial/modelo.py:669`).

Resultado por fila:

* ``separacion_verificada`` — la última etiqueta de entrenamiento no supera el corte y los
  objetivos evaluados son posteriores; o el modelo no entrena (baseline determinista).
* ``contaminada`` — hay etiquetas de entrenamiento posteriores al corte evaluado.
* ``no_verificable`` — falta evidencia en los registros para afirmar cualquiera de las dos cosas.

Uso (desde ``backend/``):

    python -m scripts.auditar_separacion_temporal            # informe + guarda la auditoría
    python -m scripts.auditar_separacion_temporal --json ruta.json
    python -m scripts.auditar_separacion_temporal --markdown ruta.md
    python -m scripts.auditar_separacion_temporal --solo-informe

Sale con código 1 si alguna evaluación resulta contaminada.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from app.comercial import modelo
from app.config import get_settings
from app.db.mongo import (
    M5_AUDITORIA_TEMPORAL,
    M5_EVALUACION_MODELOS,
    M5_MODELOS,
    init_database,
)

ESQUEMA = "m5-auditoria-temporal-1"

#: Origen de cada campo que se publica en la tabla. Sin esto la tabla no es auditable.
FUENTES = {
    "variante": "m5_modelos.variante",
    "run_id": "m5_modelos.run_id",
    "artefacto": "m5_modelos.artefacto",
    "origenes_entrenamiento": "m5_modelos.origenes_entrenamiento",
    "horizonte": "m5_modelos.horizonte",
    "num_boost_round": "m5_modelos.num_boost_round",
    "parametros": "m5_modelos.parametros",
    "metrica_recomendada": "m5_modelos.metricas_resumen.desarrollo.wape",
    "corte": "m5_evaluacion_modelos.origen_d",
    "fecha_corte": "m5_evaluacion_modelos.origen_fecha",
    "inicio_horizonte": "m5_evaluacion_modelos.origen_fecha (día siguiente)",
    "fin_horizonte": "m5_evaluacion_modelos.fin_fecha",
    "etapa": "m5_evaluacion_modelos.etapa",
    "serie_transformaciones": "app/comercial/modelo.py: construir_matriz (ventanas causales t ≤ origen)",
    "etiqueta_objetivo": "app/comercial/modelo.py:431 (objetivo = origen − 1 + h, h = 1..H)",
    "ventana_evaluada": "scripts/entrenar_m5.py::_evaluar (días origen+1 … origen+H)",
    "seleccion": "scripts/entrenar_m5.py, bloque «Variante recomendada»",
    "early_stopping": "app/comercial/modelo.py:669 (lgb.train sin callbacks ni valid_sets)",
}


def _fecha(valor) -> str | None:
    """Fecha ISO sin hora: ``datetime`` es subclase de ``date`` y arrastraba la hora."""
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    return str(valor)[:10]


def _dias_entre(desde: str | None, hasta: str | None) -> int | None:
    if not desde or not hasta:
        return None
    return (date.fromisoformat(hasta) - date.fromisoformat(desde)).days


def clasificar(
    *,
    variante: str,
    etapa: str,
    corte: int,
    horizonte: int,
    origenes: list[int],
    entrenado: bool,
) -> tuple[str, str, int | None]:
    """Clasifica una evaluación con la regla publicada.

    Devuelve ``(resultado, detalle, ultima_etiqueta)``. La regla se aísla aquí para poder probarla
    con casos sintéticos sin tocar la base:

    * sin entrenamiento y variante baseline → ``separacion_verificada`` (regla determinista);
    * sin evidencia de orígenes ni artefacto → ``no_verificable``;
    * con etiquetas (``max(origen) + horizonte``) posteriores al corte → ``contaminada``;
    * en el resto de los casos → ``separacion_verificada``.
    """
    ultima_etiqueta = (max(origenes) + horizonte) if origenes else None
    if not entrenado:
        if variante == "baseline":
            return (
                "separacion_verificada",
                "El baseline es una regla determinista sin parámetros entrenados: repite los siete "
                "días observados anteriores al corte y no usa ninguna etiqueta posterior.",
                None,
            )
        return (
            "no_verificable",
            "El manifiesto no declara orígenes de entrenamiento ni artefacto: los registros "
            "disponibles no permiten afirmar ni descartar fuga.",
            None,
        )
    if ultima_etiqueta is not None and ultima_etiqueta > corte:
        return (
            "contaminada",
            f"Las etiquetas de entrenamiento llegan a d_{ultima_etiqueta}, posterior al corte "
            f"d_{corte} (ventana d_{corte + 1}–d_{corte + horizonte}). La evaluación no es "
            "temporalmente independiente.",
            ultima_etiqueta,
        )
    return (
        "separacion_verificada",
        f"Última etiqueta de entrenamiento d_{ultima_etiqueta}; los objetivos evaluados son "
        f"d_{corte + 1}–d_{corte + horizonte}, todos posteriores al corte. Rezagos y medias móviles "
        "se calculan solo con días anteriores al origen de cada fila; el calendario del objetivo se "
        "conoce por adelantado.",
        ultima_etiqueta,
    )


def auditar(db) -> dict:
    """Reconstruye la tabla de evidencia temporal para todas las ejecuciones y cortes."""
    estado = db["m5_estado"].find_one({"publicada": True}) or db["m5_estado"].find_one()
    version = estado["version"] if estado else None
    if not version:
        raise SystemExit("Sin versión de datos publicada")

    runs = {
        documento["run_id"]: documento
        for documento in db[M5_MODELOS].find({"version": version})
    }
    filas: list[dict] = []
    for evaluacion in db[M5_EVALUACION_MODELOS].find({"version": version}).sort(
        [("run_id", 1), ("etapa", 1), ("origen_d", 1)]
    ):
        run_id = evaluacion["run_id"]
        manifiesto = runs.get(run_id)
        corte = int(evaluacion["origen_d"])
        horizonte = int((manifiesto or {}).get("horizonte") or 28)
        origenes = [int(valor) for valor in (manifiesto or {}).get("origenes_entrenamiento") or []]
        artefacto = (manifiesto or {}).get("artefacto")
        variante = (manifiesto or {}).get("variante") or evaluacion.get("variante") or "desconocida"

        inicio_horizonte = _fecha_calendario(db, version, corte + 1)
        fin_horizonte = _fecha_calendario(db, version, corte + horizonte) or _fecha(
            evaluacion.get("fin_fecha")
        )

        entrenado = bool(origenes)
        # Clasificación con la regla aislada (probada en tests/test_auditoria_temporal.py).
        resultado, detalle, ultima_etiqueta = clasificar(
            variante=variante,
            etapa=evaluacion.get("etapa") or "",
            corte=corte,
            horizonte=horizonte,
            origenes=origenes,
            entrenado=bool(origenes),
        )
        if ultima_etiqueta is None and resultado == "separacion_verificada" and not origenes:
            fecha_ultima_etiqueta = None
        else:
            fecha_ultima_etiqueta = (
                _fecha_calendario(db, version, ultima_etiqueta)
                if ultima_etiqueta is not None
                else None
            )

        uso = (
            "Comparación de variantes y selección por menor WAPE de desarrollo (uso legítimo de "
            "desarrollo)."
            if evaluacion.get("etapa") != "final"
            else "Confirmación de la publicación: los resultados se consultaron antes de publicar, "
            "así que no constituyen un holdout intacto."
        )

        filas.append(
            {
                "version": version,
                "run_id": run_id,
                "variante": variante,
                "etapa": evaluacion.get("etapa"),
                "artefacto": artefacto or None,
                "origenes_entrenamiento": origenes,
                "horizonte": horizonte,
                "corte": corte,
                "fecha_corte": _fecha(evaluacion.get("origen_fecha")),
                "inicio_horizonte": inicio_horizonte,
                "fin_horizonte": fin_horizonte,
                "ultima_etiqueta_entrenamiento": ultima_etiqueta,
                "fecha_ultima_etiqueta": fecha_ultima_etiqueta,
                "dias_entre_etiqueta_y_corte": (
                    None if ultima_etiqueta is None else corte - ultima_etiqueta
                ),
                "entrenado": entrenado,
                "resultado": resultado,
                "detalle": detalle,
                "uso_del_corte": uso,
                "early_stopping": "no (num_boost_round fijo, sin callbacks ni valid_sets)",
                "seleccion_de_hiperparametros": "no (parámetros fijos en modelo.PARAMETROS)",
                "transformaciones": (
                    "ventanas causales t ≤ origen; precio del horizonte supuesto con el último "
                    "conocido (solo variante con precios)"
                    if variante == "precios"
                    else "ventanas causales t ≤ origen; sin variables de precio"
                ),
                "fuentes": FUENTES,
                "series_evaluadas": int(evaluacion.get("series") or 0),
            }
        )

    # Procedencia de las métricas de desarrollo del manifiesto publicado.
    publicada = next((documento for documento in runs.values() if documento.get("recomendado")), None)
    procedencia = None
    if publicada is not None:
        desarrollo = (publicada.get("metricas_resumen") or {}).get("desarrollo") or {}
        candidatas = [
            documento
            for documento in runs.values()
            if documento.get("estado") == "desarrollo"
            and documento.get("variante") == publicada.get("variante")
            and ((documento.get("metricas_resumen") or {}).get("desarrollo") or {}).get("wape")
            == desarrollo.get("wape")
        ]
        if candidatas:
            procedencia = {
                "manifiesto_publicado": publicada["run_id"],
                "wape_desarrollo": desarrollo.get("wape"),
                "ejecucion_de_desarrollo": candidatas[0]["run_id"],
                "nota": (
                    "Las métricas de desarrollo del manifiesto publicado pertenecen a la ejecución "
                    "de desarrollo de la misma variante (otro artefacto y otros orígenes), no al "
                    "artefacto publicado. Los cortes finales del artefacto publicado se evalúan "
                    "aparte."
                ),
            }

    resumen = {
        "filas": len(filas),
        "verificadas": sum(1 for fila in filas if fila["resultado"] == "separacion_verificada"),
        "contaminadas": sum(1 for fila in filas if fila["resultado"] == "contaminada"),
        "no_verificables": sum(1 for fila in filas if fila["resultado"] == "no_verificable"),
    }
    return {
        "esquema": ESQUEMA,
        "version": version,
        "generado_en": date.today().isoformat(),
        "resumen": resumen,
        "procedencia_metricas_desarrollo": procedencia,
        "filas": filas,
        "nota": (
            "La comprobación se hace sobre fechas de ETIQUETA (origen + horizonte), no solo sobre el "
            "origen de cada fila. La fecha física de ejecución del experimento no se usa como "
            "evidencia: lo relevante es qué información entró al modelo."
        ),
    }


def _fecha_calendario(db, version: str, dia: int) -> str | None:
    documento = db["m5_calendario"].find_one({"d": dia}, {"fecha": 1})
    if not documento:
        return None
    return _fecha(documento.get("fecha"))


def _tabla_markdown(informe: dict) -> str:
    lineas = [
        "| Variante · ejecución | Artefacto | Corte | Fechas del horizonte | Orígenes de entrenamiento | Última etiqueta | Uso del corte | Resultado |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for fila in informe["filas"]:
        artefacto = fila["artefacto"]
        nombre_artefacto = (
            Path(artefacto).name if artefacto else ("regla determinista (sin parámetros)" if not fila["entrenado"] else "sin artefacto")
        )
        origen_lista = (
            "d_" + ", d_".join(str(origen) for origen in fila["origenes_entrenamiento"])
            if fila["origenes_entrenamiento"]
            else "—"
        )
        etiqueta = (
            f"d_{fila['ultima_etiqueta_entrenamiento']} ({fila['fecha_ultima_etiqueta']})"
            if fila["ultima_etiqueta_entrenamiento"] is not None
            else "—"
        )
        resultado = {
            "separacion_verificada": "Separación verificada",
            "contaminada": "**Contaminada**",
            "no_verificable": "No verificable con la evidencia disponible",
        }[fila["resultado"]]
        lineas.append(
            "| {variante} · `{run}` | {artefacto} | d_{corte} ({fecha}) | {inicio} – {fin} | {origenes} | {etiqueta} | {uso} | {resultado} |".format(
                variante=fila["variante"],
                run=fila["run_id"],
                artefacto=nombre_artefacto,
                corte=fila["corte"],
                fecha=fila["fecha_corte"],
                inicio=fila["inicio_horizonte"],
                fin=fila["fin_horizonte"],
                origenes=origen_lista,
                etiqueta=etiqueta,
                uso="desarrollo (selección)" if fila["etapa"] != "final" else "final (confirmación)",
                resultado=resultado,
            )
        )
    return "\n".join(lineas)


def _resumen_consola(informe: dict) -> str:
    resumen = informe["resumen"]
    lineas = [
        f"Auditoría temporal · versión {informe['version']} · {informe['generado_en']}",
        f"Filas: {resumen['filas']} · verificadas {resumen['verificadas']} · "
        f"contaminadas {resumen['contaminadas']} · no verificables {resumen['no_verificables']}",
    ]
    for fila in informe["filas"]:
        etiqueta = fila["ultima_etiqueta_entrenamiento"]
        lineas.append(
            "  {variante:<10} {run:<26} {etapa:<11} corte d_{corte:<5} etiquetas ≤ d_{etiqueta:<5} → {resultado}".format(
                variante=fila["variante"],
                run=fila["run_id"],
                etapa=fila["etapa"],
                corte=fila["corte"],
                etiqueta=etiqueta if etiqueta is not None else "—",
                resultado=fila["resultado"],
            )
        )
    procedencia = informe.get("procedencia_metricas_desarrollo")
    if procedencia:
        lineas.append(
            "\nMétricas de desarrollo del manifiesto publicado: proceden de {run} (WAPE {wape}).".format(
                run=procedencia["ejecucion_de_desarrollo"], wape=procedencia["wape_desarrollo"]
            )
        )
    return "\n".join(lineas)


def main() -> int:
    parser = argparse.ArgumentParser(description="Auditoría de separación temporal")
    parser.add_argument("--json", default=None)
    parser.add_argument("--markdown", default=None)
    parser.add_argument("--solo-informe", action="store_true", help="no guarda en MongoDB")
    args = parser.parse_args()

    db = init_database(get_settings()).m5_db
    informe = auditar(db)
    print(_resumen_consola(informe))

    if not args.solo_informe:
        db[M5_AUDITORIA_TEMPORAL].replace_one(
            {"_id": f"{informe['version']}:{informe['esquema']}"},
            {
                "_id": f"{informe['version']}:{informe['esquema']}",
                **{clave: valor for clave, valor in informe.items()},
            },
            upsert=True,
        )
        print(f"\nGuardado en {M5_AUDITORIA_TEMPORAL} ({len(informe['filas'])} filas)")

    if args.json:
        Path(args.json).write_text(json.dumps(informe, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON: {args.json}")
    if args.markdown:
        Path(args.markdown).write_text(_tabla_markdown(informe) + "\n", encoding="utf-8")
        print(f"Markdown: {args.markdown}")
    return 1 if informe["resumen"]["contaminadas"] else 0


if __name__ == "__main__":
    sys.exit(main())
