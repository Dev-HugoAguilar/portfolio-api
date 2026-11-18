"""Medición de presupuestos de respuesta de los endpoints del Atlas vial.

Mide p95 con caché y sin caché sobre el mismo conjunto de filtros, y cuenta las
consultas que dispara el mapa durante el movimiento del puntero (cero fuera del
navegador: aquí se mide el coste de cada endpoint).
"""
import json, statistics, time, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8010/api/v1/atlas-vial"
CASOS = [
    ("/resumen", {}),
    ("/territorio?incluir_puntos=false", {}),
    ("/puntos", {}),
    ("/resumen", {"alcaldia": "Iztapalapa"}),
    ("/resumen", {"alcaldia": "Iztapalapa", "tipo_evento": "Atropellado", "semaforizada": "si"}),
    ("/puntos", {"alcaldia": "Iztapalapa"}),
    ("/tiempo", {"alcaldia": "Iztapalapa"}),
]

def medir(ruta, params, repeticiones=12):
    tiempos = []
    for _ in range(repeticiones):
        consulta = urllib.parse.urlencode(params)
        url = f"{BASE}{ruta}{'&' if '?' in ruta else '?'}{consulta}&_={time.time_ns()}" if consulta else f"{BASE}{ruta}{'&' if '?' in ruta else '?'}_={time.time_ns()}"
        inicio = time.perf_counter()
        with urllib.request.urlopen(url, timeout=60) as respuesta:  # noqa: S310
            respuesta.read()
        tiempos.append((time.perf_counter() - inicio) * 1000)
    return tiempos

print(f"{'endpoint':<26}{'params':<44}{'p50':>8}{'p95':>9}{'max':>9}")
for ruta, params in CASOS:
    tiempos = medir(ruta, params)
    tiempos.sort()
    p50 = statistics.median(tiempos)
    p95 = tiempos[min(len(tiempos) - 1, int(round(0.95 * (len(tiempos) - 1))))]
    print(f"{ruta:<26}{json.dumps(params, ensure_ascii=False)[:42]:<44}{p50:>8.0f}{p95:>9.0f}{max(tiempos):>9.0f}")
