"""Endpoints del Atlas vial de la Ciudad de México (datos públicos reales de la SSC).

Una sola versión publicada alimenta todos los endpoints: el mapa, la serie, las
distribuciones, la tabla y la exportación comparten los mismos filtros y el mismo
estado, así que no pueden mostrar cifras distintas del mismo conjunto.

Los cinco recursos oficiales tienen **una fila por folio**, de modo que las
cifras no se multiplican: los hechos se cuentan por folio y las personas se suman
con los campos del propio archivo de hechos. Los desgloses por sexo, edad, tipo de
persona y vehículo describen un registro por hecho: se presentan como «hechos
con registro de…» y su cobertura viaja en cada respuesta.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import AtlasFiltrosDep, AtlasServiceDep, DatabaseDep
from app.atlas.importer import estado_publico

router = APIRouter(prefix="/atlas-vial", tags=["Atlas vial · CDMX"])

_NOTA = (
    "Datos públicos reales de la Secretaría de Seguridad Ciudadana de la Ciudad de "
    "México (SSC), conjunto «Hechos de tránsito registrados por la SSC 2024 (serie "
    "de datos ampliada, no comparativa)», licencia CC-BY-4.0-ESP."
)


@router.get(
    "/estado",
    summary="Versión publicada y estado de la sincronización",
    description=(
        "Indica qué versión de los datos sirve la API, cuándo se sincronizó por última "
        "vez, qué pasó en el último intento y el periodo real de los eventos.\n\n" + _NOTA
    ),
)
def estado(base: DatabaseDep) -> dict:
    """Estado de la versión publicada y del último intento de sincronización."""
    return estado_publico(base.db)


@router.get(
    "/catalogo",
    summary="Fuente, cobertura, dominios de filtro y metodología",
    description=(
        "Devuelve la procedencia (portal, licencia, fechas del catálogo y de cada "
        "recurso), la cobertura del conjunto, los valores reales disponibles para cada "
        "filtro y el texto de metodología.\n\n" + _NOTA
    ),
)
def catalogo(servicio: AtlasServiceDep) -> dict:
    return servicio.catalogo()


@router.get(
    "/resumen",
    summary="Indicadores del conjunto filtrado",
    description=(
        "Hechos registrados, personas lesionadas, personas fallecidas y hechos con "
        "fallecimientos, con su base y su denominador. Incluye la cobertura de "
        "coordenadas, sexo, edad, tipo de persona y vehículo del conjunto filtrado.\n\n" + _NOTA
    ),
)
def resumen(filtros: AtlasFiltrosDep, servicio: AtlasServiceDep) -> dict:
    version, importacion = servicio.version_actual()
    return servicio.resumen(filtros, importacion, version)


@router.get(
    "/territorio",
    summary="Mapa: coroplético por alcaldía, concentración o eventos",
    description=(
        "Agregado por alcaldía (hechos, personas lesionadas y fallecidas, porcentaje "
        "sobre el total filtrado) y puntos para el mapa. Con `modo=concentracion` los "
        "puntos se agrupan por celda en el servidor; la celda depende del zoom y cada "
        "caja informa su conteo. El recorte por zona visible solo se aplica si se pide "
        "de forma explícita (`filtrar_por_zona=true`): mover el mapa no cambia totales.\n\n"
        + _NOTA
    ),
)
def territorio(
    filtros: AtlasFiltrosDep,
    servicio: AtlasServiceDep,
    modo: Annotated[
        str, Query(pattern="^(alcaldias|concentracion|eventos)$")
    ] = "alcaldias",
    celda: Annotated[
        float | None,
        Query(gt=0, le=2, description="Tamaño de celda en grados para la concentración."),
    ] = None,
    limite: Annotated[int, Query(ge=100, le=6_000)] = 2_500,
    incluir_puntos: Annotated[
        bool,
        Query(
            description=(
                "Incluir los puntos del mapa en esta respuesta. La interfaz los pide "
                "aparte a /puntos y los agrupa por zoom, así que aquí se omite."
            )
        ),
    ] = True,
) -> dict:
    version, importacion = servicio.version_actual()
    return servicio.territorio(filtros, version, importacion, modo, celda, incluir_puntos)


@router.get(
    "/colonias",
    summary="Hechos y personas afectadas por colonia",
    description=(
        "Agrega el conjunto filtrado por la **colonia declarada** en el archivo de "
        "hechos, agrupando por `(alcaldía, colonia)` para no fusionar nombres "
        "repetidos en alcaldías distintas. Devuelve hechos, personas lesionadas y "
        "personas fallecidas sumadas sobre los mismos hechos, más la cobertura "
        "(registros asignados y sin colonia) para reconciliar con el total.\n\n"
        "No hay polígonos de colonia en el proyecto: el nombre es el declarado por "
        "la fuente y no se asigna por proximidad ni por centroide.\n\n" + _NOTA
    ),
)
def colonias(
    filtros: AtlasFiltrosDep,
    servicio: AtlasServiceDep,
    orden: str = Query(
        "hechos",
        pattern="^(hechos|lesionadas|fallecidas)$",
        description="Métrica del ranking: hechos, lesionadas o fallecidas.",
    ),
    limite: int = Query(10, description="Colonias a devolver: 10 o 20."),
) -> dict:
    version, _ = servicio.version_actual()
    return servicio.colonias(filtros, version, orden, limite)
    version, _ = servicio.version_actual()
    return servicio.colonias(filtros, version)


@router.get(
    "/celdas",
    summary="Concentración de hechos por celda de una malla métrica estable",
    description=(
        "Agrega el conjunto filtrado en celdas cuadradas de 500 m (0,25 km²) de la "
        "proyección **EPSG:32614 (WGS 84 / UTM 14N)**, con origen fijo y "
        "pertenencia en intervalos semiabiertos: mover el mapa, acercarlo o cambiar "
        "filtros no altera los identificadores de celda.\n\n"
        "Devuelve los conteos de todas las celdas con registros, la geometría de las "
        "diez mayores, el total del conjunto, los hechos ubicables y los excluidos "
        "por ubicación en categorías mutuamente excluyentes (`ausente`, "
        "`no_numerica`, `fuera_de_rango`, `fuera_de_cdmx`). La agregación se hace en "
        "el servidor: no se descargan documentos al navegador.\n\n" + _NOTA
    ),
)
def celdas(filtros: AtlasFiltrosDep, servicio: AtlasServiceDep) -> dict:
    version, _ = servicio.version_actual()
    return servicio.celdas(filtros, version)


@router.get(
    "/puntos",
    summary="Puntos del mapa del conjunto filtrado",
    description=(
        "Todos los hechos con coordenadas válidas del conjunto filtrado, en listas "
        "paralelas (`folios`, `lat`, `lon`, `lesionadas`, `fallecidas`). El navegador "
        "los agrupa según el zoom, así que mover o acercar el mapa no pide datos "
        "nuevos ni cambia los totales. Los registros sin coordenadas utilizables no "
        "aparecen aquí: se informan en `/resumen` y en la tabla.\n\n" + _NOTA
    ),
)
def puntos(filtros: AtlasFiltrosDep, servicio: AtlasServiceDep) -> dict:
    version, _ = servicio.version_actual()
    return servicio.puntos(filtros, version)


@router.get(
    "/geometria",
    summary="Límites de las 16 alcaldías (GeoJSON) con su atribución",
    description=(
        "Polígonos oficiales de las alcaldías de la Ciudad de México, simplificados para "
        "la web. Fuente: catálogo de datos abiertos de la CDMX, conjunto «Límite de "
        "Alcaldías (áreas geoestadísticas municipales)», CC-BY-4.0-ESP, a partir del "
        "Marco Geoestadístico 2020 del INEGI."
    ),
)
def geometria(servicio: AtlasServiceDep) -> dict:
    return servicio.geometria()


@router.get(
    "/tiempo",
    summary="Serie temporal, matriz día × hora y entorno vial",
    description=(
        "Serie por día, semana o mes; matriz de día de la semana × hora; distribución por "
        "tipo de evento, clasificación de la vialidad, tipo de intersección e "
        "intersección semaforizada. Los días sin hechos dentro del periodo cubierto son "
        "cero real; los periodos fuera de cobertura se marcan como sin datos.\n\n" + _NOTA
    ),
)
def tiempo(
    filtros: AtlasFiltrosDep,
    servicio: AtlasServiceDep,
    agregacion: Annotated[str, Query(pattern="^(dia|semana|mes)$")] = "mes",
) -> dict:
    version, _ = servicio.version_actual()
    return servicio.tiempo(filtros, version, agregacion)


@router.get(
    "/personas",
    summary="Personas y vehículos según lo que la fuente registra",
    description=(
        "Distribuciones por tipo de persona, sexo, grupos de edad y tipo de vehículo. "
        "Cada fila indica cuántos hechos tienen ese registro y cuántas personas "
        "lesionadas y fallecidas hubo en esos hechos: no se atribuyen todas las víctimas "
        "de un hecho a su tipo de persona registrado.\n\n" + _NOTA
    ),
)
def personas(filtros: AtlasFiltrosDep, servicio: AtlasServiceDep) -> dict:
    version, _ = servicio.version_actual()
    return servicio.personas(filtros, version)


@router.get(
    "/comparar",
    summary="Comparación de dos alcaldías con el mismo periodo y escala",
    description=(
        "Compara dos alcaldías con los mismos filtros. Devuelve volumen absoluto, "
        "composición porcentual por tipo de evento y la escala relativa de hechos.\n\n" + _NOTA
    ),
)
def comparar(
    filtros: AtlasFiltrosDep,
    servicio: AtlasServiceDep,
    alcaldias: Annotated[
        list[str], Query(alias="comparar", description="Dos alcaldías a comparar.")
    ],
) -> dict:
    version, _ = servicio.version_actual()
    return servicio.comparar(filtros, version, alcaldias)


@router.get(
    "/tabla",
    summary="Tabla de hechos: folio, fecha, hora, alcaldía, tipo, ubicación y personas",
    description=(
        "Filas paginadas del subconjunto filtrado, con orden configurable. Sin datos "
        "personales: solo los campos necesarios para el análisis.\n\n" + _NOTA
    ),
)
def tabla(
    filtros: AtlasFiltrosDep,
    servicio: AtlasServiceDep,
    orden: Annotated[
        str, Query(pattern="^(fecha|hora|alcaldia|tipo_evento|lesionadas|fallecidas|folio)$")
    ] = "fecha",
    sentido: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
    pagina: Annotated[int, Query(ge=1)] = 1,
    tamano: Annotated[int, Query(ge=1, le=200)] = 25,
) -> dict:
    version, _ = servicio.version_actual()
    return servicio.tabla(filtros, version, orden, sentido == "desc", pagina, tamano)


@router.get(
    "/evento/{folio:path}",
    summary="Detalle de un hecho por folio",
    description=(
        "Todos los campos publicados del hecho, incluidas las coordenadas y el estado de "
        "validación espacial. El folio no es único entre versiones: siempre se consulta "
        "la versión publicada. Los folios llevan barras (por ejemplo "
        "`C2C/20241231/00196`), por eso la ruta acepta cualquier resto de ruta.\n\n" + _NOTA
    ),
)
def evento(folio: str, servicio: AtlasServiceDep) -> dict:
    version, _ = servicio.version_actual()
    documento = servicio.evento(version, folio)
    if not documento:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No hay un hecho con el folio {folio} en la versión publicada.",
        )
    return documento


@router.get(
    "/exportar",
    summary="CSV del subconjunto filtrado con fuente, periodo, filtros y versión",
    description=(
        "Exporta las filas del filtro vigente con una cabecera documentada: fuente, "
        "licencia, versión de datos, periodo consultado, filtros aplicados y significado de "
        "las columnas. No incluye datos personales.\n\n" + _NOTA
    ),
)
def exportar(filtros: AtlasFiltrosDep, servicio: AtlasServiceDep) -> Response:
    version, importacion = servicio.version_actual()
    contenido, nombre = servicio.exportar(filtros, version, importacion)
    return Response(
        content=contenido.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
