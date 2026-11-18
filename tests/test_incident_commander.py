"""Pruebas del Data Incident Commander sin API, LLM, Atlas ni escrituras."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings
from app.incident_commander.service import (
    IncidentCommander,
    PlanBlocked,
    RunConflict,
    RunNotFound,
    ScenarioError,
)

SNAPSHOT = PROJECT_ROOT / "dashboards" / "data-incident-commander" / "runtime" / "data.json"


def servicio() -> IncidentCommander:
    return IncidentCommander(
        Settings(
            _env_file=None,
            incident_data_path=str(SNAPSHOT),
        )
    )


def test_snapshot_real_de_21_observaciones() -> None:
    app = servicio()
    salud = app.salud()
    assert salud["observations"] == 21
    assert salud["llm"] is False
    assert salud["atlas"] is False
    assert salud["external_writes"] == 0
    assert salud["source_digest"] == app.source_digest
    assert len({(r["year"], r["indicator_code"]) for r in app.clean}) == 21


def test_tres_escenarios_inyectan_falla_solo_en_copia() -> None:
    app = servicio()
    escenarios = {
        "missing": (20, "period_coverage"),
        "duplicate": (24, "key_uniqueness"),
        "schema": (21, "catalog_codes"),
    }
    for scenario, (count, failed_check) in escenarios.items():
        resultado = app.crear_run(scenario)
        assert resultado["observations_in_copy"] == count
        assert resultado["observations_in_source"] == 21
        assert resultado["status"] == "awaiting_human"
        assert resultado["external_writes"] == 0
        baseline = {check["id"]: check["passed"] for check in resultado["baseline_checks"]}
        assert baseline[failed_check] is False
        assert all(
            check["passed"]
            for check in resultado["candidate_checks"]
            if check["id"] != "human_approval_recorded"
        )
        assert resultado["recommended_plan"] == "quarantine"
        assert app.source_digest == resultado["source_digest"]


def test_planes_permitidos_y_aprobacion_local() -> None:
    app = servicio()
    for plan in ("quarantine", "backfill", "fullreload"):
        run = app.crear_run("missing")
        respuesta = app.aprobar(run["run_id"], approved=True, plan_id=plan)
        assert respuesta["status"] == "sandbox_simulation_completed"
        assert respuesta["receipt"].startswith("RCPT-")
        assert respuesta["external_writes"] == 0
        assert respuesta["writes"] == []
        assert respuesta["source_unchanged"] is True
        assert all(check["passed"] for check in respuesta["tests_after"])


def test_imputacion_bloqueada_y_rechazo_sin_efecto() -> None:
    app = servicio()
    run = app.crear_run("missing")
    with pytest.raises(PlanBlocked, match="imputación"):
        app.aprobar(run["run_id"], approved=True, plan_id="forward")
    assert app.runs[run["run_id"]]["status"] == "awaiting_human"

    rechazo = app.aprobar(run["run_id"], approved=False, plan_id="forward")
    assert rechazo["status"] == "rejected_no_effect"
    assert rechazo["receipt"] is None
    assert rechazo["external_writes"] == 0
    assert rechazo["source_unchanged"] is True


def test_rechazo_normal_y_doble_aprobacion() -> None:
    app = servicio()
    run = app.crear_run("duplicate")
    rechazo = app.aprobar(run["run_id"], approved=False, plan_id="quarantine")
    assert rechazo["status"] == "rejected_no_effect"
    with pytest.raises(RunConflict):
        app.aprobar(run["run_id"], approved=True, plan_id="quarantine")

    run2 = app.crear_run("schema")
    app.aprobar(run2["run_id"], approved=True, plan_id="quarantine")
    with pytest.raises(RunConflict):
        app.aprobar(run2["run_id"], approved=True, plan_id="quarantine")


def test_run_inexistente_y_escenario_invalido() -> None:
    app = servicio()
    with pytest.raises(RunNotFound):
        app.aprobar("DI-NO-EXISTE", approved=False, plan_id="quarantine")
    with pytest.raises(ScenarioError):
        app.crear_run("otro")


def test_estado_es_efimero_por_proceso() -> None:
    primero = servicio()
    run = primero.crear_run("missing")
    assert run["run_id"] in primero.runs
    segundo = servicio()
    with pytest.raises(RunNotFound):
        segundo.aprobar(run["run_id"], approved=False, plan_id="quarantine")


def test_fuente_sigue_intacta_y_no_hay_escrituras(tmp_path) -> None:
    copia = tmp_path / "data.json"
    copia.write_bytes(SNAPSHOT.read_bytes())
    app = IncidentCommander(
        Settings(_env_file=None, incident_data_path=str(copia))
    )
    digest_antes = hashlib.sha256(copia.read_bytes()).hexdigest()
    run = app.crear_run("schema")
    app.aprobar(run["run_id"], approved=True, plan_id="quarantine")
    digest_despues = hashlib.sha256(copia.read_bytes()).hexdigest()
    assert digest_antes == digest_despues
    assert app.salud()["external_writes"] == 0
    assert list(Path(tmp_path).glob("*")) == [copia]
