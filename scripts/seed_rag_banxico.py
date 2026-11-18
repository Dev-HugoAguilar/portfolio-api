"""Carga idempotente del corpus de «Briefing con evidencia» en Atlas.

Uso, desde ``backend/``::

    python -m scripts.seed_rag_banxico
    python -m scripts.seed_rag_banxico --dry-run

Escribe **solo** en la colección dedicada
``portfolio_demos.rag_banxico_chunks`` (o la configurada con
``RAG_BANXICO_MONGODB_DB`` / ``RAG_BANXICO_COLLECTION``). No borra documentos de
otros proyectos ni cambia la configuración del clúster. Nunca imprime la URI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pymongo import MongoClient, UpdateOne
from pymongo.errors import PyMongoError

from app.config import get_settings


def _objetivo(uri: str) -> str:
    texto = (uri or "").lower()
    if "mongodb+srv://" in texto or "mongodb.net" in texto:
        return "atlas"
    return "local"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Valida sin escribir.")
    args = parser.parse_args()

    settings = get_settings()
    ruta = Path(settings.rag_banxico_corpus_path)
    if not ruta.exists():
        raise SystemExit(f"No existe el corpus: {ruta}")
    corpus = json.loads(ruta.read_text(encoding="utf-8"))
    fuentes = {fuente["source_id"]: fuente for fuente in corpus["sources"]}
    documentos = [
        {**fragmento, "source": fuentes[fragmento["source_id"]], "snapshot": corpus["snapshot"]}
        for fragmento in corpus["chunks"]
    ]

    cliente = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5_000)
    try:
        cliente.admin.command("ping")
        base = cliente[settings.rag_banxico_mongodb_db]
        coleccion = base[settings.rag_banxico_collection]
        if args.dry_run:
            print(
                f"dry-run: {len(documentos)} fragmentos listos para "
                f"{settings.rag_banxico_mongodb_db}.{settings.rag_banxico_collection} "
                f"(destino: {_objetivo(settings.mongodb_uri)})."
            )
            return 0
        operaciones = [
            UpdateOne({"chunk_id": documento["chunk_id"]}, {"$set": documento}, upsert=True)
            for documento in documentos
        ]
        resultado = coleccion.bulk_write(operaciones, ordered=False)
        coleccion.create_index("chunk_id", unique=True)
        coleccion.create_index("source_id")
        print(
            f"Cargados {len(documentos)} fragmentos en "
            f"{settings.rag_banxico_mongodb_db}.{settings.rag_banxico_collection} "
            f"(destino: {_objetivo(settings.mongodb_uri)}; "
            f"upserts={resultado.upserted_count}; actualizados={resultado.modified_count})."
        )
        return 0
    except PyMongoError as exc:
        print(
            f"No se pudo cargar el corpus: {type(exc).__name__}. "
            "La aplicación seguirá funcionando con corpus.json.",
            file=sys.stderr,
        )
        return 1
    finally:
        cliente.close()


if __name__ == "__main__":
    raise SystemExit(main())
