"""Pruebas del servicio de «Briefing con evidencia» sin depender de Atlas."""

from __future__ import annotations

import json

from app.config import BACKEND_DIR, Settings
from app.rag_banxico import service as rag_service
from app.rag_banxico.service import BriefingBanxico, puntuar


class FakeDatabase:
    """Base mínima: la demo debe funcionar aunque Atlas no exista."""

    ping = False
    client = None


def servicio(**overrides) -> BriefingBanxico:
    valores = {
        "rag_banxico_corpus_path": str(
            BACKEND_DIR / "data" / "snapshots" / "banxico" / "corpus.json"
        ),
        "llm_base_url": None,
        "llm_api_key": None,
        "llm_model": None,
    }
    valores.update(overrides)
    return BriefingBanxico(FakeDatabase(), Settings(_env_file=None, **valores))


def test_recupera_tasa_y_cita_fuente() -> None:
    resultado = servicio().consultar("¿Cuál fue la decisión sobre la tasa y desde cuándo aplica?")
    assert resultado["retrieval_mode"] == "corpus-local"
    assert resultado["mode"] == "extractiva"
    assert resultado["vector_search"] is False
    assert resultado["evidence"]
    assert any(item["chunk_id"] == "BM-2026-11-05-TASA" for item in resultado["evidence"])
    assert "BM-2026-11-05" in resultado["answer"] or resultado["evidence"][0]["source_id"] == "BM-2026-11-05"


def test_no_inventa_evidencia() -> None:
    resultado = servicio().consultar("¿Cuál es la capital de Francia?")
    assert resultado["mode"] == "sin-coincidencias"
    assert resultado["evidence"] == []
    assert "No encontré evidencia suficiente" in resultado["answer"]


def test_salud_no_expone_uri_ni_claves() -> None:
    salud = servicio().salud()
    serializado = json.dumps(salud, ensure_ascii=False)
    assert salud["corpus_chunks"] == 7
    assert salud["vector_search"] is False
    assert salud["mongo"]["target"] in {"local", "atlas"}
    assert "mongodb://" not in serializado
    assert "mongodb+srv://" not in serializado
    assert "api_key" not in serializado.lower()


def test_puntuacion_solo_devuelve_coincidencias() -> None:
    servicio_local = servicio()
    assert puntuar("riesgos al alza", servicio_local.corpus["chunks"])[0]["chunk_id"] == "BM-2026-11-05-RIESGOS"
    assert puntuar("capital de Francia", servicio_local.corpus["chunks"]) == []


def test_modelo_configurado_responde_solo_con_evidencia(monkeypatch) -> None:
    """El camino LLM exige los identificadores de las fuentes recuperadas."""

    class RespuestaFalsa:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "La tasa quedó en 6.25%."}}]}
            ).encode("utf-8")

    monkeypatch.setattr(rag_service, "urlopen", lambda request, timeout: RespuestaFalsa())
    servicio_llm = servicio(
        llm_base_url="http://proveedor.invalid/v1",
        llm_api_key="clave-de-prueba",
        llm_model="modelo-de-prueba",
    )
    resultado = servicio_llm.consultar("¿Cuál fue la decisión sobre la tasa?")
    assert resultado["mode"] == "llm-grounded"
    assert "La tasa quedó en 6.25%." in resultado["answer"]
    assert "[BM-2026-11-05]" in resultado["answer"]
    assert resultado["vector_search"] is False


def test_modelo_que_falla_conserva_respuesta_extractiva(monkeypatch) -> None:
    """Un error del proveedor no deja la consulta sin respuesta ni la inventa."""

    def fallar(request, timeout):
        raise TimeoutError("proveedor no disponible")

    monkeypatch.setattr(rag_service, "urlopen", fallar)
    servicio_llm = servicio(
        llm_base_url="http://proveedor.invalid/v1",
        llm_api_key="clave-de-prueba",
        llm_model="modelo-de-prueba",
    )
    resultado = servicio_llm.consultar("¿Cuál fue la decisión sobre la tasa?")
    assert resultado["mode"] == "extractiva-respaldo"
    assert resultado["evidence"]
    assert "6.25%" in resultado["answer"]
