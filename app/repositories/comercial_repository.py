"""Acceso a datos del Atlas comercial (M5) con caché por versión y filtros.

La API nunca importa ni entrena: lee la versión publicada. Este repositorio es la
única puerta a MongoDB para este proyecto, de modo que el servicio y las rutas no
conocen colecciones ni pipelines.
"""

from __future__ import annotations

import time
from typing import Any

from pymongo.database import Database

from app.comercial import consultas, derivados, exploracion, planificacion as plan, precios_analisis
from app.db.mongo import M5_PUBLICACION
from app.comercial.ingesta import estado_publico, version_actual
from app.comercial.inventario import inventario as _inventario
from app.db.mongo import (
    M5_DERIVADOS,
    M5_EVALUACION,
    M5_AUDITORIA_TEMPORAL,
    M5_EVALUACION_MODELOS,
    M5_MODELOS,
    M5_PRECIOS_MANIFEST,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PRONOSTICOS,
)


class ComercialRepositorio:
    """Lecturas del proyecto, con caché corta para no repetir el mismo cálculo."""

    def __init__(self, db: Database, *, cache_ttl_s: int = 120) -> None:
        self._db = db
        self._ttl = max(0, cache_ttl_s)
        self._cache: dict[str, tuple[float, Any]] = {}
        self.ultimo_calculo_ms = 0
        self.ultimo_desde_cache = False

    # -- estado y catálogo ---------------------------------------------------
    @property
    def db(self) -> Database:
        """Base de datos activa (el servicio la usa para validar filtros)."""
        return self._db

    def estado(self) -> dict:
        return estado_publico(self._db)

    def version(self) -> str | None:
        return version_actual(self._db)

    def catalogo(self, version: str) -> dict:
        return self._con_cache(f"catalogo:{version}", lambda: consultas.catalogo(self._db, version))

    def productos(self, version: str, *, texto: str | None, limite: int, desplazamiento: int) -> tuple[list[dict], int]:
        return consultas.buscar_productos(
            self._db, version, texto=texto, limite=limite, desplazamiento=desplazamiento
        )

    def cobertura(self, version: str) -> dict:
        return self._con_cache(f"cobertura:{version}", lambda: consultas.cobertura_publicada(self._db, version))

    # -- pulso ---------------------------------------------------------------
    def pulso(self, version: str, filtros: dict, agregacion: str) -> dict:
        """Series, A/B, concentración, panorama y composición del contexto.

        La clave de caché incluye la versión, el nivel, el territorio (estado, tienda
        y tiendas del contexto), el departamento, la selección de productos y las
        fechas: un resultado de otro contexto nunca se reutiliza.
        """
        clave = "pulso:{}:{}:{}:{}:{}:{}:{}:{}".format(
            version,
            agregacion,
            filtros.get("nivel", consultas.NIVEL_POR_OMISION),
            filtros.get("estado") or "-",
            filtros.get("tienda") or "-",
            filtros.get("departamento") or "-",
            _firma_derivado(self._db, version),
            "|".join(
                [
                    str(filtros["ventana"].desde),
                    str(filtros["ventana"].hasta),
                    ",".join(filtros["tiendas"]),
                    ",".join(filtros.get("tiendas_contexto") or []),
                    ",".join(filtros["categorias"]),
                    ",".join(filtros["departamentos"]),
                    ",".join(filtros["productos"]),
                    ",".join(filtros.get("departamentos_panel") or []),
                    _firma_ab(filtros),
                ]
            ),
        )

        def _calcular() -> dict:
            fechas = consultas.mapa_fechas(self._db)
            series = consultas.series_por_nivel(
                self._db, version, filtros=filtros, agregacion=agregacion, fechas=fechas
            )
            concentracion = consultas.concentracion(self._db, version, filtros=filtros, fechas=fechas)
            comparacion = consultas.comparacion_ab(self._db, version, filtros=filtros, fechas=fechas)
            total_seleccion = sum(serie["total"] for serie in series["series"])
            total_contexto = None
            if filtros.get("nivel") == "producto":
                total_contexto = consultas.total_departamento(
                    self._db,
                    version,
                    tiendas=consultas._tiendas_contexto(filtros),
                    departamento=filtros["departamento"],
                    ventana=filtros["ventana"],
                    fechas=fechas,
                )
            resumen = consultas.resumen(
                series=series,
                filtros=filtros,
                concentracion_datos=concentracion,
                total_contexto=total_contexto,
                total_seleccion=total_seleccion,
            )
            return {
                "series": series,
                "concentracion": concentracion,
                "comparacion": comparacion,
                "panorama": consultas.panorama(self._db, version, filtros=filtros, fechas=fechas),
                "composicion": consultas.composicion(self._db, version, filtros=filtros, fechas=fechas),
                "resumen": resumen,
                "derivado": derivados.info(self._db, version),
            }

        return self._con_cache(clave, _calcular)

    def historico(self, version: str, filtros: dict, anio: int, modo: str) -> dict:
        """Comparación histórica del contexto contra años anteriores completos."""
        clave = "historico|{}|{}|{}|{}|{}|{}|{}|{}".format(
            version,
            _firma_derivado(self._db, version),
            anio,
            modo,
            filtros.get("estado") or "-",
            ",".join(filtros["tiendas"]),
            ",".join(filtros["categorias"]),
            ",".join(filtros["departamentos"]),
        )
        return self._con_cache(
            clave,
            lambda: consultas.historico(
                self._db, version, filtros=filtros, anio=anio, modo=modo
            ),
        )

    # -- productos (búsqueda del explorador) ---------------------------------
    def productos_contexto(
        self,
        version: str,
        *,
        tiendas: list[str],
        departamento: str | None,
        categorias: list[str],
        desde,
        hasta,
        texto: str | None,
        orden: str,
        limite: int,
        desplazamiento: int,
    ) -> dict:
        clave = "productos:{}:{}:{}:{}:{}:{}:{}:{}:{}:{}:{}".format(
            version,
            ",".join(sorted(tiendas)),
            departamento or "-",
            ",".join(sorted(categorias)),
            desde,
            hasta,
            (texto or "").strip().lower(),
            orden,
            limite,
            desplazamiento,
            _firma_derivado(self._db, version),
        )
        return self._con_cache(
            clave,
            lambda: consultas.buscar_productos_contexto(
                self._db,
                version,
                tiendas=list(tiendas),
                departamento=departamento,
                categorias=categorias,
                desde=desde,
                hasta=hasta,
                texto=texto,
                orden=orden,
                limite=limite,
                desplazamiento=desplazamiento,
            ),
        )

    # -- pronósticos y evaluación -------------------------------------------
    def pronosticos(self, version: str, nivel: str) -> dict | None:
        return self._con_cache(
            f"pron:{version}:{nivel}",
            lambda: self._db[M5_PRONOSTICOS].find_one({"version": version, "nivel": nivel, "tipo": "futuro"}),
        )

    def backtesting(self, version: str, nivel: str) -> list[dict]:
        return self._con_cache(
            f"back:{version}:{nivel}",
            lambda: list(
                self._db[M5_PRONOSTICOS]
                .find({"version": version, "nivel": nivel, "tipo": "backtesting"})
                .sort("origen_d", 1)
            ),
        )

    def evaluacion(self, version: str, nivel: str) -> dict | None:
        return self._con_cache(
            f"eval:{version}:{nivel}",
            lambda: self._db[M5_EVALUACION].find_one({"version": version, "nivel": nivel}),
        )

    # -- modelo predictivo ---------------------------------------------------
    def ejecuciones(self, version: str) -> list[dict]:
        """Ejecuciones publicadas o en desarrollo de la versión (más recientes primero)."""
        return self._con_cache(
            f"modelos:{version}",
            lambda: list(
                self._db[M5_MODELOS]
                .find({"version": version})
                .sort([("recomendado", -1), ("creado_en", -1)])
                .limit(40)
            ),
        )

    def ejecucion(self, version: str, run_id: str) -> dict | None:
        return self._con_cache(
            f"modelo:{version}:{run_id}",
            lambda: self._db[M5_MODELOS].find_one({"_id": run_id, "version": version}),
        )

    def ejecucion_recomendada(self, version: str) -> dict | None:
        return self._con_cache(
            f"modelo-recomendado:{version}",
            lambda: self._db[M5_MODELOS].find_one(
                {"version": version, "recomendado": True}
            )
            or self._db[M5_MODELOS].find_one(
                {"version": version, "estado": "publicada"}, sort=[("creado_en", -1)]
            ),
        )

    def predictores(self, version: str, run_id: str, *, origen: int, dimension: str, claves: list[str] | None) -> list[dict]:
        """Predicciones agregadas de una ejecución y un origen (revisión activa)."""
        revision = (self.publicacion() or {}).get("_id")
        consulta: dict = {
            "run_id": run_id,
            "version": version,
            "origen_d": origen,
            "dimension": dimension,
            **({"revision": revision} if revision else {"revision": {"$exists": False}}),
        }
        if claves:
            consulta["clave"] = {"$in": list(claves)}
        return self._con_cache(
            f"pred:{version}:{run_id}:{(self.publicacion() or {}).get('_id', 'legado')}:{origen}:{dimension}:{','.join(sorted(claves or []))}",
            lambda: list(
                self._db[M5_PREDICCIONES].find(consulta, {"_id": 0}).sort("clave", 1)
            ),
        )

    def predictores_series(self, version: str, run_id: str, *, tienda: str, items: list[str]) -> list[dict]:
        """Predicciones artículo × tienda de la revisión activa de la publicación."""
        revision = (self.publicacion() or {}).get("_id")
        filtro = (
            {"revision": revision} if revision else {"revision": {"$exists": False}}
        )
        return self._con_cache(
            f"pred-series:{version}:{run_id}:{revision or 'legado'}:{tienda}:{','.join(sorted(items))}",
            lambda: list(
                self._db[M5_PREDICCIONES_SERIES]
                .find(
                    {"run_id": run_id, "store_id": tienda, "item_id": {"$in": list(items)}, **filtro},
                    {"_id": 0},
                )
                .sort("item_id", 1)
            ),
        )

    def evaluaciones_modelo(
        self, version: str, run_id: str, *, etapa: str, origen: int | None = None
    ) -> list[dict]:
        consulta: dict = {"run_id": run_id, "version": version, "etapa": etapa}
        if origen:
            consulta["origen_d"] = origen
        return self._con_cache(
            f"eval-modelo:{version}:{run_id}:{etapa}:{origen or 'todos'}",
            lambda: list(
                self._db[M5_EVALUACION_MODELOS].find(consulta, {"_id": 0}).sort("origen_d", 1)
            ),
        )

    def manifiesto_precios(self) -> dict | None:
        return self._con_cache(
            "precios-manifiesto",
            lambda: self._db[M5_PRECIOS_MANIFEST].find_one(sort=[("importado_en", -1)]),
        )

    # -- revisión de publicación ---------------------------------------------
    def origenes_publicados(self, version: str, run_id: str) -> list[int]:
        """Orígenes con predicciones por día publicadas en la revisión activa."""
        revision = (self.publicacion() or {}).get("_id")
        filtro: dict = {"version": version, "run_id": run_id}
        filtro["revision"] = revision if revision else {"$exists": False}
        return self._con_cache(
            f"origenes-publicados:{version}:{run_id}:{revision or 'legado'}",
            lambda: sorted(
                {int(documento["origen_d"]) for documento in self._db[M5_PREDICCIONES].find(filtro, {"origen_d": 1})}
            ),
        )

    def auditoria_temporal(self, version: str) -> dict | None:
        """Auditoría de separación temporal guardada para la versión publicada."""
        return self._con_cache(
            f"auditoria-temporal:{version}",
            lambda: self._db[M5_AUDITORIA_TEMPORAL].find_one(
                {"version": version}, {"_id": 0}, sort=[("generado_en", -1)]
            ),
        )

    def publicacion(self) -> dict | None:
        """Revisión activa de la publicación (política de precisión incluida)."""
        return self._con_cache(
            "publicacion-activa",
            lambda: self._db[M5_PUBLICACION].find_one({"activa": True}),
        )

    # -- planificación de ventas y evaluación al horizonte --------------------
    def planificacion(self, version: str, run_id: str, **parametros) -> dict:
        """Página del plan de ventas, con clave de caché completa por parámetros."""
        clave = "plan:{}:{}:{}".format(
            version,
            run_id,
            ";".join(f"{nombre}={valor}" for nombre, valor in sorted(parametros.items()) if valor is not None),
        )
        return self._con_cache(
            clave, lambda: plan.planificacion(self._db, version, run_id, **parametros)
        )

    def planificacion_completa(self, version: str, run_id: str, **parametros) -> dict:
        clave = "plan-todo:{}:{}:{}".format(
            version,
            run_id,
            ";".join(f"{nombre}={valor}" for nombre, valor in sorted(parametros.items()) if valor is not None),
        )
        return self._con_cache(
            clave, lambda: plan.planificacion_completa(self._db, version, run_id, **parametros)
        )

    def detalle_plan(
        self, version: str, run_id: str, *, tienda: str, item: str, revision: str | None = None
    ) -> dict | None:
        return self._con_cache(
            f"plan-serie:{version}:{run_id}:{revision or 'legado'}:{tienda}:{item}",
            lambda: plan.detalle_serie(
                self._db, version, run_id, tienda=tienda, item=item, revision=revision
            ),
        )

    def evaluacion_horizonte(self, version: str, run_id: str, **parametros) -> dict:
        clave = "plan-eval28:{}:{}:{}".format(
            version,
            run_id,
            ";".join(f"{nombre}={valor}" for nombre, valor in sorted(parametros.items()) if valor is not None),
        )
        return self._con_cache(
            clave, lambda: plan.evaluacion_horizonte(self._db, version, run_id, **parametros)
        )

    # -- exploración descriptiva ---------------------------------------------
    def fechas(self, version: str) -> dict:
        """``d_N`` → fecha del calendario importado (lectura costosa, cacheada)."""
        return self._con_cache(f"fechas:{version}", lambda: consultas.mapa_fechas(self._db))

    def existencias(self, version: str) -> dict:
        """Series artículo × tienda realmente importadas, por tienda."""
        return self._con_cache(
            f"existencias:{version}", lambda: exploracion.existencias(self._db, version)
        )

    def comparacion_tienda_categoria(self, version: str, filtros: dict) -> dict:
        clave = "comparacion-tc:{}:{}:{}:{}".format(
            version,
            ",".join(filtros["tiendas"]),
            ",".join(filtros["categorias"]),
            _firma_exploracion(filtros),
        )
        return self._con_cache(
            clave,
            lambda: exploracion.comparacion_tienda_categoria(
                self._db, version, filtros=filtros, fechas=self.fechas(version)
            ),
        )

    def conteos_contexto(self, version: str, filtros: dict) -> dict:
        clave = "conteos:{}:{}:{}:{}".format(
            version,
            ",".join(filtros["tiendas"]),
            ",".join(filtros["categorias"]),
            _firma_exploracion(filtros),
        )

        def calcular() -> dict:
            fechas = self.fechas(version)
            return exploracion.conteos_contexto(
                self._db,
                version,
                filtros=filtros,
                fechas=fechas,
                dias=consultas._dias_de(filtros["ventana"], fechas),
            )

        return self._con_cache(clave, calcular)

    def estacionalidad(self, version: str, filtros: dict, dimension: str) -> dict:
        clave = "estacionalidad:{}:{}:{}:{}".format(
            version, dimension, ",".join(filtros["tiendas"]), _firma_exploracion(filtros)
        )
        return self._con_cache(
            clave,
            lambda: exploracion.estacionalidad(
                self._db, version, filtros=filtros, fechas=self.fechas(version), dimension=dimension
            ),
        )

    def matriz_temporal(self, version: str, filtros: dict, anio: int) -> dict:
        clave = "matriz:{}:{}:{}:{}".format(
            version, anio, ",".join(filtros["tiendas"]), _firma_exploracion(filtros)
        )
        return self._con_cache(
            clave,
            lambda: exploracion.matriz_temporal(
                self._db, version, filtros=filtros, fechas=self.fechas(version), anio=anio
            ),
        )

    def articulo(self, version: str, filtros: dict, *, item_id: str, tienda: str) -> dict:
        clave = "articulo:{}:{}:{}:{}".format(
            version, item_id, tienda, _firma_exploracion(filtros)
        )
        return self._con_cache(
            clave,
            lambda: exploracion.detalle_articulo(
                self._db,
                version,
                filtros=filtros,
                fechas=self.fechas(version),
                item_id=item_id,
                tienda=tienda,
            ),
        )

    def intermitencia(
        self,
        version: str,
        *,
        tienda: str,
        desde,
        hasta,
        categorias: list[str],
        departamentos: list[str],
        pagina: int,
        tamano: int,
        orden: str,
    ) -> dict:
        clave = "intermitencia:{}:{}:{}:{}:{}:{}:{}:{}:{}".format(
            version,
            tienda,
            desde,
            hasta,
            ",".join(sorted(categorias)),
            ",".join(sorted(departamentos)),
            pagina,
            tamano,
            orden,
        )

        def calcular() -> dict:
            return exploracion.intermitencia(
                self._db,
                version,
                tienda=tienda,
                desde=desde,
                hasta=hasta,
                fechas=self.fechas(version),
                categorias=categorias,
                departamentos=departamentos,
                pagina=pagina,
                tamano=tamano,
                orden=orden,
            )

        return self._con_cache(clave, calcular)

    def precios_distribucion(self, version_precios: str, datos: dict) -> dict:
        clave = "precios-dist:{}:{}:{}:{}:{}:{}:{}".format(
            version_precios,
            datos["tienda"],
            datos["desde"],
            datos["hasta"],
            ",".join(sorted(datos["categorias"])),
            ",".join(sorted(datos["departamentos"])),
            ",".join(sorted(datos["items_permitidos"] or [])),
        )
        return self._con_cache(
            clave,
            lambda: precios_analisis.distribucion(
                self._db,
                version_precios,
                tienda=datos["tienda"],
                desde=datos["desde"],
                hasta=datos["hasta"],
                categorias=datos["categorias"],
                departamentos=datos["departamentos"],
                items_permitidos=datos["items_permitidos"],
                version_ventas=datos["version_ventas"],
            ),
        )

    def precios_cambios(self, version_precios: str, datos: dict) -> dict:
        clave = "precios-cambios:{}:{}:{}:{}:{}:{}".format(
            version_precios,
            datos["tienda"],
            datos["desde"],
            datos["hasta"],
            ",".join(sorted(datos["categorias"])),
            ",".join(sorted(datos["departamentos"])),
        )
        return self._con_cache(
            clave,
            lambda: precios_analisis.cambios(
                self._db,
                version_precios,
                tienda=datos["tienda"],
                desde=datos["desde"],
                hasta=datos["hasta"],
                categorias=datos["categorias"],
                departamentos=datos["departamentos"],
            ),
        )

    def precios_evolucion(self, version_precios: str, datos: dict) -> dict:
        clave = "precios-evo:{}:{}:{}:{}:{}".format(
            version_precios, datos["tienda"], datos["item_id"], datos["desde"], datos["hasta"]
        )
        return self._con_cache(
            clave,
            lambda: precios_analisis.evolucion(
                self._db,
                version_precios,
                tienda=datos["tienda"],
                item_id=datos["item_id"],
                desde=datos["desde"],
                hasta=datos["hasta"],
            ),
        )

    # -- inventario ----------------------------------------------------------
    def inventario(self) -> dict:
        return _inventario(self._db)

    # -- utilidades ----------------------------------------------------------
    def _con_cache(self, clave: str, calcular):
        ahora = time.monotonic()
        guardado = self._cache.get(clave)
        if guardado and ahora - guardado[0] < self._ttl:
            self.ultimo_calculo_ms = 0
            self.ultimo_desde_cache = True
            return guardado[1]
        self.ultimo_desde_cache = False
        inicio = time.perf_counter()
        valor = calcular()
        self.ultimo_calculo_ms = int((time.perf_counter() - inicio) * 1000)
        if self._ttl:
            self._cache[clave] = (ahora, valor)
        return valor

    def invalidar(self) -> None:
        self._cache.clear()


