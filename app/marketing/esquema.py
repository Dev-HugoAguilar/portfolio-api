"""Esquema del conjunto GA4 de BigQuery y reglas de validación.

Todo lo que se calcula en este módulo depende de columnas concretas del esquema
oficial de BigQuery Export de Google Analytics 4
(https://support.google.com/analytics/answer/7029846). La ingesta **verifica el
esquema real antes de calcular** y se detiene si falta algo imprescindible: es
preferible no publicar que publicar cifras construidas sobre supuestos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Proyecto, conjunto y familia de tablas diarias del conjunto público.
PROYECTO = "bigquery-public-data"
CONJUNTO = "ga4_obfuscated_sample_ecommerce"
TABLAS = f"{PROYECTO}.{CONJUNTO}.events_*"

#: Región del conjunto (donde vive y donde se factura la consulta).
REGION = "US"

#: Tabla de referencia para inspeccionar el esquema (metadata, sin costo).
#: Se escribe con dos puntos porque la CLI `bq` interpreta el formato con
#: puntos como perteneciente al proyecto activo.
TABLA_REFERENCIA = f"{PROYECTO}:{CONJUNTO}.events_20201101"

#: Periodo que la documentación oficial atribuye al conjunto.
PERIODO_DOCUMENTADO = {"desde": "2020-11-01", "hasta": "2021-01-31"}

#: Fecha de consulta del catálogo/documentación que se usó al implementar.
DOCUMENTACION = "https://developers.google.com/analytics/bigquery/web-ecommerce-demo-dataset"
ESQUEMA_OFICIAL = "https://support.google.com/analytics/answer/7029846"

#: Licencia y condición de los datos, tal como los describe la fuente.
LICENCIA = "BigQuery Public Datasets (datos ofuscados de demostración)"
CONDICION = (
    "Muestra histórica y ofuscada de eventos de comercio electrónico de Google "
    "Merchandise Store. Puede contener valores de marcador y su consistencia "
    "interna es limitada; no representa resultados comerciales actuales ni "
    "información íntegra de Google."
)

#: Columnas imprescindibles por bloque de métricas. Clave: nombre lógico.
REQUERIDAS_BASE = {
    "event_date": "STRING",
    "event_timestamp": "INTEGER",
    "event_name": "STRING",
    "user_pseudo_id": "STRING",
    "event_params": "RECORD",
    "traffic_source.source": "STRING",
    "traffic_source.medium": "STRING",
    "device.category": "STRING",
    "geo.country": "STRING",
    "geo.region": "STRING",
}

REQUERIDAS_COMPRA = {
    "ecommerce.transaction_id": "STRING",
    "ecommerce.purchase_revenue_in_usd": "FLOAT",
    "items.item_name": "STRING",
}

#: Columna que se comprueba pero cuya ausencia solo desactiva una vista.
OPCIONALES = {
    "items.item_category": "STRING",
    "ecommerce.total_item_quantity": "INTEGER",
    "geo.city": "STRING",
    "device.operating_system": "STRING",
}

#: Valores de marcador de la fuente. Se conservan como «Sin información».
MARCADORES = {
    "",
    "(not set)",
    "(none)",
    "(direct)",
    "(other)",
    "<other>",
    "(data deleted)",
    "(not set)",
    "data deleted",
    "not set",
    "none",
    "null",
    "sin información",
    "sin informacion",
}

#: Nombre de la etiqueta con la que se muestran los valores ausentes.
SIN_INFORMACION = "Sin información"

#: Parámetro de evento que identifica la sesión (documentado por Google).
PARAMETRO_SESION = "ga_session_id"
PARAMETRO_ENGAGEMENT = "engagement_time_msec"

#: Pasos del embudo. El orden es el que se exige en el cálculo.
EMBUDO = [
    {"clave": "view_item", "etiqueta": "Vio un artículo"},
    {"clave": "add_to_cart", "etiqueta": "Añadió al carrito"},
    {"clave": "begin_checkout", "etiqueta": "Inició el pago"},
    {"clave": "purchase", "etiqueta": "Compró"},
]

#: Unidad del embudo: se cuenta la **sesión** (`ga_session_id`), porque los pasos
#: se encadenan dentro de una misma sesión y así se evita atribuir a una persona
#: pasos que ocurrieron en visitas distintas.
UNIDAD_EMBUDO = "sesion"

#: Ventana en la que se exige que los pasos ocurran en orden: 24 horas desde el
#: primer paso de la sesión. Es una decisión documentada, no una propiedad de la
#: fuente.
VENTANA_EMBUDO_HORAS = 24

#: Unidad de la retención: usuario (`user_pseudo_id`), agrupado por semana ISO.
UNIDAD_RETENCION = "usuario"
GRANULARIDAD_COHORTES = "semana"

#: Población original del periodo, medida en la extracción **sin muestreo** del
#: mismo conjunto (1 nov 2020 – 31 ene 2021): 270 154 usuarios distintos y
#: 319 066 filas usuario-día. Se cuentan en la copia original
#: ``data/marketing/v2/usuarios.csv.gz`` (extraída el 2026-11-05) y no se guardan
#: en Atlas: el tablero publica la muestra, no la población.
POBLACION_USUARIOS = 270_154
POBLACION_FILAS_USUARIO_DIA = 319_066
POBLACION_ORIGEN = "data/marketing/v2/usuarios.csv.gz"

#: Predicado con el que se eligen las personas (no las filas) de la muestra.
PREDICADO_MUESTRA = (
    "MOD(ABS(FARM_FINGERPRINT(CONCAT('{semilla}|', user_pseudo_id))), 10000) < {umbral}"
)


@dataclass
class Esquema:
    """Columnas reales de la tabla, tal como las devuelve BigQuery."""

    columnas: dict[str, str] = field(default_factory=dict)

    def tiene(self, columna: str) -> bool:
        return columna in self.columnas

    def faltantes(self, requeridas: dict[str, str]) -> list[str]:
        return [columna for columna in requeridas if not self.tiene(columna)]

    def resumen(self) -> dict[str, Any]:
        return {
            "columnas_detectadas": len(self.columnas),
            "columnas": dict(sorted(self.columnas.items())),
        }


def validar(esquema: Esquema) -> dict[str, Any]:
    """Comprueba el esquema real y devuelve qué se puede calcular y qué no.

    No lanza excepción: la ingesta decide. Si faltan las columnas base, la
    publicación se rechaza; si solo falta una columna opcional, la capacidad
    correspondiente queda desactivada y así se informa en la interfaz.
    """
    errores: list[str] = []
    avisos: list[str] = []

    faltan_base = esquema.faltantes(REQUERIDAS_BASE)
    if faltan_base:
        errores.append(
            "Faltan columnas imprescindibles del esquema GA4: " + ", ".join(faltan_base)
        )

    faltan_compra = esquema.faltantes(REQUERIDAS_COMPRA)
    if faltan_compra:
        errores.append(
            "Faltan columnas de comercio electrónico: " + ", ".join(faltan_compra)
        )

    sin_categoria = not esquema.tiene("items.item_category")
    if sin_categoria:
        avisos.append(
            "La tabla no expone «items.item_category»: la vista por categoría de "
            "producto queda desactivada."
        )

    return {
        "valida": not errores,
        "errores": errores,
        "avisos": avisos,
        "capacidades": {
            "adquisicion": not faltan_base,
            "conversion": not faltan_base,
            "cohortes": not faltan_base,
            "ingresos": not faltan_compra,
            "categoria_producto": esquema.tiene("items.item_category"),
            "ciudad": esquema.tiene("geo.city"),
            "sistema_operativo": esquema.tiene("device.operating_system"),
        },
        "esquema": esquema.resumen(),
    }


def normalizar(valor: Any) -> str:
    """Valor categórico listo para mostrar: los marcadores se agrupan aparte."""
    if valor is None:
        return SIN_INFORMACION
    texto = str(valor).strip()
    if not texto or texto.lower() in MARCADORES:
        return SIN_INFORMACION
    return texto
