"""Construye y publica el derivado local ``departamento_tienda`` (M5).

M5 no publica la intersección **tienda × departamento** y no puede deducirse de
los marginales. Este proceso la calcula desde ``m5_bloques`` + catálogo, la
reconcilia contra los agregados ``total``, ``tienda`` y ``departamento`` y solo
entonces la publica en ``m5_agregados``.

- Idempotente: con el derivado ya publicado no recalcula nada (``--reconstruir``
  lo fuerza).
- No reimporta ventas, no toca los bloques, no cambia la versión activa.
- Mientras no esté publicado, la API calcula la serie al vuelo y no sirve nada a
  medias.

Uso:
    python -m scripts.agregar_m5                 # construye si falta y publica
    python -m scripts.agregar_m5 --reconstruir   # recalcula aunque exista
    python -m scripts.agregar_m5 --estado        # estado del derivado
    python -m scripts.agregar_m5 --verificar     # solo reconcilia (no escribe)
    python -m scripts.agregar_m5 --explicar      # plan de la consulta de lectura

Códigos de salida: 0 éxito · 1 fallo de reconciliación · 3 sin datos importados.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.comercial import derivados, ingesta
from app.config import get_settings
from app.db.mongo import M5_AGREGADOS, init_database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OK = 0
FALLO = 1
SIN_DATOS = 3


def _json(datos) -> None:
    print(json.dumps(datos, ensure_ascii=False, indent=2, default=str))


def _explicar(db, version: str) -> dict:
    """Comprueba que la lectura del derivado usa el índice compuesto."""
    clave = f"CA_1|FOODS_1"
    plan = db[M5_AGREGADOS].find(
        {"version": version, "dimension": derivados.NOMBRE_DERIVADO, "clave": clave},
        {"clave": 1, "d_inicio": 1, "unidades": 1},
    ).explain()
    texto = json.dumps(plan.get("queryPlanner", {}), default=str)
    return {
        "consulta": {"version": version, "dimension": derivados.NOMBRE_DERIVADO, "clave": clave},
        "usa_ixscan": "IXSCAN" in texto,
        "usa_collscan": "COLLSCAN" in texto,
        "indice": plan.get("queryPlanner", {}).get("winningPlan", {}).get("inputStage", {}).get("indexName"),
        "nota": (
            "El índice único (version, dimension, clave) ya existente cubre la lectura del "
            "derivado: no se añadió ningún índice nuevo."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Construye el derivado departamento_tienda de M5.")
    parser.add_argument("--reconstruir", action="store_true", help="Recalcula aunque ya esté publicado.")
    parser.add_argument("--estado", action="store_true", help="Muestra el estado del derivado.")
    parser.add_argument("--verificar", action="store_true", help="Solo reconcilia, sin escribir nada.")
    parser.add_argument("--explicar", action="store_true", help="Plan de la consulta de lectura.")
    args = parser.parse_args(argv)

    settings = get_settings()
    base = init_database(settings)
    db = base.m5_db
    version = ingesta.version_actual(db)

    if not version:
        _json(
            {
                "estado": "pendiente",
                "mensaje": "Datos pendientes de importar",
                "detalle": "Ejecuta python -m scripts.importar_m5 antes de derivar agregados.",
                "base": db.name,
            }
        )
        return SIN_DATOS

    if args.estado:
        _json(
            {
                "version": version,
                "base": db.name,
                "derivado": derivados.info(db, version),
                "documentos_derivado": db[M5_AGREGADOS].count_documents(
                    {"version": version, "dimension": derivados.NOMBRE_DERIVADO}
                ),
            }
        )
        return OK

    if args.verificar:
        calculo = derivados.calcular(db, version)
        reconciliacion = derivados.reconciliar(
            db, version, calculo["series"], d_inicio=calculo["d_inicio"]
        )
        _json({"version": version, "base": db.name, "reconciliacion": reconciliacion})
        return OK if reconciliacion["ok"] else FALLO

    if args.explicar:
        _json(_explicar(db, version))
        return OK

    try:
        resultado = derivados.construir(db, version, forzar=args.reconstruir)
    except derivados.ErrorDerivado as exc:
        _json({"estado": "fallo", "version": version, "detalle": str(exc)})
        return FALLO

    _json(
        {
            "version": version,
            "base": db.name,
            "destino_local": True,
            "derivado": resultado["nombre"],
            "estado": resultado["estado"],
            "reutilizado": resultado.get("reutilizado"),
            "documentos": resultado.get("documentos"),
            "dias": resultado.get("dias"),
            "duracion_s": resultado.get("duracion_s"),
            "reconciliacion": resultado.get("reconciliacion"),
            "documentos_derivado": db[M5_AGREGADOS].count_documents(
                {"version": version, "dimension": derivados.NOMBRE_DERIVADO}
            ),
        }
    )
    return OK


if __name__ == "__main__":
    sys.exit(main())
