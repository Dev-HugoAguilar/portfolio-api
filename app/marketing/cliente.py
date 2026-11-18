"""Acceso a BigQuery, con diagnóstico explícito cuando no está disponible.

El tablero **nunca** consulta BigQuery desde el navegador: la ingesta es la única
que habla con Google, y lo hace con credenciales del entorno (nunca incrustadas en
el código ni en el frontend).

Se intentan dos vías, en este orden:

1. la biblioteca oficial `google-cloud-bigquery` con las credenciales de
   aplicación por omisión (ADC);
2. la herramienta `bq` del SDK de Google Cloud, con la cuenta autenticada del
   equipo.

Si ninguna funciona, se lanza :class:`SinAccesoBigQuery` con el motivo real y los
comandos exactos que hacen falta. No se crean credenciales ni se simulan datos.
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol

from app.marketing import consultas, esquema

logger = logging.getLogger(__name__)

#: Comandos que resuelven el acceso cuando falta.
REMEDIO = [
    "gcloud auth login",
    "gcloud config set project TU_PROYECTO",
    "gcloud auth application-default login",
    r".venv\Scripts\python.exe -m pip install google-cloud-bigquery",
    r".venv\Scripts\python.exe -m scripts.importar_marketing",
]


class SinAccesoBigQuery(RuntimeError):
    """No hay forma de consultar el conjunto público en este entorno."""

    def __init__(self, motivo: str, detalle: dict[str, Any] | None = None) -> None:
        super().__init__(motivo)
        self.motivo = motivo
        self.detalle = detalle or {}

    def publico(self) -> dict[str, Any]:
        return {
            "disponible": False,
            "motivo": self.motivo,
            "remedio": REMEDIO,
            "detalle": self.detalle,
            "conjunto": esquema.TABLAS,
            "documentacion": esquema.DOCUMENTACION,
        }


@dataclass
class EstadoAcceso:
    """Resultado del diagnóstico de acceso (no lanza excepción)."""

    disponible: bool
    via: str | None = None
    motivo: str = ""
    detalle: dict[str, Any] = field(default_factory=dict)


class Fuente(Protocol):
    """Contrato mínimo de una fuente de consulta."""

    nombre: str

    def ejecutar(self, sql: str, max_filas: int = 200_000) -> list[dict[str, Any]]: ...

    def iterar(self, sql: str) -> Iterator[dict[str, Any]]:
        """Recorre el resultado fila a fila, sin cargarlo entero en memoria."""

    def ejecutar(self, sql: str, max_filas: int = 200_000) -> list[dict[str, Any]]: ...

    def iterar(self, sql: str) -> Iterator[dict[str, Any]]:
        """Recorre el resultado fila a fila, sin cargarlo entero en memoria."""


class _ViaCliente:
    """Biblioteca oficial `google-cloud-bigquery`."""

    nombre = "google-cloud-bigquery"

    def __init__(self, cliente: Any) -> None:
        self._cliente = cliente

    def ejecutar(self, sql: str, max_filas: int = 200_000) -> list[dict[str, Any]]:
        trabajo = self._cliente.query(sql)
        filas = trabajo.result(max_results=max_filas)
        return [dict(fila.items()) for fila in filas]

    def iterar(self, sql: str) -> Iterator[dict[str, Any]]:
        trabajo = self._cliente.query(sql)
        for fila in trabajo.result():
            yield dict(fila.items())


def proyecto_gcloud(ejecutable_gcloud: str | None = None) -> str | None:
    """Proyecto activo de `gcloud`, solo como respaldo del valor configurado.

    La herramienta `bq` lanzada desde un proceso no interactivo no siempre hereda
    el proyecto activo («Cannot start a job without a project id»), así que se
    resuelve aquí y se pasa explícito en cada consulta.
    """
    gcloud = ejecutable_gcloud or shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not gcloud:
        return None
    try:
        proceso = subprocess.run(  # noqa: S603 - ejecutable resuelto por ruta
            [gcloud, "config", "get-value", "project", "--quiet"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    valor = (proceso.stdout or "").strip()
    if proceso.returncode != 0 or not valor or valor.startswith("ERROR") or "(unset)" in valor:
        return None
    return valor


class _ViaCli:
    """Herramienta `bq` del SDK de Google Cloud."""

    nombre = "bq-cli"

    def __init__(self, ejecutable: str, proyecto: str | None) -> None:
        self._bq = ejecutable
        self._proyecto = proyecto
        self._ubicacion = esquema.REGION

    def esquema(self) -> dict[str, str]:
        """Columnas de la tabla de referencia, aplanadas con nombres con punto.

        Usa `bq show --schema`, que es una lectura de metadatos: no escanea datos ni
        consume la cuota de consulta.
        """
        argumentos = [self._bq, "show", "--schema", "--format=json", esquema.TABLA_REFERENCIA]
        if self._proyecto:
            argumentos.insert(1, f"--project_id={self._proyecto}")
        proceso = subprocess.run(  # noqa: S603 - ejecutable resuelto por ruta
            argumentos,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            shell=False,
        )
        if proceso.returncode != 0:
            detalle = (proceso.stderr or proceso.stdout or "").strip().splitlines()
            raise SinAccesoBigQuery(
                "No se pudo leer el esquema de la tabla de referencia.",
                {"salida": detalle[:6]},
            )
        campos = json.loads(proceso.stdout or "[]")
        columnas: dict[str, str] = {}

        def recorrer(lista: list[dict[str, Any]], prefijo: str) -> None:
            for campo in lista:
                nombre = str(campo.get("name") or "")
                if not nombre:
                    continue
                ruta = f"{prefijo}{nombre}"
                columnas[ruta] = str(campo.get("type") or "")
                hijos = campo.get("fields") or []
                if hijos:
                    recorrer(hijos, f"{ruta}.")

        recorrer(campos, "")
        return columnas

    def iterar(self, sql: str) -> Iterator[dict[str, Any]]:
        """Recorre el resultado en CSV, línea a línea.

        La salida se lee mientras llega, así que no hay que materializar millones
        de filas en memoria ni depender de `--max_rows` (que truncaba resultados).
        La salida de error se drena en un hilo aparte: si se llena su tubería,
        `bq` se queda esperando y la lectura termina en un fallo engañoso.
        """
        argumentos = [
            self._bq,
            "query",
            "--use_legacy_sql=false",
            "--format=csv",
            "--max_rows=20000000",
            f"--location={self._ubicacion}",
        ]
        if self._proyecto:
            argumentos.append(f"--project_id={self._proyecto}")

        proceso = subprocess.Popen(  # noqa: S603 - ejecutable resuelto por ruta
            argumentos,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1 << 16,
            shell=False,
        )
        assert proceso.stdin and proceso.stdout and proceso.stderr

        diagnostico: list[str] = []

        def drenar() -> None:
            for linea in proceso.stderr:
                texto = linea.rstrip()
                if texto:
                    diagnostico.append(texto)
                    if len(diagnostico) > 400:
                        del diagnostico[:200]

        hilo = threading.Thread(target=drenar, name="bq-stderr", daemon=True)
        hilo.start()

        try:
            proceso.stdin.write(sql)
            proceso.stdin.close()
        except (BrokenPipeError, OSError):  # pragma: no cover - la CLI cerró la entrada
            pass

        cabecera: list[str] | None = None
        error_stdout: list[str] = []
        try:
            for fila in csv.reader(proceso.stdout):
                if not fila:
                    continue
                if cabecera is None:
                    primera = fila[0].strip()
                    # `bq` escribe los errores de SQL en la salida estándar: no se
                    # confunden con una cabecera de datos.
                    if primera.lower().startswith("error"):
                        error_stdout.append(",".join(fila))
                        continue
                    if primera.lower() == "waiting on" or "status:" in primera:
                        continue
                    cabecera = [columna.strip() for columna in fila]
                    continue
                if error_stdout:
                    error_stdout.append(",".join(fila))
                    continue
                yield dict(zip(cabecera, fila))
        finally:
            try:
                proceso.stdout.close()
            except OSError:  # pragma: no cover - cierre defensivo
                pass
            try:
                proceso.wait(timeout=180)
            except subprocess.TimeoutExpired:  # pragma: no cover - proceso colgado
                proceso.kill()
                proceso.wait(timeout=60)
            hilo.join(timeout=10)

        if proceso.returncode not in (0, None) or error_stdout:
            utiles = [
                linea
                for linea in diagnostico
                if "Waiting on" not in linea and "Current status" not in linea
            ]
            detalle = error_stdout or utiles or diagnostico
            raise SinAccesoBigQuery(
                "La consulta a BigQuery falló durante la lectura del resultado.",
                {"salida": detalle[-6:]},
            )

    def ejecutar(self, sql: str, max_filas: int = 200_000) -> list[dict[str, Any]]:
        argumentos = [
            self._bq,
            "query",
            "--use_legacy_sql=false",
            "--format=json",
            f"--max_rows={max_filas}",
            f"--location={self._ubicacion}",
        ]
        if self._proyecto:
            argumentos.append(f"--project_id={self._proyecto}")

        # La consulta va por **stdin** y no como argumento: en Windows la CLI es un
        # archivo por lotes y los saltos de línea (y los «%» de PARSE_DATE) se
        # romperían al pasar por `cmd.exe`. Por entrada estándar llega intacta.
        proceso = subprocess.run(  # noqa: S603 - ejecutable resuelto por ruta
            argumentos,
            input=sql,
            capture_output=True,
            text=True,
            # UTF-8 explícito: con la codificación local de Windows (cp1252) los
            # acentos de la consulta llegan corruptos y la CLI de BigQuery falla.
            encoding="utf-8",
            errors="replace",
            timeout=900,
            shell=False,
        )
        if proceso.returncode != 0:
            crudo = f"{proceso.stderr or ''}\n{proceso.stdout or ''}"
            detalle = [linea for linea in crudo.splitlines() if linea.strip()]
            raise SinAccesoBigQuery(
                "La consulta a BigQuery falló con la herramienta `bq`.",
                {"salida": detalle[:6]},
            )
        salida = proceso.stdout.strip() or "[]"
        try:
            datos = json.loads(salida)
        except json.JSONDecodeError as error:  # pragma: no cover - salida anómala
            raise SinAccesoBigQuery(
                "La salida de `bq` no es JSON válido.",
                {"detalle": str(error)},
            ) from error
        return [dict(fila) for fila in datos]


def resolver(proyecto: str | None = None) -> Fuente:
    """Devuelve la primera vía de acceso que funcione o lanza la excepción."""
    problemas: list[str] = []

    try:  # 1) biblioteca oficial
        from google.cloud import bigquery  # type: ignore[import-not-found]

        cliente = bigquery.Client(project=proyecto) if proyecto else bigquery.Client()
        # Comprobación real: una consulta trivial cuesta 0 bytes.
        cliente.query("SELECT 1").result()
        logger.info("Acceso a BigQuery por la biblioteca oficial")
        return _ViaCliente(cliente)
    except Exception as error:  # noqa: BLE001 - cualquier fallo cae a la vía 2
        problemas.append(f"google-cloud-bigquery: {type(error).__name__}: {error}")

    ejecutable = shutil.which("bq") or shutil.which("bq.cmd")
    if ejecutable:
        try:  # 2) herramienta de línea de comandos
            objetivo = proyecto or proyecto_gcloud()
            if not objetivo:
                problemas.append(
                    "bq: no hay proyecto configurado. Define MARKETING_GCP_PROJECT o "
                    "ejecuta `gcloud config set project TU_PROYECTO`."
                )
                raise SinAccesoBigQuery(problemas[-1])
            fuente = _ViaCli(ejecutable, objetivo)
            fuente.ejecutar("SELECT 1 AS ok", max_filas=1)
            logger.info("Acceso a BigQuery por la herramienta `bq`")
            return fuente
        except Exception as error:  # noqa: BLE001
            problemas.append(f"bq: {type(error).__name__}: {error}")
    else:
        problemas.append("bq: no encontrado en PATH")

    raise SinAccesoBigQuery(
        "No hay acceso a BigQuery en este entorno: no se pudo autenticar ni "
        "consultar el conjunto público. Sin acceso, la ingesta no publica nada.",
        {"intentos": problemas},
    )


#: Caché del diagnóstico: comprobar el acceso implica intentar autenticar (y, con
#: la herramienta `bq`, lanzar un proceso). Se repite como mucho una vez cada
#: minuto para no castigar cada petición de la interfaz.
_DIAGNOSTICO: dict[str, Any] = {"estado": None, "momento": 0.0}
_DIAGNOSTICO_SEGUNDOS = 60.0


def diagnosticar(proyecto: str | None = None, *, refrescar: bool = False) -> EstadoAcceso:
    """Comprueba el acceso sin lanzar excepción (para la API y la CLI)."""
    from time import monotonic

    ahora = monotonic()
    cacheado = _DIAGNOSTICO.get("estado")
    if (
        not refrescar
        and isinstance(cacheado, EstadoAcceso)
        and ahora - float(_DIAGNOSTICO.get("momento") or 0) < _DIAGNOSTICO_SEGUNDOS
    ):
        return cacheado

    try:
        fuente = resolver(proyecto)
        estado = EstadoAcceso(disponible=True, via=fuente.nombre)
    except SinAccesoBigQuery as error:
        estado = EstadoAcceso(
            disponible=False,
            motivo=error.motivo,
            detalle=error.detalle,
        )

    _DIAGNOSTICO["estado"] = estado
    _DIAGNOSTICO["momento"] = ahora
    return estado


def leer_esquema(fuente: Fuente) -> esquema.Esquema:
    """Inspecciona las columnas reales de la tabla antes de calcular nada.

    Se prefiere la lectura de metadatos de la propia fuente (no escanea datos); si
    la fuente no la ofrece, se consulta la vista de región de INFORMATION_SCHEMA.
    """
    nativo = getattr(fuente, "esquema", None)
    if callable(nativo):
        columnas = nativo()
        if columnas:
            return esquema.Esquema(columnas=dict(columnas))
    filas = fuente.ejecutar(consultas.sql_esquema())
    columnas: dict[str, str] = {}
    for fila in filas:
        nombre = str(fila.get("column_name") or "").strip()
        tipo = str(fila.get("data_type") or "").strip()
        if nombre:
            columnas[nombre] = tipo
    if not columnas:
        raise SinAccesoBigQuery(
            "BigQuery respondió sin columnas para la tabla de muestra.",
            {"conjunto": esquema.TABLAS},
        )
    return esquema.Esquema(columnas=columnas)