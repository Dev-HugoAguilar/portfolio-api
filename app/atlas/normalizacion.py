"""Normalización de los valores de los CSV de la SSC.

Reglas **comprobadas** contra los cinco archivos (ver
`docs/atlas-vial/AUDITORIA-FUENTES.md`):

* Codificación: los archivos traen BOM UTF-8. Algunos campos vienen con *doble
  codificación* (UTF-8 guardado como si fuera latin-1): en el archivo de
  vehículos todos los valores de `no_vehiculo` («VehÃ­culo 1»), y en los hechos
  unas pocas filas de `origen` («BOTÃ“N DE AUXILIO»). Se repara con
  ``encode('latin-1').decode('utf-8')`` solo cuando la marca de mojibake está
  presente y la reparación no pierde caracteres.
* Encabezados duplicados: el recurso «por tipo de persona» repite
  ``condicion_persona``. La primera aparición es la **consecuencia** (lesionada /
  fallecida) y la segunda el **tipo de usuario de la vía** (PEATON, CONDUCTOR…).
  Ambas columnas se conservan y se mapean a nombres distintos; las claves
  originales quedan en ``_originales``.
* Mayúsculas, acentos y erratas: se agrupa con una forma canónica y se conserva
  el valor original para poder auditar. Ejemplos reales: ``VIA PRIMA`` (por
  VÍA PRIMARIA), ``VAC  ANULAR`` (doble espacio), ``BOTON DE AUXILIO``,
  ``DOMINGO``/``domingo``.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any

# --------------------------------------------------------------------- mojibake
#: Marca de doble codificación: «Ã»/«Â» seguido de otro carácter de la extensión
#: latin-1 (0x80–0xBF), que es lo que produce leer UTF-8 como latin-1.
_MOJIBAKE = re.compile(r"[\u00c2\u00c3][\u0080-\u00bf]")
#: Algunos archivos se re-guardaron en cp1252, así que el segundo carácter llega
#: como comilla tipográfica («BOTÃ\u201cN»). Este mapa devuelve su byte original.
_BYTES_CP1252 = {
    "\u20ac": 0x80, "\u201a": 0x82, "\u0192": 0x83, "\u201e": 0x84, "\u2026": 0x85,
    "\u2020": 0x86, "\u2021": 0x87, "\u02c6": 0x88, "\u2030": 0x89, "\u0160": 0x8A,
    "\u2039": 0x8B, "\u0152": 0x8C, "\u017d": 0x8E, "\u2018": 0x91, "\u2019": 0x92,
    "\u201c": 0x93, "\u201d": 0x94, "\u2022": 0x95, "\u2013": 0x96, "\u2014": 0x97,
    "\u02dc": 0x98, "\u2122": 0x99, "\u0161": 0x9A, "\u203a": 0x9B, "\u0153": 0x9C,
    "\u017e": 0x9E, "\u0178": 0x9F,
}


def _bytes_cp1252(texto: str) -> bytes | None:
    """Bytes equivalentes en cp1252, o ``None`` si hay un carácter fuera de rango."""
    salida = bytearray()
    for caracter in texto:
        byte = _BYTES_CP1252.get(caracter)
        if byte is not None:
            salida.append(byte)
        elif ord(caracter) < 256:
            salida.append(ord(caracter))
        else:
            return None
    return bytes(salida)


def reparar_mojibake(valor: str) -> str:
    """Revierte la doble codificación cuando existe y la reparación es segura.

    Dos casos reales, ambos verificados en los archivos de la SSC:

    * ``«VehÃ\\u00adculo 1»`` — el UTF-8 se leyó como latin-1: se repara con
      ``encode('latin-1').decode('utf-8')``;
    * ``«BOTÃ\\u201cN DE AUXILIO»`` — el mismo problema pero re-guardado en cp1252,
      donde el byte 0x93 llega como comilla tipográfica: se repara devolviendo
      cada carácter a su byte de cp1252 y decodificando en UTF-8.

    Si la reparación no produce texto válido se devuelve el valor original: es
    preferible mostrar un carácter raro que corromper un dato bueno.
    """
    if not valor:
        return valor
    if "Ã" not in valor and "Â" not in valor:
        return valor

    if _MOJIBAKE.search(valor):
        try:
            return valor.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    bytes_cp1252 = _bytes_cp1252(valor)
    if bytes_cp1252 is not None:
        try:
            return bytes_cp1252.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return valor


def clave(texto: str) -> str:
    """Forma canónica para agrupar: sin acentos, sin dobles espacios, mayúsculas."""
    limpio = reparar_mojibake(texto or "")
    limpio = unicodedata.normalize("NFKD", limpio)
    limpio = "".join(c for c in limpio if not unicodedata.combining(c))
    limpio = re.sub(r"\s+", " ", limpio).strip().upper()
    return limpio


# ------------------------------------------------------------------- alcaldías
#: Nombre oficial (como en la geometría del catálogo) y alias observados.
ALCALDIAS: dict[str, str] = {
    "AZCAPOTZALCO": "Azcapotzalco",
    "COYOACAN": "Coyoacán",
    "CUAJIMALPA DE MORELOS": "Cuajimalpa de Morelos",
    "CUAJIMALPA": "Cuajimalpa de Morelos",
    "GUSTAVO A MADERO": "Gustavo A. Madero",
    "GUSTAVO A. MADERO": "Gustavo A. Madero",
    "IZTACALCO": "Iztacalco",
    "IZTAPALAPA": "Iztapalapa",
    "LA MAGDALENA CONTRERAS": "La Magdalena Contreras",
    "MAGDALENA CONTRERAS": "La Magdalena Contreras",
    "MILPA ALTA": "Milpa Alta",
    "ALVARO OBREGON": "Álvaro Obregón",
    "TLAHUAC": "Tláhuac",
    "TLALPAN": "Tlalpan",
    "XOCHIMILCO": "Xochimilco",
    "BENITO JUAREZ": "Benito Juárez",
    "CUAUHTEMOC": "Cuauhtémoc",
    "MIGUEL HIDALGO": "Miguel Hidalgo",
    "VENUSTIANO CARRANZA": "Venustiano Carranza",
}


def alcaldia_canonica(texto: str) -> str | None:
    """Devuelve el nombre oficial de la alcaldía o ``None`` si no se reconoce."""
    if not texto:
        return None
    return ALCALDIAS.get(clave(texto))


# ----------------------------------------------------------------- fecha y hora
_FECHA = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_FECHA_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_HORA = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def leer_fecha(valor: str) -> date | None:
    """Interpreta ``dd/mm/aaaa`` (formato de los cinco archivos)."""
    if not valor:
        return None
    texto = valor.strip()
    coincidencia = _FECHA.match(texto)
    if coincidencia:
        dia, mes, anio = (int(parte) for parte in coincidencia.groups())
        try:
            return date(anio, mes, dia)
        except ValueError:
            return None
    coincidencia = _FECHA_ISO.match(texto)
    if coincidencia:
        try:
            return date(*(int(p) for p in coincidencia.groups()))
        except ValueError:
            return None
    return None


def leer_hora(valor: str) -> int | None:
    """Hora entera 0–23. ``None`` si el campo no es una hora válida."""
    if not valor:
        return None
    coincidencia = _HORA.match(valor.strip())
    if not coincidencia:
        return None
    hora = int(coincidencia.group(1))
    minuto = int(coincidencia.group(2))
    if not (0 <= hora <= 23 and 0 <= minuto <= 59):
        return None
    return hora


def dia_semana(fecha: date) -> str:
    return DIAS[fecha.weekday()]


# ------------------------------------------------------------------- numéricos
def leer_entero(valor: str) -> tuple[int | None, bool]:
    """Devuelve (número, es_ausente). Los vacíos y no numéricos son ausentes."""
    if valor is None:
        return None, True
    texto = str(valor).strip()
    if texto == "":
        return None, True
    try:
        return int(float(texto)), False
    except ValueError:
        return None, True


# ------------------------------------------------------------------ categóricos
CONSECUENCIAS = {"LESIONADA": "lesionada", "LESIONADO": "lesionada", "FALLECIDA": "fallecida", "FALLECIDO": "fallecida"}

TIPOS_USUARIO = {
    "PEATON": "Peatón",
    "CONDUCTOR": "Conductor",
    "PASAJERO": "Pasajero",
    "MOTOCICLISTA": "Motociclista",
    "CICLISTA": "Ciclista",
    "SD": "Sin dato",
    "NA": "Sin dato",
    "": "Sin dato",
}

SEXOS = {"HOMBRE": "Hombre", "MUJER": "Mujer", "SD": "Sin dato", "NA": "Sin dato", "": "Sin dato"}

VIALIDADES = {
    "VIA PRIMARIA": "Vía primaria",
    "VIA PRIMA": "Vía primaria",
    "VIA SECUNDARIA": "Vía secundaria",
    "EJE VIAL": "Eje vial",
    "VAC ANULAR": "Vía de acceso controlado (anular)",
    "VAC RADIAL": "Vía de acceso controlado (radial)",
    "VAC VIADUCTO": "Vía de acceso controlado (viaducto)",
    "ACCESO CARRETERO": "Acceso carretero",
    "SD": "Sin dato",
    "": "Sin dato",
}

INTERSECCIONES = {
    "CRUZ": "Cruce",
    "T": "Intersección en T",
    "RECTA": "Tramo recto",
    "Y": "Bifurcación en Y",
    "CURVA": "Curva",
    "RAMAS MULTIPLES": "Ramas múltiples",
    "GLORIETA": "Glorieta",
    "DESNIVEL": "Desnivel",
    "GAZA": "Gaza",
    "SD": "Sin dato",
    "": "Sin dato",
}

ORIGENES = {
    "LLAMADA DEL 911": "Llamada al 911",
    "RADIO": "Radio",
    "BOTON DE AUXILIO": "Botón de auxilio",
    "CAMARA": "Cámara",
    "OTROS": "Otros",
    "REDES SOCIALES": "Redes sociales",
    "MI CALLE": "Mi calle",
    "MI CALLE(BOTON)": "Mi calle (botón)",
    "MI POLICIA": "Mi policía",
    "SD": "Sin dato",
    "": "Sin dato",
}

TIPOS_EVENTO = {
    "CHOQUE": "Choque",
    "DERRAPADO": "Derrapado",
    "ATROPELLADO": "Atropellado",
    "CAIDA DE CICLISTA": "Caída de ciclista",
    "CAIDA DE PASAJERO": "Caída de pasajero",
    "VOLCADURA": "Volcadura",
    "SD": "Sin dato",
    "": "Sin dato",
}

TIPOS_VEHICULO = {
    "AUTOMOVIL": "Automóvil",
    "MOTOCICLETA": "Motocicleta",
    "BICICLETA": "Bicicleta",
    "CAMIONETA": "Camioneta",
    "TAXI": "Taxi",
    "AUTOBUS DE PASAJEROS": "Autobús de pasajeros",
    "CAMION DE CARGA": "Camión de carga",
    "METROBUS": "Metrobús",
    "MICROBUS": "Microbús",
    "MONOPATIN": "Monopatín",
    "TROLEBUS": "Trolebús",
    "MOTO": "Motocicleta",
    "SD": "Sin dato",
    "": "Sin dato",
}


def _canonica(valor: str, tabla: dict[str, str], omision: str | None = None) -> str | None:
    forma = clave(valor)
    if forma in tabla:
        return tabla[forma]
    return omision


def consecuencia(valor: str) -> str | None:
    return _canonica(valor, CONSECUENCIAS)


def tipo_usuario(valor: str) -> str | None:
    return _canonica(valor, TIPOS_USUARIO)


def sexo(valor: str) -> str | None:
    return _canonica(valor, SEXOS)


def vialidad(valor: str) -> str | None:
    return _canonica(valor, VIALIDADES)


def interseccion(valor: str) -> str | None:
    return _canonica(valor, INTERSECCIONES)


def origen(valor: str) -> str | None:
    return _canonica(valor, ORIGENES)


def tipo_evento(valor: str) -> str | None:
    return _canonica(valor, TIPOS_EVENTO)


def tipo_vehiculo(valor: str) -> str | None:
    return _canonica(valor, TIPOS_VEHICULO)


def si_no(valor: str) -> bool | None:
    """``SI``/``NO``. Cualquier otro valor (``NI``, vacío) es desconocido."""
    forma = clave(valor)
    if forma in {"SI", "SÍ"}:
        return True
    if forma == "NO":
        return False
    return None


# ------------------------------------------------------------------ geografía
LAT_MIN, LAT_MAX = 19.0, 19.62
LON_MIN, LON_MAX = -99.40, -98.92


def leer_coordenadas(latitud: str, longitud: str) -> tuple[float | None, float | None, str]:
    """Valida el par de coordenadas.

    Devuelve ``(lat, lon, estado)`` con estado en
    ``ok | ausente | no_numerica | fuera_de_rango | fuera_de_cdmx``.
    """
    lat_texto = (latitud or "").strip()
    lon_texto = (longitud or "").strip()
    if not lat_texto or not lon_texto:
        return None, None, "ausente"
    try:
        lat = float(lat_texto)
        lon = float(lon_texto)
    except ValueError:
        return None, None, "no_numerica"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None, "fuera_de_rango"
    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        return lat, lon, "fuera_de_cdmx"
    return lat, lon, "ok"


def edad_valor(valor: str) -> tuple[int | None, str]:
    """Edad de un registro.

    Los archivos traen ``SD`` (sin dato) y ``NA``, además de erratas como
    ``2O`` (letra O) o ``9M`` (meses). Devuelve ``(edad, texto_original)``; la
    edad es ``None`` cuando el campo no es un número de años utilizable.
    """
    texto = (valor or "").strip()
    if not texto:
        return None, texto
    limpio = texto.upper().replace("O", "0").replace(" ", "")
    if limpio.endswith("M"):  # 9M, 5M: meses de vida, no años
        return 0, texto
    if not limpio.isdigit():
        return None, texto
    edad = int(limpio)
    if edad > 120:
        return None, texto
    return edad, texto
