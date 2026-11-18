"""Filtros de consulta del dashboard.

Reglas de normalización (idénticas para todas las métricas):

* Los identificadores son slugs en minúsculas: ``^[a-z0-9][a-z0-9_-]{0,63}$``.
* Cada lista se recorta, se pasa a minúsculas, se deduplica y se ordena.
* Las fechas son **días naturales en UTC**, inclusivos en los dos extremos.
* El rango no puede superar ``max_date_range_days`` ni estar invertido.
* Como máximo ``max_filter_values`` valores por dimensión.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Dimensión de filtro -> campo en MongoDB y etiqueta legible.
DIMENSIONS: dict[str, tuple[str, str]] = {
    "regions": ("region_slug", "Región"),
    "channels": ("canal_slug", "Canal"),
    "categories": ("categoria_slug", "Categoría"),
    "products": ("producto_id", "Producto"),
    "customers": ("cliente_id", "Cliente"),
}


def to_utc_datetime(day: date) -> datetime:
    """Medianoche UTC de un día natural (así se guarda ``fecha``)."""
    return datetime.combine(day, time.min, tzinfo=timezone.utc).replace(tzinfo=None)


class SalesFilters(BaseModel):
    """Filtros ya normalizados; es lo que viaja a MongoDB."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    date_from: date
    date_to: date
    regions: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    customers: tuple[str, ...] = ()

    @field_validator("dataset_id")
    @classmethod
    def _dataset_valido(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if not SLUG_PATTERN.match(value):
            raise ValueError("dataset_id debe ser un slug en minúsculas (a-z, 0-9, -, _)")
        return value

    @field_validator("regions", "channels", "categories", "products", "customers", mode="before")
    @classmethod
    def _listas_normalizadas(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            valores = [value]
        else:
            valores = list(value)
        limpios: list[str] = []
        for item in valores:
            slug = str(item).strip().lower()
            if not slug:
                continue
            if not SLUG_PATTERN.match(slug):
                raise ValueError(f"valor de filtro no válido: {item!r}")
            limpios.append(slug)
        return tuple(sorted(set(limpios)))

    @model_validator(mode="after")
    def _rango_coherente(self) -> "SalesFilters":
        if self.date_from > self.date_to:
            raise ValueError("date_from no puede ser posterior a date_to")
        return self

    # --- utilidades ---------------------------------------------------------
    @property
    def since(self) -> datetime:
        return to_utc_datetime(self.date_from)

    @property
    def until_exclusive(self) -> datetime:
        return to_utc_datetime(self.date_to + timedelta(days=1))

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    def values_for(self, dimension: str) -> tuple[str, ...]:
        return getattr(self, dimension)

    def mongo_match(self, *, exclude: str | None = None) -> dict:
        """Filtro de MongoDB. Con ``exclude`` se omite esa dimensión."""
        match: dict = {
            "dataset_id": self.dataset_id,
            "fecha": {"$gte": self.since, "$lt": self.until_exclusive},
        }
        for dimension, (field, _etiqueta) in DIMENSIONS.items():
            if dimension == exclude:
                continue
            valores = self.values_for(dimension)
            if valores:
                match[field] = {"$in": list(valores)}
        return match

    def normalized(self) -> dict:
        """Filtros tal y como se devuelven al frontend (fechas en ISO)."""
        return {
            "dataset_id": self.dataset_id,
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "regions": list(self.regions),
            "channels": list(self.channels),
            "categories": list(self.categories),
            "products": list(self.products),
            "customers": list(self.customers),
        }

    def with_dates(self, date_from: date, date_to: date) -> "SalesFilters":
        """Mismo filtro de negocio con otras fechas (para comparar periodos)."""
        return self.model_copy(update={"date_from": date_from, "date_to": date_to})


class SalesQuery(BaseModel):
    """Parámetros de paginación y orden del detalle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1)
    sort: str = "fecha"
    order: str = "desc"

    @field_validator("sort")
    @classmethod
    def _sort_permitido(cls, value: str) -> str:
        if value not in SORT_FIELDS:
            raise ValueError(f"orden no permitido; usa uno de: {', '.join(sorted(SORT_FIELDS))}")
        return value

    @field_validator("order")
    @classmethod
    def _order_permitido(cls, value: str) -> str:
        valor = (value or "").lower()
        if valor not in {"asc", "desc"}:
            raise ValueError("order debe ser 'asc' o 'desc'")
        return valor


#: Campos por los que se puede ordenar el detalle.
SORT_FIELDS: dict[str, str] = {
    "fecha": "fecha",
    "venta_neta_cent": "venta_neta_cent",
    "margen_cent": "margen_cent",
    "margen_pct": "margen_cent",  # se ordena por margen absoluto
    "cantidad": "cantidad",
    "producto": "producto_nombre",
    "cliente": "cliente_nombre",
    "region": "region",
    "canal": "canal",
}
