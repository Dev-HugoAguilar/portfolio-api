"""Servicio de catálogo y de resolución del dataset activo."""

from __future__ import annotations

from datetime import date

from fastapi import HTTPException, status

from app.models.responses import DatasetInfo, DatasetRef, Periodo
from app.repositories.dataset_repository import DatasetRepository
from app.repositories.sales_repository import SalesRepository


class DatasetNotFound(HTTPException):
    def __init__(self, dataset_id: str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"El dataset '{dataset_id}' no existe en este entorno.",
        )


class CatalogService:
    def __init__(self, datasets: DatasetRepository, sales: SalesRepository) -> None:
        self._datasets = datasets
        self._sales = sales

    # ------------------------------------------------------------- catálogo
    def list_datasets(self) -> list[DatasetInfo]:
        return [self._to_info(meta) for meta in self._datasets.list_all()]

    def get_dataset(self, dataset_id: str) -> DatasetInfo:
        meta = self._datasets.get(dataset_id)
        if meta is None:
            raise DatasetNotFound(dataset_id)
        return self._to_info(meta)

    def default_dataset_id(self) -> str | None:
        """Si solo hay un dataset, se usa por defecto."""
        metas = self._datasets.list_all()
        if len(metas) == 1:
            return metas[0]["dataset_id"]
        return None

    # ------------------------------------------------------------- utilidades
    def metadata_or_404(self, dataset_id: str) -> dict:
        meta = self._datasets.get(dataset_id)
        if meta is None:
            raise DatasetNotFound(dataset_id)
        return meta

    def dataset_ref(self, meta: dict) -> DatasetRef:
        return DatasetRef(
            dataset_id=meta["dataset_id"],
            dataset_version=int(meta.get("dataset_version", 1)),
            esquema_version=str(meta.get("esquema_version", "1.0.0")),
            origen=str(meta.get("origen", "sintetico")),
            moneda=str(meta.get("moneda", "MXN")),
        )

    def period_of(self, meta: dict) -> tuple[date, date]:
        """Periodo cubierto según los metadatos; si faltan, se mide en la base."""
        periodo = meta.get("periodo") or {}
        desde = periodo.get("desde")
        hasta = periodo.get("hasta")
        if desde and hasta:
            return date.fromisoformat(desde), date.fromisoformat(hasta)

        bounds = self._sales.dataset_bounds(meta["dataset_id"])
        if bounds is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El dataset no tiene líneas de venta cargadas.",
            )
        return bounds[0].date(), bounds[1].date()

    def _to_info(self, meta: dict) -> DatasetInfo:
        desde, hasta = self.period_of(meta)
        return DatasetInfo(
            dataset_id=meta["dataset_id"],
            nombre=meta.get("nombre", meta["dataset_id"]),
            descripcion=meta.get("descripcion", ""),
            origen=meta.get("origen", "sintetico"),
            moneda=meta.get("moneda", "MXN"),
            dataset_version=int(meta.get("dataset_version", 1)),
            esquema_version=str(meta.get("esquema_version", "1.0.0")),
            periodo=Periodo(desde=desde, hasta=hasta, dias=(hasta - desde).days + 1),
            filas=int(meta.get("filas", 0)),
            pedidos=int(meta.get("pedidos", 0)),
            clientes=int(meta.get("clientes", 0)),
            productos=int(meta.get("productos", 0)),
            semilla=meta.get("semilla"),
            generado_en=meta.get("generado_en"),
            inmutable=bool(meta.get("inmutable", True)),
        )
