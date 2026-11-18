"""Malla métrica estable para la concentración territorial del Atlas vial.

La app no usa teselas ni proyecciones en el navegador: MongoDB guarda `lat`/`lon`
en WGS84 y el mapa dibuja en lon/lat. Para medir concentración hace falta una
retícula de **celdas cuadradas de igual superficie**, con origen fijo e
identificadores que no dependan del encuadre ni del zoom.

Elección: **EPSG:32614 (WGS 84 / UTM zona 14N)**, la zona UTM que cubre Ciudad de
México (longitud −99°). Es una proyección conforme y, a esta escala (≈1 500 km²
alrededor del meridiano central −99°), la distorsión de superficie dentro del
ámbito es despreciable frente al tamaño de celda; las celdas son cuadradas en
metros y su superficie es 0,25 km² para 500 m.

Transformación: series clásicas USGS (Snyder) de Transversa de Mercator, en Python
puro, porque el entorno del backend no incluye `pyproj` ni `numpy` y no se añaden
dependencias por esta entrega. Precisión esperada: sub-métrica dentro de la zona,
muy por debajo de la resolución de 500 m.

Reglas fijas (documentadas en la respuesta del endpoint):

- `RESOLUCION_M = 500` y una sola resolución en esta entrega.
- Origen: esquina suroeste del marco de CDMX, redondeada hacia abajo al múltiplo
  de 500 m en UTM. Se calcula con constantes del propio tablero (marco
  `lat 19.02918–19.61182`, `lon −99.43712 – −98.86810`), no con los datos: así no
  cambia al filtrar.
- Pertenencia: intervalos semiabiertos `[W, W + 500)` en ambos ejes. Un punto
  exactamente sobre un borde pertenece a la celda que empieza en ese borde.
- Identificador reproducible: `C500-<ix>-<iy>` con `ix`, `iy` relativos al origen.
"""

from __future__ import annotations

import math
from typing import Iterator

RESOLUCION_M = 500
ZONA_UTM = "EPSG:32614"

# Marco del tablero (el mismo que publica `/geometria` como extensión).
MARCO_LAT_MIN = 19.02918
MARCO_LON_MIN = -99.43712
MARCO_LAT_MAX = 19.61182
MARCO_LON_MAX = -98.86810

# Parámetros WGS84 / UTM.
_A = 6378137.0
_F = 1 / 298.257223563
_K0 = 0.9996
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_FALSO_ESTE = 500000.0
_MERIDIANO_CENTRAL = -99.0  # zona 14N


def _meridiano_arco(lat_rad: float) -> float:
    """Longitud de arco del meridiano (Snyder 3-21)."""
    e4 = _E2 * _E2
    e6 = e4 * _E2
    return _A * (
        (1 - _E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * lat_rad
        - (3 * _E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * lat_rad)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * lat_rad)
        - (35 * e6 / 3072) * math.sin(6 * lat_rad)
    )


def a_utm(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 (grados) → UTM 14N (metros)."""
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    lon0 = math.radians(_MERIDIANO_CENTRAL)

    seno = math.sin(lat_rad)
    coseno = math.cos(lat_rad)
    tangente = math.tan(lat_rad)
    n = _A / math.sqrt(1 - _E2 * seno * seno)
    t = tangente * tangente
    c = _EP2 * coseno * coseno
    a = coseno * (lon_rad - lon0)

    m = _meridiano_arco(lat_rad)
    este = _FALSO_ESTE + _K0 * n * (
        a
        + (1 - t + c) * a**3 / 6
        + (5 - 18 * t + t * t + 72 * c - 58 * _EP2) * a**5 / 120
    )
    norte = _K0 * (
        m
        + n
        * tangente
        * (
            a * a / 2
            + (5 - t + 9 * c + 4 * c * c) * a**4 / 24
            + (61 - 58 * t + t * t + 600 * c - 330 * _EP2) * a**6 / 720
        )
    )
    return este, norte


def a_wgs84(este: float, norte: float) -> tuple[float, float]:
    """UTM 14N (metros) → WGS84 (grados). Necesaria para dibujar la celda."""
    x = este - _FALSO_ESTE
    y = norte
    m = y / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))

    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )

    seno = math.sin(phi1)
    coseno = math.cos(phi1)
    tangente = math.tan(phi1)
    c1 = _EP2 * coseno * coseno
    t1 = tangente * tangente
    n1 = _A / math.sqrt(1 - _E2 * seno * seno)
    r1 = _A * (1 - _E2) / (1 - _E2 * seno * seno) ** 1.5
    d = x / (n1 * _K0)

    lat = phi1 - (n1 * tangente / r1) * (
        d * d / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * _EP2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * _EP2 - 3 * c1 * c1) * d**6 / 720
    )
    lon = (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * _EP2 + 24 * t1 * t1) * d**5 / 120
    ) / coseno
    return math.degrees(lat), math.degrees(lon) + _MERIDIANO_CENTRAL


# Origen fijo: esquina suroeste del marco, alineada a la rejilla de 500 m.
_origen_este, _origen_norte = a_utm(MARCO_LAT_MIN, MARCO_LON_MIN)
ORIGEN_X = math.floor(_origen_este / RESOLUCION_M) * RESOLUCION_M
ORIGEN_Y = math.floor(_origen_norte / RESOLUCION_M) * RESOLUCION_M


def celda_de(lat: float, lon: float) -> tuple[int, int]:
    """Índices (ix, iy) de la celda que contiene el punto (intervalos semiabiertos)."""
    este, norte = a_utm(lat, lon)
    ix = math.floor((este - ORIGEN_X) / RESOLUCION_M)
    iy = math.floor((norte - ORIGEN_Y) / RESOLUCION_M)
    return int(ix), int(iy)


def identificador(ix: int, iy: int) -> str:
    return f"C{RESOLUCION_M}-{ix}-{iy}"


def geometria_celda(ix: int, iy: int) -> list[list[float]]:
    """Anillo [lon, lat] del cuadrado de la celda, para dibujarla en el mapa."""
    x0 = ORIGEN_X + ix * RESOLUCION_M
    y0 = ORIGEN_Y + iy * RESOLUCION_M
    x1 = x0 + RESOLUCION_M
    y1 = y0 + RESOLUCION_M
    esquinas = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    anillo: list[list[float]] = []
    for este, norte in esquinas:
        lat, lon = a_wgs84(este, norte)
        anillo.append([round(lon, 6), round(lat, 6)])
    return anillo


def marco_celda(ix: int, iy: int) -> tuple[float, float, float, float]:
    """Extensión [lonMin, latMin, lonMax, latMax] de la celda."""
    anillo = geometria_celda(ix, iy)
    lons = [punto[0] for punto in anillo]
    lats = [punto[1] for punto in anillo]
    return min(lons), min(lats), max(lons), max(lats)


def describir() -> dict:
    """Metadatos de la malla que viajan en la respuesta del endpoint."""
    return {
        "resolucion_m": RESOLUCION_M,
        "superficie_km2": round((RESOLUCION_M / 1000) ** 2, 4),
        "proyeccion": ZONA_UTM,
        "zona_utm": "14N",
        "origen": {"x": ORIGEN_X, "y": ORIGEN_Y, "unidad": "m"},
        "pertenencia": "intervalos semiabiertos [o, o+resolucion): el borde pertenece a la celda que empieza en él",
    }
