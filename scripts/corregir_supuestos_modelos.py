"""Alinea los supuestos guardados en el manifiesto con las variables reales de cada ejecución.

Motivo: los manifiestos anteriores declaraban «el precio de los 28 días pronosticados es el
último precio conocido» en la variante de calendario, que no tiene ninguna variable de precio, y
afirmaban que «el vector publicado es entero», cosa que dejó de ser cierta con la política
decimal-v1. La descripción del modelo no puede contradecir a la ejecución que describe.

El script no reentrena, no reimporta y no recalcula predicciones: solo reescribe el campo
``supuestos`` de los documentos de ``m5_modelos`` con el texto que corresponde a sus variables.
Por omisión informa; con ``--aplicar`` escribe.
"""

from __future__ import annotations

import json
import sys

from app.config import get_settings
from app.db.mongo import M5_MODELOS, init_database
from app.services.comercial_service import supuestos_modelo


def main(aplicar: bool) -> int:
    db = init_database(get_settings()).m5_db
    cambios = 0
    for documento in db[M5_MODELOS].find({}, {"variables": 1, "supuestos": 1, "run_id": 1}):
        correctos = supuestos_modelo(documento)
        if correctos == (documento.get("supuestos") or {}):
            print(f"= {documento['_id']}: sin cambios")
            continue
        cambios += 1
        print(f"~ {documento['_id']} ({len(documento.get('variables') or [])} variables)")
        print("  antes:   " + json.dumps(documento.get("supuestos"), ensure_ascii=False))
        print("  después: " + json.dumps(correctos, ensure_ascii=False))
        if aplicar:
            db[M5_MODELOS].update_one({"_id": documento["_id"]}, {"$set": {"supuestos": correctos}})
    print(f"\n{cambios} manifiesto(s) con supuestos desalineados.")
    if cambios and not aplicar:
        print("Ejecuta con --aplicar para escribir la corrección.")
    return 0


if __name__ == "__main__":
    sys.exit(main("--aplicar" in sys.argv[1:]))
