"""Baseline de pronóstico y evaluación fuera de muestra (Walmart M5).

Calcula, sobre la versión publicada en MongoDB local:

- Backtesting con ventanas separadas un horizonte de 28 días.
- Pronóstico desde el último día observado (sin futuro conocido).
- Métricas MAE, sesgo y WAPE por ventana y por agregado.

Uso:
    python -m scripts.pronosticar_m5                 # calcula y guarda
    python -m scripts.pronosticar_m5 --estado        # muestra lo ya guardado
    python -m scripts.pronosticar_m5 --ventanas 8 --horizonte 28

Códigos de salida: 0 éxito · 3 sin datos publicados.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.comercial import forecast
from app.comercial import ingesta
from app.config import get_settings
from app.db.mongo import M5_EVALUACION, M5_PRONOSTICOS, init_database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OK = 0
SIN_DATOS = 3


def _json(datos) -> None:
    print(json.dumps(datos, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Baseline estacional semanal de M5.")
    parser.add_argument("--ventanas", type=int, default=None, help="Número de ventanas de backtesting.")
    parser.add_argument("--horizonte", type=int, default=28, help="Días de horizonte (por omisión 28).")
    parser.add_argument("--estado", action="store_true", help="Muestra la evaluación ya guardada.")
    args = parser.parse_args(argv)

    settings = get_settings()
    base = init_database(settings)
    version = ingesta.version_actual(base.m5_db)

    if not version:
        _json(
            {
                "estado": "pendiente",
                "mensaje": "Datos pendientes de importar",
                "accion": "python -m scripts.importar_m5 (requiere los CSV de M5 en backend/data/m5)",
            }
        )
        return SIN_DATOS

    if args.estado:
        evaluaciones = list(base.m5_db[M5_EVALUACION].find({"version": version}, {"_id": 0}))
        pronosticos = base.m5_db[M5_PRONOSTICOS].count_documents({"version": version})
        _json({"version": version, "pronosticos": pronosticos, "evaluaciones": evaluaciones})
        return OK

    resultado = forecast.calcular(
        base.m5_db,
        version,
        horizonte=args.horizonte,
        ventanas=args.ventanas or settings.comercial_ventanas,
    )
    _json(resultado)
    return OK


if __name__ == "__main__":
    sys.exit(main())
