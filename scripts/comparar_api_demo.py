"""Paridad funcional y latencia: API local (base completa) frente a API del paquete demo.

Levanta dos instancias del mismo código (``uvicorn app.main:app``): una apuntando a la base
local ``atlas_comercial_local`` y otra a la base compacta ``atlas_comercial_demo``. Recorre
las mismas rutas con los mismos filtros, compara las respuestas normalizadas y mide latencias
representativas. No modifica ninguna base ni escribe en ellas.

Uso::

    python -m scripts.comparar_api_demo \
        --origen http://127.0.0.1:8010 --destino http://127.0.0.1:8011 \
        --json ..\\evidencias\\comercial\\paridad-api-demo.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PREFIJO = "/api/v1/comercial/m5"

# (nombre, ruta, espera_json)
# Cubre las seis pantallas de la demo: panorama/pulso, exploración (comparación,
# estacionalidad, matriz, artículo, precios), futuro (pronóstico, evaluación, modelo),
# plan (lista, detalle, exportación) y auditoría.
CASOS: list[tuple[str, str]] = [
    # ·· estado y catálogo ······································································
    ("estado", f"{PREFIJO}/estado"),
    ("catalogo-base", f"{PREFIJO}/catalogo"),
    ("catalogo-busqueda", f"{PREFIJO}/catalogo?texto=FOODS_1&limite=5"),
    ("catalogo-desplazamiento", f"{PREFIJO}/catalogo?texto=FOODS&limite=3&desplazamiento=9"),
    ("modelos", f"{PREFIJO}/modelos"),
    ("inventario", f"{PREFIJO}/inventario"),
    # ·· pulso / panorama ······································································
    ("pulso-base", f"{PREFIJO}/pulso"),
    ("pulso-semana", f"{PREFIJO}/pulso?agregacion=semana"),
    ("pulso-nivel-departamento", f"{PREFIJO}/pulso?nivel=departamento&tienda=CA_1"),
    (
        "pulso-nivel-producto",
        f"{PREFIJO}/pulso?nivel=producto&tienda=CA_1"
        "&productos=FOODS_1_001&productos=FOODS_1_002&productos=FOODS_3_090",
    ),
    ("pulso-periodo", f"{PREFIJO}/pulso?desde=2015-01-01&hasta=2015-12-31"),
    (
        "pulso-ab",
        f"{PREFIJO}/pulso?a_desde=2014-05-23&a_hasta=2014-06-19"
        "&b_desde=2015-05-23&b_hasta=2015-06-19",
    ),
    ("pulso-filtros", f"{PREFIJO}/pulso?categorias=FOODS&departamentos=FOODS_3&tiendas=CA_1"),
    # ·· exploración: productos y artículo ···················································
    ("productos-base", f"{PREFIJO}/productos"),
    ("productos-tienda", f"{PREFIJO}/productos?tienda=CA_1&limite=10"),
    ("productos-texto", f"{PREFIJO}/productos?texto=FOODS_3_09&limite=5"),
    ("productos-orden", f"{PREFIJO}/productos?orden=item_id&limite=5&desplazamiento=2"),
    ("productos-filtros", f"{PREFIJO}/productos?departamento=FOODS_3&desde=2015-01-01&hasta=2015-06-30"),
    ("historico-semana", f"{PREFIJO}/historico?anio=2015&modo=semana"),
    ("historico-acumulado", f"{PREFIJO}/historico?anio=2015&modo=acumulado"),
    ("historico-filtros", f"{PREFIJO}/historico?anio=2014&tiendas=CA_1&categorias=HOBBIES"),
    ("articulo", f"{PREFIJO}/articulo?item=FOODS_1_001&tienda=CA_1"),
    (
        "articulo-periodo",
        f"{PREFIJO}/articulo?item=FOODS_3_090&tienda=TX_2&desde=2012-01-01&hasta=2012-12-31",
    ),
    ("articulo-inexistente", f"{PREFIJO}/articulo?item=NO_EXISTE_9_999&tienda=CA_1"),
    # ·· exploración: comparación, estacionalidad, matriz, intermitencia ···················
    (
        "comparacion",
        f"{PREFIJO}/comparacion?a_desde=2015-01-01&a_hasta=2015-06-30"
        "&b_desde=2016-01-01&b_hasta=2016-06-30",
    ),
    ("comparacion-filtros", f"{PREFIJO}/comparacion?tiendas=CA_1&estado=CA"),
    ("estacionalidad-estado", f"{PREFIJO}/estacionalidad"),
    ("estacionalidad-tienda", f"{PREFIJO}/estacionalidad?dimension=tienda&tiendas=CA_1,CA_2"),
    ("matriz-2015", f"{PREFIJO}/matriz-temporal?anio=2015"),
    ("matriz-2016", f"{PREFIJO}/matriz-temporal?anio=2016&categorias=FOODS"),
    ("intermitencia", f"{PREFIJO}/intermitencia?tienda=CA_1"),
    (
        "intermitencia-orden",
        f"{PREFIJO}/intermitencia?tienda=WI_1&orden=unidades&pagina=2&tamano=15",
    ),
    # ·· precios ··············································································
    ("precios-distribucion", f"{PREFIJO}/precios/distribucion?tienda=CA_1"),
    (
        "precios-distribucion-periodo",
        f"{PREFIJO}/precios/distribucion?tienda=TX_1&desde=2013-01-01&hasta=2014-12-31",
    ),
    ("precios-cambios", f"{PREFIJO}/precios/cambios?tienda=CA_1"),
    ("precios-cambios-filtros", f"{PREFIJO}/precios/cambios?tienda=CA_1&departamentos=FOODS_1"),
    ("precios-evolucion", f"{PREFIJO}/precios/evolucion?tienda=CA_1&item=FOODS_1_001"),
    (
        "precios-evolucion-periodo",
        f"{PREFIJO}/precios/evolucion?tienda=CA_1&item=FOODS_3_090&desde=2015-01-01&hasta=2015-12-31",
    ),
    # ·· futuro: pronóstico, evaluación, horizontes ·······································
    ("pronosticos-tienda", f"{PREFIJO}/pronosticos"),
    ("pronosticos-categoria", f"{PREFIJO}/pronosticos?nivel=categoria"),
    ("pronosticos-departamento", f"{PREFIJO}/pronosticos?nivel=departamento"),
    ("pronosticos-producto", f"{PREFIJO}/pronosticos?nivel=producto&items=FOODS_1_001"),
    ("pronosticos-categoria-tienda", f"{PREFIJO}/pronosticos?nivel=categoria_tienda&dias_historico=180"),
    ("evaluacion-total", f"{PREFIJO}/evaluacion?nivel=total"),
    ("evaluacion-tienda", f"{PREFIJO}/evaluacion?nivel=tienda"),
    ("evaluacion-departamento", f"{PREFIJO}/evaluacion?nivel=departamento"),
    ("evaluacion-desarrollo", f"{PREFIJO}/evaluacion?nivel=total&etapa=desarrollo"),
    ("evaluacion-ventanas-final", f"{PREFIJO}/evaluacion-ventanas"),
    ("evaluacion-ventanas-desarrollo", f"{PREFIJO}/evaluacion-ventanas?etapa=desarrollo"),
    ("matriz-horizontes", f"{PREFIJO}/matriz-horizontes"),
    ("matriz-horizontes-tienda", f"{PREFIJO}/matriz-horizontes?nivel=tienda"),
    ("evaluacion-horizonte", f"{PREFIJO}/evaluacion-horizonte"),
    ("evaluacion-horizonte-tienda", f"{PREFIJO}/evaluacion-horizonte?tienda=CA_1"),
    # ·· plan ·················································································
    ("plan-lista", f"{PREFIJO}/planificacion"),
    ("plan-lista-filtros", f"{PREFIJO}/planificacion?texto=FOODS_1_0&categorias=FOODS&tamano=10"),
    ("plan-lista-orden", f"{PREFIJO}/planificacion?orden=volumen_observado&minimo_observado=5&pagina=2"),
    ("plan-serie", f"{PREFIJO}/planificacion/serie?tienda=CA_1&item=FOODS_1_001"),
    ("plan-serie-tx", f"{PREFIJO}/planificacion/serie?tienda=TX_3&item=HOBBIES_1_120"),
    # ·· auditoría ············································································
    ("auditoria-temporal", f"{PREFIJO}/auditoria-temporal"),
]

# Exportación completa del plan: se compara por hash porque es CSV de 30 490 filas.
CASO_CSV = ("plan-exportar-csv", f"{PREFIJO}/planificacion/exportar")

# Consultas representativas para medir latencia (las más costosas de cada pantalla).
LATENCIA: list[tuple[str, str]] = [
    ("pulso", f"{PREFIJO}/pulso"),
    ("pulso-semana", f"{PREFIJO}/pulso?agregacion=semana"),
    ("productos", f"{PREFIJO}/productos"),
    ("comparacion", f"{PREFIJO}/comparacion"),
    ("estacionalidad", f"{PREFIJO}/estacionalidad"),
    ("matriz-temporal", f"{PREFIJO}/matriz-temporal?anio=2015"),
    ("articulo", f"{PREFIJO}/articulo?item=FOODS_1_001&tienda=CA_1"),
    ("intermitencia", f"{PREFIJO}/intermitencia?tienda=CA_1"),
    ("precios-distribucion", f"{PREFIJO}/precios/distribucion?tienda=CA_1"),
    ("precios-cambios", f"{PREFIJO}/precios/cambios?tienda=CA_1"),
    ("precios-evolucion", f"{PREFIJO}/precios/evolucion?tienda=CA_1&item=FOODS_1_001"),
    ("pronosticos", f"{PREFIJO}/pronosticos"),
    ("evaluacion", f"{PREFIJO}/evaluacion?nivel=total"),
    ("matriz-horizontes", f"{PREFIJO}/matriz-horizontes"),
    ("evaluacion-horizonte", f"{PREFIJO}/evaluacion-horizonte"),
    ("plan-lista", f"{PREFIJO}/planificacion"),
    ("plan-serie", f"{PREFIJO}/planificacion/serie?tienda=CA_1&item=FOODS_1_001"),
    ("auditoria-temporal", f"{PREFIJO}/auditoria-temporal"),
]

TOL_ABS = 1e-6
TOL_REL = 1e-9
MAX_DIFERENCIAS = 12

#: Campos que cambian en cada respuesta y no describen datos: se retiran antes de comparar.
VOLATILES = frozenset(
    {
        "generado_en",
        "generado",
        "actualizado_en",
        "construido_en",
        "medido_en",
        "verificado_en",
        "calculo_ms",
        "duracion_ms",
        "ms",
        "duracion_s",
    }
)

#: Diferencia esperada y deseada: la base de la demo se llama distinto a la de trabajo.
CAMPOS_IDENTIDAD = {
    "estado": frozenset({"base", "destino"}),
}

#: La ruta del artefacto del modelo se sustituye por su nombre de archivo en el paquete.
CAMPOS_ARTEFACTO = frozenset({"artefacto"})


def _sin_volatiles(valor: Any) -> Any:
    """Copia de la respuesta sin marcas de tiempo ni duraciones de cálculo."""
    if isinstance(valor, dict):
        return {clave: _sin_volatiles(dato) for clave, dato in valor.items() if clave not in VOLATILES}
    if isinstance(valor, list):
        return [_sin_volatiles(dato) for dato in valor]
    return valor


def _normalizar(caso: str, datos: Any) -> Any:
    """Normaliza una respuesta: quita lo volátil y resuelve las diferencias previstas."""
    limpio = _sin_volatiles(datos)
    if caso in CAMPOS_IDENTIDAD and isinstance(limpio, dict):
        for campo in CAMPOS_IDENTIDAD[caso]:
            limpio.pop(campo, None)
    if caso == "modelos" and isinstance(limpio, dict):
        # El paquete guarda `artefacto_nombre` (sin la ruta del equipo); se comparan nombres.
        for ejecucion in limpio.get("ejecuciones") or []:
            if not isinstance(ejecucion, dict):
                continue
            nombre = ejecucion.pop("artefacto_nombre", None) or ejecucion.get("artefacto")
            if nombre:
                ejecucion["artefacto"] = str(nombre).replace("\\", "/").split("/")[-1]
            else:
                ejecucion.pop("artefacto", None)
    if caso == "inventario" and isinstance(limpio, dict):
        # El inventario describe el almacenamiento físico: se compara el contenido, no los bytes.
        limpio = {
            "colecciones": [
                {"coleccion": item.get("coleccion"), "documentos": item.get("documentos")}
                for item in limpio.get("colecciones") or []
            ]
        }
    return limpio


def _traer(base: str, ruta: str, tiempo_limite: float = 300.0) -> tuple[int, bytes, float]:
    """GET simple; devuelve (estado, cuerpo, segundos)."""
    url = base.rstrip("/") + ruta
    inicio = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=tiempo_limite) as respuesta:  # noqa: S310 - local
            cuerpo = respuesta.read()
            estado = respuesta.status
    except urllib.error.HTTPError as exc:  # 4xx/5xx esperados: se comparan también
        cuerpo = exc.read()
        estado = exc.code
    except Exception as exc:  # noqa: BLE001 - se reporta como diferencia de transporte
        raise RuntimeError(f"fallo al pedir {url}: {exc}") from exc
    return estado, cuerpo, time.perf_counter() - inicio


def _comparar(a: Any, b: Any, ruta: str = "", salida: list[str] | None = None) -> list[str]:
    """Diferencias recursivas entre dos estructuras JSON, tolerando ruido de coma flotante."""
    salida = [] if salida is None else salida
    if len(salida) >= MAX_DIFERENCIAS:
        return salida
    if type(a) is not type(b) and not (
        isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool)
    ):
        salida.append(f"{ruta or '$'}: tipo {type(a).__name__} != {type(b).__name__}")
        return salida
    if isinstance(a, dict):
        faltan = sorted(set(a) - set(b))
        sobran = sorted(set(b) - set(a))
        if faltan:
            salida.append(f"{ruta or '$'}: faltan claves en destino {faltan[:6]}")
        if sobran:
            salida.append(f"{ruta or '$'}: claves sobrantes en destino {sobran[:6]}")
        for clave in sorted(set(a) & set(b)):
            _comparar(a[clave], b[clave], f"{ruta}.{clave}", salida)
            if len(salida) >= MAX_DIFERENCIAS:
                break
        return salida
    if isinstance(a, list):
        if len(a) != len(b):
            salida.append(f"{ruta}: longitud {len(a)} != {len(b)}")
            if not a or not b:
                return salida
        for indice, (x, y) in enumerate(zip(a, b, strict=False)):
            _comparar(x, y, f"{ruta}[{indice}]", salida)
            if len(salida) >= MAX_DIFERENCIAS:
                break
        return salida
    if isinstance(a, float) or isinstance(b, float):
        if math.isnan(float(a)) and math.isnan(float(b)):
            return salida
        if not math.isclose(float(a), float(b), rel_tol=TOL_REL, abs_tol=TOL_ABS):
            salida.append(f"{ruta}: {a!r} != {b!r}")
        return salida
    if a != b:
        salida.append(f"{ruta}: {a!r} != {b!r}")
    return salida


def _medir(base: str, ruta: str, repeticiones: int) -> dict[str, float]:
    tiempos: list[float] = []
    for _ in range(repeticiones):
        _, _, segundos = _traer(base, ruta)
        tiempos.append(segundos)
    return {
        "p50_ms": round(statistics.median(tiempos) * 1000, 1),
        "min_ms": round(min(tiempos) * 1000, 1),
        "max_ms": round(max(tiempos) * 1000, 1),
        "n": len(tiempos),
    }


def comparar(origen: str, destino: str, repeticiones: int = 7) -> dict[str, Any]:
    filas: list[dict[str, Any]] = []
    for nombre, ruta in CASOS:
        estado_o, cuerpo_o, t_o = _traer(origen, ruta)
        estado_d, cuerpo_d, t_d = _traer(destino, ruta)
        fila: dict[str, Any] = {
            "caso": nombre,
            "ruta": ruta,
            "estado_origen": estado_o,
            "estado_destino": estado_d,
            "bytes_origen": len(cuerpo_o),
            "bytes_destino": len(cuerpo_d),
            "sha_origen": hashlib.sha256(cuerpo_o).hexdigest()[:16],
            "sha_destino": hashlib.sha256(cuerpo_d).hexdigest()[:16],
        }
        if estado_o != estado_d:
            fila["diferencias"] = [f"estado HTTP {estado_o} != {estado_d}"]
        elif cuerpo_o == cuerpo_d:
            fila["diferencias"] = []
            fila["identico_byte"] = True
        else:
            try:
                json_o = _normalizar(nombre, json.loads(cuerpo_o))
                json_d = _normalizar(nombre, json.loads(cuerpo_d))
            except json.JSONDecodeError:
                fila["diferencias"] = ["cuerpo no JSON y bytes distintos"]
            else:
                fila["diferencias"] = _comparar(json_o, json_d)
                # Bytes distintos por marcas de tiempo o por la identidad de la base, pero
                # mismos datos: se declara explícitamente para no confundirlo con una regresión.
                fila["identico_normalizado"] = not fila["diferencias"]
        fila["ok"] = not fila.get("diferencias")
        filas.append(fila)
        marca = "OK  " if fila["ok"] else "DIF "
        print(f"{marca} {nombre:<32} {len(cuerpo_o):>9,} B / {len(cuerpo_d):>9,} B")

    # ·· exportación CSV completa ·····························································
    nombre_csv, ruta_csv = CASO_CSV
    estado_o, cuerpo_o, _ = _traer(origen, ruta_csv)
    estado_d, cuerpo_d, _ = _traer(destino, ruta_csv)
    fila_csv = {
        "caso": nombre_csv,
        "ruta": ruta_csv,
        "estado_origen": estado_o,
        "estado_destino": estado_d,
        "bytes_origen": len(cuerpo_o),
        "bytes_destino": len(cuerpo_d),
        "sha_origen": hashlib.sha256(cuerpo_o).hexdigest(),
        "sha_destino": hashlib.sha256(cuerpo_d).hexdigest(),
        "lineas_origen": cuerpo_o.count(b"\n"),
        "lineas_destino": cuerpo_d.count(b"\n"),
        "diferencias": [],
    }
    if estado_o != estado_d or cuerpo_o != cuerpo_d:
        fila_csv["diferencias"] = ["el CSV completo no coincide byte a byte"]
    fila_csv["ok"] = not fila_csv["diferencias"]
    filas.append(fila_csv)
    print(
        f"{'OK  ' if fila_csv['ok'] else 'DIF '} {nombre_csv:<32} "
        f"{len(cuerpo_o):>9,} B / {len(cuerpo_d):>9,} B"
    )

    # ·· latencias ···········································································
    latencias = []
    for nombre, ruta in LATENCIA:
        med_o = _medir(origen, ruta, repeticiones)
        med_d = _medir(destino, ruta, repeticiones)
        mejora = 100.0 * (1 - med_d["p50_ms"] / med_o["p50_ms"]) if med_o["p50_ms"] else 0.0
        latencias.append(
            {
                "caso": nombre,
                "ruta": ruta,
                "origen": med_o,
                "destino": med_d,
                "mejora_pct": round(mejora, 1),
            }
        )
        print(
            f"LAT {nombre:<32} origen {med_o['p50_ms']:>8.1f} ms · "
            f"demo {med_d['p50_ms']:>8.1f} ms · {mejora:+.1f} %"
        )

    con_diferencias = [f for f in filas if not f["ok"]]
    byte_a_byte = [f for f in filas if f.get("identico_byte")]
    resumen = {
        "casos": len(filas),
        "casos_ok": len(filas) - len(con_diferencias),
        "casos_con_diferencias": len(con_diferencias),
        "casos_detalle": [f["caso"] for f in con_diferencias],
        "casos_byte_a_byte": len(byte_a_byte),
        "casos_iguales_normalizados": sum(1 for f in filas if f.get("identico_normalizado")),
        "latencia_mediana_origen_ms": round(
            statistics.median([l["origen"]["p50_ms"] for l in latencias]), 1
        ),
        "latencia_mediana_destino_ms": round(
            statistics.median([l["destino"]["p50_ms"] for l in latencias]), 1
        ),
        "latencia_media_origen_ms": round(
            statistics.mean([l["origen"]["p50_ms"] for l in latencias]), 1
        ),
        "latencia_media_destino_ms": round(
            statistics.mean([l["destino"]["p50_ms"] for l in latencias]), 1
        ),
        "latencia_mejora_media_pct": round(
            statistics.mean([l["mejora_pct"] for l in latencias]), 1
        ),
    }
    return {
        "origen": origen,
        "destino": destino,
        "tolerancia": {"abs": TOL_ABS, "rel": TOL_REL},
        "resumen": resumen,
        "casos": filas,
        "latencias": latencias,
    }


def main(argv: list[str] | None = None) -> int:
    analizador = argparse.ArgumentParser(description=__doc__)
    analizador.add_argument("--origen", default="http://127.0.0.1:8010")
    analizador.add_argument("--destino", default="http://127.0.0.1:8011")
    analizador.add_argument("--repeticiones", type=int, default=7)
    analizador.add_argument("--json", type=Path, default=None)
    args = analizador.parse_args(argv)

    informe = comparar(args.origen, args.destino, args.repeticiones)
    resumen = informe["resumen"]
    print()
    print(
        f"Paridad: {resumen['casos_ok']}/{resumen['casos']} casos equivalentes "
        f"({resumen['casos_byte_a_byte']} byte a byte · "
        f"{resumen['casos_iguales_normalizados']} iguales tras normalizar) · "
        f"latencia mediana {resumen['latencia_mediana_origen_ms']} ms → "
        f"{resumen['latencia_mediana_destino_ms']} ms "
        f"({resumen['latencia_mejora_media_pct']:+.1f} % de media)"
    )
    if resumen["casos_con_diferencias"]:
        print("Casos con diferencias: " + ", ".join(resumen["casos_detalle"]))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(informe, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"Informe escrito en {args.json}")
    return 0 if resumen["casos_con_diferencias"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