_instancias: dict[tuple[int, int], "ComercialRepositorio"] = {}


def _firma_ab(filtros: dict) -> str:
    """Firma de los intervalos A/B (una caché no puede mezclar comparaciones)."""
    partes = []
    for clave in ("a", "b"):
        ventana = filtros.get(clave)
        partes.append(f"{ventana.desde}:{ventana.hasta}" if ventana else "-")
    return "|".join(partes)


def _firma_exploracion(filtros: dict) -> str:
    """Firma completa de los filtros de una vista descriptiva.

    Incluye periodo, intervalos A/B, departamentos y productos: la caché nunca puede
    devolver un análisis de otro contexto.
    """
    ventana = filtros.get("ventana")
    contexto = (
        f"{ventana.desde}:{ventana.hasta}:{ventana.dias}" if ventana else "-"
    )
    return "|".join(
        [
            contexto,
            _firma_ab(filtros),
            ",".join(sorted(filtros.get("departamentos") or [])),
            ",".join(sorted(filtros.get("productos") or [])),
            str(filtros.get("nivel") or "-"),
            str(filtros.get("estado") or "-"),
            str(filtros.get("tienda") or "-"),
        ]
    )


def _firma_derivado(db: Database, version: str) -> str:
    """Huella del derivado disponible: si cambia, la caché deja de valer."""
    documento = db[M5_DERIVADOS].find_one(
        {"version": version, "derivado": "departamento_tienda"}, {"estado": 1, "huella": 1}
    )
    if not documento:
        return "sin-derivado"
    return f"{documento.get('estado')}:{documento.get('huella')}"


def obtener_repositorio(db: Database, *, cache_ttl_s: int = 120) -> ComercialRepositorio:
    """Repositorio único por proceso y base.

    La caché por versión y filtros debe sobrevivir entre peticiones (y compartirse con
    el precalentamiento del arranque); crear uno nuevo en cada petición la anularía y
    la concentración por producto se recalcularía siempre.
    """
    clave = (id(db), cache_ttl_s)
    instancia = _instancias.get(clave)
    if instancia is None or instancia.db is not db:
        instancia = ComercialRepositorio(db, cache_ttl_s=cache_ttl_s)
        _instancias[clave] = instancia
    return instancia
