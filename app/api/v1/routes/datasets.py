"""Catálogo de datasets disponibles."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CatalogDep
from app.models.responses import DatasetInfo

router = APIRouter(prefix="/datasets", tags=["Datasets"])


@router.get(
    "",
    response_model=list[DatasetInfo],
    summary="Lista los datasets cargados",
    description=(
        "Devuelve los metadatos de cada dataset: origen, periodo cubierto, moneda, "
        "versión de esquema y número de filas. Un dataset es inmutable una vez cargado."
    ),
)
def list_datasets(catalog: CatalogDep) -> list[DatasetInfo]:
    return catalog.list_datasets()


@router.get(
    "/{dataset_id}",
    response_model=DatasetInfo,
    summary="Metadatos de un dataset",
)
def get_dataset(dataset_id: str, catalog: CatalogDep) -> DatasetInfo:
    return catalog.get_dataset(dataset_id)
