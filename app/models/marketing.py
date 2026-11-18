"""Filtros y definiciones del tablero «Marketing y clientes».

Un **único conjunto de filtros** alimenta las cuatro vistas, la tabla y la
exportación. La semántica es la misma que en el resto del portafolio:

* dentro de una dimensión, los valores se suman (**OR**): dos canales muestran la
  unión de ambos;
* entre dimensiones distintas se cruzan (**AND**): canal Y dispositivo.

Los usuarios se cuentan como **usuarios distintos** en el rango filtrado; nunca se
suman los usuarios de cada día porque eso contaría dos veces a quien vuelve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

#: Definiciones que la interfaz muestra junto a cada cifra y repite en la ficha.
DEFINICIONES = {
    "usuario": (
        "Usuario distinto por `user_pseudo_id` no vacío dentro del rango filtrado. "
        "Se cuenta una sola vez aunque vuelva varios días."
    ),
    "sesion": (
        "Sesión distinta por la pareja `user_pseudo_id` + `ga_session_id` "
        "(parámetro de evento documentado). Los eventos sin ese parámetro quedan "
        "fuera de las métricas de sesión y se informan aparte."
    ),
    "compra": (
        "Evento `purchase` con `ecommerce.transaction_id`. La clave es "
        "usuario + transacción, así que un evento repetido no cuenta dos veces; si "
        "falta el identificador se usa usuario + marca de tiempo y se informa."
    ),
    "ingresos": (
        "Suma de `ecommerce.purchase_revenue_in_usd` **solo** sobre las compras ya "
        "deduplicadas. Moneda: dólares estadounidenses, tal como los publica la "
        "fuente. No se suman ingresos de eventos que no son compras."
    ),
    "conversion": (
        "Compradores distintos ÷ usuarios distintos del mismo rango y filtros. "
        "Numerador: personas que registraron al menos una compra; denominador: "
        "personas con actividad observada. Es una tasa observada en la muestra, no "
        "una tasa de conversión del negocio real."
    ),
    "canal": (
        "`traffic_source.source / traffic_source.medium` de la **primera actividad "
        "observada** del usuario (adquisición de usuario). No es el canal de cada "
        "sesión: la fuente no documenta atribución por sesión en este conjunto."
    ),
    "embudo": (
        "Sesiones que alcanzan cada paso **en orden** (ver artículo → añadir al "
        "carrito → iniciar pago → comprar) y dentro de una ventana de "
        "24 horas desde el primer paso. La unidad es la sesión en los cuatro pasos."
    ),
    "cohorte": (
        "Usuarios agrupados por la semana de su **primera actividad observada** "
        "dentro del conjunto. No es necesariamente su primera actividad real: el "
        "conjunto empieza el 1 de noviembre de 2020."
    ),
    "retencion": (
        "Usuarios de la cohorte con actividad en la semana k. Las celdas cuya "
        "ventana de observación no existe o está incompleta se muestran vacías, "
        "nunca como retención cero."
    ),
    "recompra": (
        "Usuarios de la cohorte que registraron una compra en la semana k. Es "
        "distinto de volver al sitio: alguien puede regresar sin comprar."
    ),
}

#: Avisos fijos sobre la fuente que la interfaz muestra siempre.
AVISOS = [
    "Los datos son una muestra histórica y ofuscada de eventos de comercio "
    "electrónico (Google Merchandise Store): no son resultados actuales ni "
    "información comercial íntegra de Google.",
    "El conjunto puede contener valores de marcador (<Other>, (not set), NULL) y "
    "su consistencia interna es limitada; se conservan como «Sin información».",
    "No se calcula atribución publicitaria ni causalidad: los campos disponibles "
    "solo permiten describir lo observado.",
    "No hay actualización en tiempo real: la versión publicada es una copia de la "
    "muestra, con su fecha de ingesta.",
]

#: Unidad y ventana del embudo (viajan en la respuesta para que la interfaz las
#: muestre sin duplicar la definición).
EMBUDO_UNIDAD = "sesion"
EMBUDO_VENTANA_HORAS = 24

#: Dimensiones por las que se puede abrir la matriz de cohortes.
DIMENSIONES_COHORTE = {
    "canal": "Canal de adquisición",
    "dispositivo": "Dispositivo",
    "pais": "País",
}

#: Ordenamientos admitidos por la tabla de detalle.
ORDEN_TABLA = {
    "fecha": "fecha",
    "evento": "evento",
    "dispositivo": "dispositivo",
    "canal": "canal",
    "articulo": "articulo",
    "ingresos": "ingresos_usd",
}


@dataclass
class MarketingFiltros:
    """Filtros ya normalizados (listas sin repetidos y ordenadas)."""

    desde: date | None = None
    hasta: date | None = None
    canales: list[str] = field(default_factory=list)
    dispositivos: list[str] = field(default_factory=list)
    paises: list[str] = field(default_factory=list)
    busqueda: str = ""

    def condiciones(self) -> dict[str, Any]:
        """Condición de Mongo. Un dict plano, sin expresiones ni ``$where``."""
        consulta: dict[str, Any] = {}

        rango: dict[str, str] = {}
        if self.desde:
            rango["$gte"] = self.desde.isoformat()
        if self.hasta:
            rango["$lte"] = self.hasta.isoformat()
        if rango:
            consulta["fecha"] = rango

        if self.canales:
            consulta["canal"] = {"$in": self.canales}
        if self.dispositivos:
            consulta["dispositivo"] = {"$in": self.dispositivos}
        if self.paises:
            consulta["pais"] = {"$in": self.paises}
        return consulta

    def condiciones_busqueda(self) -> dict[str, Any]:
        """Búsqueda de la tabla: se escapa el término, nunca es una expresión."""
        termino = self.busqueda.strip()
        if not termino:
            return {}
        import re

        patron = re.escape(termino)
        return {
            "$or": [
                {"evento": {"$regex": patron, "$options": "i"}},
                {"articulo": {"$regex": patron, "$options": "i"}},
                {"canal": {"$regex": patron, "$options": "i"}},
                {"dispositivo": {"$regex": patron, "$options": "i"}},
                {"pais": {"$regex": patron, "$options": "i"}},
            ]
        }

    def activos(self) -> list[dict[str, Any]]:
        """Filtros activos para las etiquetas removibles de la interfaz."""
        activos: list[dict[str, Any]] = []
        if self.desde or self.hasta:
            activos.append(
                {
                    "clave": "periodo",
                    "etiqueta": "Periodo",
                    "valores": [
                        f"{self.desde.isoformat() if self.desde else 'inicio'} → "
                        f"{self.hasta.isoformat() if self.hasta else 'fin'}"
                    ],
                    "quitar": {"desde": None, "hasta": None},
                }
            )
        if self.canales:
            activos.append(
                {
                    "clave": "canal",
                    "etiqueta": "Canal",
                    "valores": list(self.canales),
                    "quitar": {"canal": []},
                }
            )
        if self.dispositivos:
            activos.append(
                {
                    "clave": "dispositivo",
                    "etiqueta": "Dispositivo",
                    "valores": list(self.dispositivos),
                    "quitar": {"dispositivo": []},
                }
            )
        if self.paises:
            activos.append(
                {
                    "clave": "pais",
                    "etiqueta": "País",
                    "valores": list(self.paises),
                    "quitar": {"pais": []},
                }
            )
        if self.busqueda.strip():
            activos.append(
                {
                    "clave": "busqueda",
                    "etiqueta": "Búsqueda",
                    "valores": [self.busqueda.strip()],
                    "quitar": {"busqueda": ""},
                }
            )
        return activos

    def periodo_anterior(self) -> tuple[date, date] | None:
        """Ventana del periodo anterior de igual longitud, si existe."""
        if not self.desde or not self.hasta:
            return None
        dias = (self.hasta - self.desde).days + 1
        fin = self.desde - timedelta(days=1)
        inicio = fin - timedelta(days=dias - 1)
        return inicio, fin

    def rango_legible(self) -> str:
        desde = self.desde.isoformat() if self.desde else "inicio"
        hasta = self.hasta.isoformat() if self.hasta else "fin"
        return f"{desde} → {hasta}"


def normalizar_lista(valores: list[str] | None) -> list[str]:
    """Lista sin repetidos, sin espacios y ordenada."""
    if not valores:
        return []
    limpios = {valor.strip() for valor in valores if valor and valor.strip()}
    return sorted(limpios)
