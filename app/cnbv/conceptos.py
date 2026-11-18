"""Catálogo curado de conceptos del boletín (evidencia, no invención).

Los identificadores corresponden uno a uno con filas reales de la hoja
``EF Resumen`` de los boletines importados; el sufijo ``#n`` es el número de
ocurrencia dentro del boletín (porque algunos conceptos se repiten en el bloque
de etapa 1, etapa 2 y etapa 3). Si un identificador deja de existir en la fuente,
las pruebas lo detectan y la API deja de anunciarlo como destacado.
"""

from __future__ import annotations

#: concepto_id -> nombre corto para el dashboard.
CONCEPTOS_DESTACADOS: dict[str, str] = {
    "activo#1": "Activo total",
    "cartera_de_credito#1": "Cartera de crédito",
    "cartera_de_credito_neto#1": "Cartera de crédito neta",
    "total_de_cartera_de_credito_neto#1": "Cartera total neta",
    "estimacion_preventiva_para_riesgos_crediticios#1": "Estimación preventiva (saldo)",
    "pasivo#1": "Pasivo total",
    "captacion_tradicional#1": "Captación tradicional",
    "depositos_de_exigibilidad_inmediata#1": "Depósitos de exigibilidad inmediata",
    "depositos_a_plazo#1": "Depósitos a plazo",
    "capital_contable#1": "Capital contable",
    "ingresos_por_intereses#1": "Ingresos por intereses",
    "gastos_por_intereses#1": "Gastos por intereses",
    "margen_financiero#1": "Margen financiero",
    "margen_financiero_ajustado_por_riesgos_crediticios#1": "Margen financiero ajustado",
    "resultado_de_la_operacion#1": "Resultado de la operación",
    "resultado_neto#1": "Resultado neto",
}

#: Concepto que se usa cuando la consulta no indica uno.
CONCEPTO_POR_OMISION = "activo#1"

#: Agrupación de "tipo de cartera" construida con filas reales del boletín:
#: la hoja publica la cartera comercial, de consumo y de vivienda partida por
#: etapa (1 = vigente, 2 = en observación, 3 = vencida).
TIPOS_CARTERA: dict[str, dict] = {
    "comercial": {
        "nombre": "Cartera comercial",
        "conceptos": ["creditos_comerciales#1", "creditos_comerciales#2", "creditos_comerciales#3"],
        "nota": "Créditos comerciales por etapa (1, 2 y 3) tal como los publica la CNBV.",
    },
    "consumo": {
        "nombre": "Cartera de consumo",
        "conceptos": ["creditos_de_consumo#1", "creditos_de_consumo#2", "creditos_de_consumo#3"],
        "nota": "Créditos de consumo por etapa (1, 2 y 3).",
    },
    "vivienda": {
        "nombre": "Cartera de vivienda",
        "conceptos": ["creditos_a_la_vivienda#1", "creditos_a_la_vivienda#2", "creditos_a_la_vivienda#3"],
        "nota": "Créditos a la vivienda por etapa (1, 2 y 3).",
    },
}

#: Etapas de la cartera, para etiquetar la ocurrencia de cada concepto.
ETAPAS: dict[int, str] = {1: "Etapa 1", 2: "Etapa 2", 3: "Etapa 3"}


def nombre_visible(concepto_id: str, nombre_original: str) -> str:
    """Nombre corto si el concepto está en el catálogo curado."""
    return CONCEPTOS_DESTACADOS.get(concepto_id, nombre_original)


def es_destacado(concepto_id: str) -> bool:
    return concepto_id in CONCEPTOS_DESTACADOS


def grupos_de_cartera(conceptos_disponibles: set[str]) -> list[dict]:
    """Tipos de cartera con los conceptos que sí existen en los datos."""
    grupos = []
    for clave, datos in TIPOS_CARTERA.items():
        presentes = [c for c in datos["conceptos"] if c in conceptos_disponibles]
        if not presentes:
            continue
        grupos.append(
            {
                "clave": clave,
                "nombre": datos["nombre"],
                "nota": datos["nota"],
                "conceptos": presentes,
            }
        )
    return grupos
