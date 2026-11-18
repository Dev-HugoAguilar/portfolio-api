"""Filtros y modelos del Atlas vial.

Una **única representación del estado**: los ocho endpoints de análisis y la
exportación reciben los mismos parámetros y los normalizan aquí. La lógica entre
filtros es la documentada en la interfaz:

* dentro de una misma dimensión, los valores se combinan con **OR** (por ejemplo,
  dos alcaldías seleccionadas suman ambas);
* entre dimensiones distintas se combina con **AND** (alcaldía Y tipo de evento);
* el recorte por zona visible solo se aplica cuando la persona lo pide de forma
  explícita (``filtrar_por_zona``): mover el mapa no cambia los totales.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel

CONSECUENCIAS = {
    "con_fallecimientos": "Hechos con personas fallecidas",
    "solo_lesionados": "Hechos solo con personas lesionadas",
}


@dataclass
class AtlasFiltros:
    """Filtros ya normalizados (listas sin repetidos y ordenadas)."""

    desde: date | None = None
    hasta: date | None = None
    alcaldias: list[str] = field(default_factory=list)
    tipos_evento: list[str] = field(default_factory=list)
    consecuencias: list[str] = field(default_factory=list)
    tipos_usuario: list[str] = field(default_factory=list)
    tipos_vehiculo: list[str] = field(default_factory=list)
    vialidades: list[str] = field(default_factory=list)
    semaforizada: list[str] = field(default_factory=list)
    sexos: list[str] = field(default_factory=list)
    edad_min: int | None = None
    edad_max: int | None = None
    buscar: str = ""
    bbox: tuple[float, float, float, float] | None = None
    filtrar_por_zona: bool = False

    def condiciones(self, incluir_zona: bool = True) -> dict[str, Any]:
        """Condición de Mongo. Un dict plano, sin ``$where`` ni expresiones."""
        consulta: dict[str, Any] = {}

        rango: dict[str, str] = {}
        if self.desde:
            rango["$gte"] = self.desde.isoformat()
        if self.hasta:
            rango["$lte"] = self.hasta.isoformat()
        if rango:
            consulta["fecha"] = rango

        if self.alcaldias:
            consulta["alcaldia"] = {"$in": self.alcaldias}
        if self.tipos_evento:
            consulta["tipo_evento_norm"] = {"$in": self.tipos_evento}
        if self.vialidades:
            consulta["vialidad_norm"] = {"$in": self.vialidades}
        if self.tipos_usuario:
            consulta["personas_tipo_usuario"] = {"$in": self.tipos_usuario}
        if self.tipos_vehiculo:
            consulta["vehiculo_tipo"] = {"$in": self.tipos_vehiculo}
        if self.sexos:
            consulta["personas_sexo"] = {"$in": self.sexos}

        if self.consecuencias:
            condiciones = []
            if "con_fallecimientos" in self.consecuencias:
                condiciones.append({"personas_fallecidas": {"$gt": 0}})
            if "solo_lesionados" in self.consecuencias:
                condiciones.append(
                    {"personas_fallecidas": {"$lte": 0}, "personas_lesionadas": {"$gt": 0}}
                )
            if condiciones:
                consulta["$or"] = condiciones

        if self.semaforizada:
            valores = []
            for valor in self.semaforizada:
                if valor == "si":
                    valores.append(True)
                elif valor == "no":
                    valores.append(False)
                elif valor == "sin_dato":
                    valores.append(None)
            if valores:
                consulta["semaforizada"] = {"$in": valores}

        if self.edad_min is not None or self.edad_max is not None:
            rango_edad: dict[str, int] = {}
            if self.edad_min is not None:
                rango_edad["$gte"] = self.edad_min
            if self.edad_max is not None:
                rango_edad["$lte"] = self.edad_max
            consulta["personas_edad"] = rango_edad

        if incluir_zona and self.filtrar_por_zona and self.bbox:
            lon_min, lat_min, lon_max, lat_max = self.bbox
            consulta["ubicacion"] = {
                "$geoWithin": {
                    "$box": [[lon_min, lat_min], [lon_max, lat_max]],
                }
            }

        # Búsqueda libre (folio o texto de ubicación). El término se escapa: el
        # usuario nunca puede introducir una expresión regular.
        termino = (self.buscar or "").strip()
        if termino:
            patron = re.escape(termino)
            consulta["$and"] = [
                {
                    "$or": [
                        {"folio": {"$regex": patron, "$options": "i"}},
                        {"colonia": {"$regex": patron, "$options": "i"}},
                        {"punto_1": {"$regex": patron, "$options": "i"}},
                        {"punto_2": {"$regex": patron, "$options": "i"}},
                    ]
                }
            ]
        return consulta

    # --- representación del estado (chips removibles y URL) ------------------
    def activos(self) -> list[dict[str, Any]]:
        """Lista de filtros activos, en el mismo orden en que se muestran."""
        activos: list[dict[str, Any]] = []

        def agregar(clave: str, etiqueta: str, valores: list[str], quitar: dict[str, Any]) -> None:
            if valores:
                activos.append({"clave": clave, "etiqueta": etiqueta, "valores": valores, "quitar": quitar})

        if self.desde or self.hasta:
            agregar(
                "periodo",
                "Periodo",
                [f"{self.desde.isoformat() if self.desde else 'inicio'} → {self.hasta.isoformat() if self.hasta else 'fin'}"],
                {"desde": None, "hasta": None},
            )
        agregar("alcaldia", "Alcaldía", self.alcaldias, {"alcaldia": []})
        agregar("tipo_evento", "Tipo de evento", self.tipos_evento, {"tipo_evento": []})
        agregar(
            "consecuencia",
            "Consecuencia",
            [CONSECUENCIAS.get(valor, valor) for valor in self.consecuencias],
            {"consecuencia": []},
        )
        agregar("tipo_usuario", "Tipo de persona", self.tipos_usuario, {"tipo_usuario": []})
        agregar("tipo_vehiculo", "Tipo de vehículo", self.tipos_vehiculo, {"tipo_vehiculo": []})
        agregar("vialidad", "Vialidad", self.vialidades, {"vialidad": []})
        agregar("sexo", "Sexo registrado", self.sexos, {"sexo": []})
        if self.semaforizada:
            etiquetas = {"si": "Semaforizada", "no": "Sin semáforo", "sin_dato": "Sin dato"}
            agregar(
                "semaforizada",
                "Intersección",
                [etiquetas.get(valor, valor) for valor in self.semaforizada],
                {"semaforizada": []},
            )
        if self.edad_min is not None or self.edad_max is not None:
            agregar(
                "edad",
                "Edad",
                [f"{self.edad_min if self.edad_min is not None else 0}–{self.edad_max if self.edad_max is not None else 'más'}"],
                {"edad_min": None, "edad_max": None},
            )
        if self.filtrar_por_zona and self.bbox:
            agregar(
                "zona",
                "Zona visible",
                ["sí"],
                {"bbox": None, "filtrar_por_zona": False},
            )
        if (self.buscar or "").strip():
            agregar("buscar", "Búsqueda", [self.buscar.strip()], {"buscar": ""})
        return activos

    def consulta(self) -> dict[str, Any]:
        """Parámetros equivalentes para reconstruir el estado en la URL."""
        datos: dict[str, Any] = {}
        if self.desde:
            datos["desde"] = self.desde.isoformat()
        if self.hasta:
            datos["hasta"] = self.hasta.isoformat()
        if self.alcaldias:
            datos["alcaldia"] = self.alcaldias
        if self.tipos_evento:
            datos["tipo_evento"] = self.tipos_evento
        if self.consecuencias:
            datos["consecuencia"] = self.consecuencias
        if self.tipos_usuario:
            datos["tipo_usuario"] = self.tipos_usuario
        if self.tipos_vehiculo:
            datos["tipo_vehiculo"] = self.tipos_vehiculo
        if self.vialidades:
            datos["vialidad"] = self.vialidades
        if self.sexos:
            datos["sexo"] = self.sexos
        if self.semaforizada:
            datos["semaforizada"] = self.semaforizada
        if self.edad_min is not None:
            datos["edad_min"] = self.edad_min
        if self.edad_max is not None:
            datos["edad_max"] = self.edad_max
        if self.bbox and self.filtrar_por_zona:
            datos["bbox"] = ",".join(f"{valor:.5f}" for valor in self.bbox)
            datos["filtrar_por_zona"] = True
        if (self.buscar or "").strip():
            datos["buscar"] = self.buscar.strip()
        return datos


class AtlasFuente(BaseModel):
    """Procedencia y estado de la versión publicada."""

    conjunto: str
    titulo: str
    portal: str
    licencia: str
    notas: str | None = None
    catalogo_modificado: str | None = None
    version: str
    ultima_sincronizacion: str | None = None
    periodo: dict[str, Any] | None = None
    recursos: dict[str, Any] | None = None


class AtlasCatalogo(BaseModel):
    """Respuesta de ``/catalogo``: fuente, cobertura, dominios de filtro y avisos."""

    fuente: AtlasFuente
    cobertura: dict[str, Any]
    filtros: dict[str, Any]
    avisos: list[str]
    metodologia: dict[str, Any]


def normalizar_lista(valores: list[str] | None) -> list[str]:
    """Lista sin repetidos, sin espacios y ordenada (para comparar y exportar)."""
    if not valores:
        return []
    limpios = {valor.strip() for valor in valores if valor and valor.strip()}
    return sorted(limpios)
