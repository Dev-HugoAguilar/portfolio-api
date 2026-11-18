"""Mide el tiempo de respuesta de las vistas principales, en frío y con caché.

Dos medidas por vista, siempre sobre la misma versión publicada:

* **sin caché** (``X-Marketing-Cache: miss``): se pide una combinación de filtros
  que todavía no está en la caché de 300 s de la API, así que el cálculo se hace
  entero en ese momento.
* **con caché** (``X-Marketing-Cache: hit``): se repite la misma petición, que ya
  sale de la caché del proceso.

El Atlas vial y el comparativo bancario no tienen caché de aplicación: se piden
dos veces seguidas y se informa de las dos, sin llamarlas «con caché».

Uso::

    python -m scripts.medir_respuesta                  # todas las vistas
    python -m scripts.medir_respuesta --repeticiones 3 # mediana de 3 medidas
    python -m scripts.medir_respuesta --json medidas.json

Nunca imprime credenciales: solo habla por HTTP con la API local.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

API = "http://127.0.0.1:8010/api/v1"

#: Vistas que se miden: nombre, ruta y si la API la cachea.
VISTAS = [
    ("Marketing · resumen (tarjetas, serie, Sankey, lollipop)", "/marketing/resumen", True),
    ("Marketing · adquisición (ranking y productos)", "/marketing/adquisicion", True),
    ("Marketing · conversión (embudo, tiempos, fricción)", "/marketing/conversion", True),
    ("Marketing · cohortes (matriz, curvas, puntos)", "/marketing/cohortes", True),
    ("Marketing · productos (categorías y afinidad)", "/marketing/productos", True),
    ("Marketing · exportación de agregados", "/marketing/exportar", False),
    ("Atlas vial · resumen", "/atlas-vial/resumen", False),
    ("Atlas vial · puntos del mapa", "/atlas-vial/puntos", False),
    ("Comparativo bancario · resumen", "/cnbv/banca-multiple/resumen", False),
    ("Comparativo bancario · detalle paginado", "/cnbv/banca-multiple/detalle?page_size=50", False),
]


def peticion(ruta: str) -> tuple[float, int, dict[str, str]]:
    """Devuelve (milisegundos, bytes, cabeceras) de una petición GET."""
    inicio = time.perf_counter()
    with urllib.request.urlopen(f"{API}{ruta}", timeout=300) as respuesta:
        cuerpo = respuesta.read()
        cabeceras = {clave.lower(): valor for clave, valor in respuesta.headers.items()}
    return (time.perf_counter() - inicio) * 1000, len(cuerpo), cabeceras


def ventana(indice: int, desplazamiento: int) -> str:
    """Combinación de fechas distinta en cada llamada, para forzar un fallo de caché.

    La API cachea por versión + ruta + filtros durante 300 s; con una ventana nueva
    el cálculo se hace de verdad en esa petición.
    """
    inicio = date(2020, 11, 1) + timedelta(days=desplazamiento)
    fin = inicio + timedelta(days=6)
    return f"desde={inicio.isoformat()}&hasta={fin.isoformat()}"


def medir(ruta: str, cacheada: bool, repeticiones: int, desplazamiento: int) -> dict:
    resultados: dict = {"ruta": ruta, "cacheada": cacheada, "frios": [], "calientes": []}

    for intento in range(repeticiones):
        # Cada repetición usa una ventana distinta: así siempre hay fallo de caché.
        consulta = ventana(intento, desplazamiento + intento * 7)
        separador = "&" if "?" in ruta else "?"
        ms, bytes_, cabeceras = peticion(f"{ruta}{separador}{consulta}")
        resultados["frios"].append(ms)
        resultados["estado_cache_frio"] = cabeceras.get("x-marketing-cache")
        resultados["ms_calculo"] = cabeceras.get("x-marketing-ms")
        resultados["bytes"] = bytes_

        ms, bytes_, cabeceras = peticion(f"{ruta}{separador}{consulta}")
        resultados["calientes"].append(ms)
        resultados["estado_cache_caliente"] = cabeceras.get("x-marketing-cache")

    resultados["frio_mediana_ms"] = round(statistics.median(resultados["frios"]), 1)
    resultados["caliente_mediana_ms"] = round(statistics.median(resultados["calientes"]), 1)
    return resultados


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Mide la respuesta de las vistas")
    argumentos.add_argument("--repeticiones", type=int, default=2)
    argumentos.add_argument(
        "--desplazamiento", type=int, default=0, help="Día de inicio de la primera ventana."
    )
    argumentos.add_argument("--json", default=None, help="Guarda las medidas en este archivo.")
    opciones = argumentos.parse_args()

    salida = []
    print(f"{'vista':58s} {'sin caché':>11s} {'con caché':>11s} {'KB':>6s}  caché")
    print("-" * 100)
    for indice, (nombre, ruta, cacheada) in enumerate(VISTAS):
        datos = medir(ruta, cacheada, opciones.repeticiones, opciones.desplazamiento + indice)
        datos["vista"] = nombre
        salida.append(datos)
        frio = f"{datos['frio_mediana_ms']:.0f} ms"
        caliente = (
            f"{datos['caliente_mediana_ms']:.0f} ms" if datos["estado_cache_caliente"] == "hit"
            else f"{datos['caliente_mediana_ms']:.0f} ms*"
        )
        estado = (
            f"{datos['estado_cache_frio']} → {datos['estado_cache_caliente']}"
            if cacheada
            else "sin caché de aplicación"
        )
        print(
            f"{nombre:58s} {frio:>11s} {caliente:>11s} {datos['bytes'] / 1024:6.1f}  {estado}"
        )

    print(
        "\n* Segunda llamada sin caché de aplicación: el cálculo se repite completo.\n"
        "  «sin caché» = primera petición de una combinación de filtros nueva (miss).\n"
        "  «con caché» = la misma petición repetida dentro de los 300 s (hit)."
    )
    if opciones.json:
        with open(opciones.json, "w", encoding="utf-8") as archivo:
            json.dump(salida, archivo, ensure_ascii=False, indent=1)
        print(f"\nmedidas guardadas en {opciones.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
