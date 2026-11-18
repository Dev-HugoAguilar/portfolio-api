"""Lógica del Atlas vial: indicadores, cobertura y textos de metodología.

Principios que se aplican en cada respuesta:

* **Hechos y personas se cuentan por separado.** Los indicadores de personas
  suman ``personas_lesionadas`` y ``personas_fallecidas`` del archivo de hechos;
  nunca se multiplican por los desgloses de personas.
* **Cada porcentaje lleva su denominador** (``denominador`` y ``base``), porque
  el conjunto filtrado puede ser una parte pequeña del total.
* **La cobertura se informa siempre**: cuántos hechos del conjunto filtrado
  tienen coordenadas, sexo, edad, tipo de usuario o vehículo. Un desglose parcial
  se presenta como parcial.
* **No hay tasas por población ni atribuciones causales**: no se incorporan
  denominadores de población, viajes o tráfico, y la concentración de hechos no
  se presenta como riesgo individual.
* **Una sola fuente por versión**: la publicada; el periodo de los eventos, la
  fecha de los recursos y la última sincronización viajan en cada respuesta.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.atlas.geometry import ATRIBUCION, obtener_geometria
from app.config import Settings
from app.models.atlas import AtlasFiltros
from app.repositories.atlas_repository import AtlasRepository

DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

#: Grupos de edad para las distribuciones (se documentan en la metodología).
GRUPOS_EDAD = [
    ("0–14", 0, 14),
    ("15–24", 15, 24),
    ("25–34", 25, 34),
    ("35–44", 35, 44),
    ("45–54", 45, 54),
    ("55–64", 55, 64),
    ("65 y más", 65, 200),
]

#: Avisos fijos: límites del conjunto que la interfaz muestra siempre.
AVISOS_FIJOS = [
    "Cada fila del conjunto es un hecho de tránsito registrado por la SSC; las "
    "personas y los vehículos se cuentan con los campos del propio archivo de hechos.",
    "Los recursos de personas (sexo, edad y tipo de usuario) traen un registro por "
    "hecho: describen a la persona principal del expediente, no a todas las víctimas. "
    "Por eso se muestran como «hechos con registro de…», no como totales de personas.",
    "El recurso de vehículos registra el vehículo principal de cada hecho; su "
    "distribución describe hechos, no el parque vehicular involucrado.",
    "La concentración de hechos es descriptiva: no mide riesgo individual ni "
    "atribuye causas a vialidades, semáforos o vehículos.",
    "No se calculan tasas por población, viajes o tráfico: el conjunto no incluye "
    "esos denominadores.",
    "Los hechos sin coordenadas utilizables se conservan y se informan: no se "
    "descartan ni se reasignan a una alcaldía para mejorar el mapa.",
]


class AtlasService:
    def __init__(self, repo: AtlasRepository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings

    # ------------------------------------------------------------------ fuente
    def version_actual(self) -> tuple[str, dict[str, Any]]:
        version = self.repo.version_publicada()
        if not version:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Todavía no hay una versión publicada del Atlas vial. Ejecuta "
                    "`python -m scripts.importar_atlas_vial` para descargar y validar "
                    "los recursos oficiales."
                ),
            )
        importacion = self.repo.importacion(version) or {}
        return version, importacion

    def _fuente(self, version: str, importacion: dict[str, Any]) -> dict[str, Any]:
        catalogo = importacion.get("fuente") or {}
        return {
            "conjunto": catalogo.get("conjunto") or "",
            "titulo": catalogo.get("titulo") or "",
            "portal": catalogo.get("portal") or "",
            "licencia": catalogo.get("licencia") or "",
            "notas": catalogo.get("notas") or "",
            "catalogo_modificado": catalogo.get("catalogo_modificado"),
            "version": version,
            "ultima_sincronizacion": _iso(importacion.get("publicado_en")),
            "periodo": importacion.get("periodo"),
            "recursos": {
                clave: {
                    "nombre": recurso.get("nombre"),
                    "url": recurso.get("url"),
                    "bytes": recurso.get("bytes"),
                    "sha256": recurso.get("sha256"),
                    "modificado_en_catalogo": recurso.get("modificado_en_catalogo"),
                }
                for clave, recurso in (importacion.get("recursos") or {}).items()
            },
        }

    def _periodo(self, importacion: dict[str, Any], filtros: AtlasFiltros) -> dict[str, Any]:
        periodo = importacion.get("periodo") or {}
        desde = periodo.get("desde")
        hasta = periodo.get("hasta")
        return {
            "desde": desde,
            "hasta": hasta,
            "consulta_desde": filtros.desde.isoformat() if filtros.desde else desde,
            "consulta_hasta": filtros.hasta.isoformat() if filtros.hasta else hasta,
        }

    # ------------------------------------------------------------------ catálogo
    def catalogo(self) -> dict[str, Any]:
        version, importacion = self.version_actual()
        valores = self.repo.valores_filtro(version)
        cobertura = self.repo.resumen(version, AtlasFiltros())
        hechos = cobertura.get("hechos", 0) or 0
        return {
            "fuente": self._fuente(version, importacion),
            "cobertura": {
                "hechos": hechos,
                "personas_lesionadas": cobertura.get("lesionadas", 0) or 0,
                "personas_fallecidas": cobertura.get("fallecidas", 0) or 0,
                "hechos_con_fallecimientos": cobertura.get("con_fallecimientos", 0) or 0,
                "con_coordenadas": cobertura.get("con_coordenadas", 0) or 0,
                "sin_coordenadas": cobertura.get("sin_coordenadas", 0) or 0,
                "fuera_de_cdmx": cobertura.get("fuera_de_cdmx", 0) or 0,
                "con_sexo": cobertura.get("con_sexo", 0) or 0,
                "con_edad": cobertura.get("con_edad", 0) or 0,
                "con_tipo_usuario": cobertura.get("con_tipo_usuario", 0) or 0,
                "con_vehiculo": cobertura.get("con_vehiculo", 0) or 0,
                "sin_fecha": cobertura.get("sin_fecha", 0) or 0,
                "sin_hora": cobertura.get("sin_hora", 0) or 0,
                "coordenadas": (importacion.get("incidencias") or {}).get("coordenadas"),
                "avisos": AVISOS_FIJOS,
            },
            "filtros": {
                **valores,
                "consecuencias": [
                    {"valor": "con_fallecimientos", "etiqueta": "Con personas fallecidas"},
                    {"valor": "solo_lesionados", "etiqueta": "Solo con personas lesionadas"},
                ],
                "semaforizada": [
                    {"valor": "si", "etiqueta": "Semaforizada"},
                    {"valor": "no", "etiqueta": "Sin semáforo"},
                    {"valor": "sin_dato", "etiqueta": "Sin dato"},
                ],
                "grupos_edad": [{"etiqueta": etiqueta, "desde": desde, "hasta": hasta} for etiqueta, desde, hasta in GRUPOS_EDAD],
                "periodo": self._periodo(importacion, AtlasFiltros()),
            },
            "avisos": AVISOS_FIJOS,
            "metodologia": self.metodologia(),
        }

    def metodologia(self) -> dict[str, Any]:
        return {
            "fuente": {
                "conjunto": "Hechos de tránsito registrados por la SSC 2024 (serie de datos ampliada, no comparativa)",
                "portal": "https://datos.cdmx.gob.mx/dataset/hechos-de-transito-registrados-por-la-ssc-2024-serie-de-datos-ampliada-no-comparativa",
                "autoridad": "Secretaría de Seguridad Ciudadana de la Ciudad de México (SSC)",
                "licencia": "CC-BY-4.0-ESP",
                "geometria": ATRIBUCION,
            },
            "fechas": {
                "catalogo": "Fecha de modificación del conjunto en el catálogo.",
                "recurso": "Fecha de modificación de cada archivo (nivel de recurso).",
                "descarga": "Fecha de la última sincronización correcta.",
                "eventos": "Periodo real de los hechos dentro del archivo; no se deduce de las fechas del catálogo.",
            },
            "granularidad": {
                "hechos": "Una fila por folio: 30 655 filas y 30 655 folios únicos.",
                "personas": "Un registro por folio en cada recurso de personas: describe a la persona principal del expediente.",
                "vehiculos": "Un registro por folio: describe el vehículo principal del hecho.",
                "clave": "version + folio. Los cinco recursos se cruzan 1:1 en la importación, sin uniones que multipliquen registros.",
            },
            "metricas": {
                "hechos": "Conteo de folios únicos.",
                "personas_lesionadas": "Suma de personas_lesionadas del archivo de hechos.",
                "personas_fallecidas": "Suma de personas_fallecidas del archivo de hechos.",
                "hechos_con_fallecimientos": "Hechos con personas_fallecidas > 0.",
                "porcentajes": "Cada porcentaje declara su denominador y su base.",
            },
            "tratamiento": {
                "desconocidos": "«SD» y «NA» se conservan como «Sin dato»; nunca se convierten en cero.",
                "desgloses": "Sexo, edad y tipo de usuario no se suman entre sí: pueden describir a las mismas personas.",
                "sin_coordenadas": "Los registros sin coordenadas utilizables se cuentan y se informan; no se eliminan.",
                "fechas_sin_datos": "Los días o meses fuera del periodo cubierto se marcan como sin datos; no se rellenan con ceros.",
            },
            "limitaciones": [
                "Los recursos de personas y vehículos describen un registro por hecho, no todas las víctimas ni todos los vehículos.",
                "No hay tasas por población, viajes o tráfico: el conjunto no trae esos denominadores.",
                "La serie es anual y no comparativa: la propia fuente advierte que no debe usarse para comparar entre años.",
                "El conjunto no documenta la causa de los hechos: los patrones son descriptivos y no implican causalidad.",
            ],
        }

    # ------------------------------------------------------------------ resumen
    def resumen(self, filtros: AtlasFiltros, importacion: dict[str, Any], version: str) -> dict[str, Any]:
        datos = self.repo.resumen(version, filtros)
        hechos = datos.get("hechos", 0) or 0
        lesionadas = datos.get("lesionadas", 0) or 0
        fallecidas = datos.get("fallecidas", 0) or 0
        con_coordenadas = datos.get("con_coordenadas", 0) or 0
        return {
            "indicadores": {
                "hechos": {
                    "valor": hechos,
                    "base": "folios únicos del conjunto filtrado",
                },
                "personas_lesionadas": {
                    "valor": lesionadas,
                    "base": "suma del campo personas_lesionadas",
                },
                "personas_fallecidas": {
                    "valor": fallecidas,
                    "base": "suma del campo personas_fallecidas",
                },
                "hechos_con_fallecimientos": {
                    "valor": datos.get("con_fallecimientos", 0) or 0,
                    "base": "hechos con personas_fallecidas > 0",
                    "porcentaje": _pct(datos.get("con_fallecimientos", 0) or 0, hechos),
                    "denominador": hechos,
                },
            },
            "personas_por_hecho": _ratio(lesionadas + fallecidas, hechos),
            "cobertura": {
                "hechos": hechos,
                "con_coordenadas": con_coordenadas,
                "sin_coordenadas": datos.get("sin_coordenadas", 0) or 0,
                "fuera_de_cdmx": datos.get("fuera_de_cdmx", 0) or 0,
                "porcentaje_con_coordenadas": _pct(con_coordenadas, hechos),
                "con_sexo": datos.get("con_sexo", 0) or 0,
                "con_edad": datos.get("con_edad", 0) or 0,
                "con_tipo_usuario": datos.get("con_tipo_usuario", 0) or 0,
                "con_vehiculo": datos.get("con_vehiculo", 0) or 0,
                "porcentaje_con_tipo_usuario": _pct(datos.get("con_tipo_usuario", 0) or 0, hechos),
                "sin_fecha": datos.get("sin_fecha", 0) or 0,
                "sin_hora": datos.get("sin_hora", 0) or 0,
            },
            "alcaldias": datos.get("alcaldias", []),
            "periodo": {
                "eventos_desde": datos.get("primera_fecha"),
                "eventos_hasta": datos.get("ultima_fecha"),
                "fuente": self._periodo(importacion, filtros),
            },
            "filtros_activos": filtros.activos(),
        }

    # --------------------------------------------------------------- territorio
    def territorio(self, filtros: AtlasFiltros, version: str, importacion: dict[str, Any], modo: str, celda: float | None, incluir_puntos: bool = True) -> dict[str, Any]:
        por_alcaldia = self.repo.por_alcaldia(version, filtros)
        resumen = self.resumen(filtros, importacion, version)
        hechos_totales = resumen["indicadores"]["hechos"]["valor"]
        for fila in por_alcaldia:
            fila["porcentaje_hechos"] = _pct(fila["hechos"], hechos_totales)
            fila["denominador"] = hechos_totales
            fila["lesionadas_por_hecho"] = _ratio(fila["lesionadas"], fila["hechos"])
            fila["fallecidas_por_100_hechos"] = _ratio(100 * fila["fallecidas"], fila["hechos"])

        if incluir_puntos:
            limite = 4000 if (celda or 0) > 0 else 2500
            puntos = self.repo.puntos(version, filtros, celda=celda, limite=limite)
        else:
            # El navegador pide los puntos al endpoint /puntos y los agrupa él
            # mismo según el zoom: aquí solo se informa cuántos hay.
            puntos = {
                "tipo": "omitidos",
                "total": resumen["cobertura"]["con_coordenadas"],
                "limite": 0,
                "puntos": [],
            }
        geometria = obtener_geometria()
        return {
            "modo": modo,
            "resumen": resumen,
            "alcaldias": por_alcaldia,
            "geometria": {
                "atribucion": ATRIBUCION,
                "extension": geometria.extension(),
                "alcaldias": [alcaldia["nombre"] for alcaldia in geometria.alcaldias],
            },
            "puntos": puntos,
            "metricas": {
                "alcaldias": "hechos, personas lesionadas y fallecidas por alcaldía; porcentaje sobre el total filtrado",
                "concentracion": "densidad de puntos por celda; la celda depende del zoom y cada caja informa su conteo",
            },
            "filtros_activos": filtros.activos(),
        }

    def geometria(self) -> dict[str, Any]:
        return obtener_geometria().geojson()

    def puntos(self, filtros: AtlasFiltros, version: str) -> dict[str, Any]:
        """Puntos del mapa listos para agrupar por zoom en el navegador."""
        datos = self.repo.puntos_completos(version, filtros, self.settings.atlas_max_puntos)
        geometria = obtener_geometria()
        return {
            **datos,
            "extension": geometria.extension(),
            "filtros_activos": filtros.activos(),
        }

    def colonias(
        self,
        filtros: AtlasFiltros,
        version: str,
        orden: str = "hechos",
        limite: int = 10,
    ) -> dict[str, Any]:
        """Agregados por colonia, ordenados por la métrica pedida.

        La ordenación y el recorte se hacen en el servidor: el navegador nunca
        recibe un subconjunto ordenado por otra métrica ni reordena un top-10
        ajeno. El desempate usa el identificador estable `alcaldía|colonia`.
        """
        metrica = orden if orden in ("hechos", "lesionadas", "fallecidas") else "hechos"
        tope = 20 if limite == 20 else 10
        datos = self.repo.colonias(version, filtros)
        colonias = []
        for fila in datos["colonias"]:
            identificador = "|".join(
                [(fila["alcaldia"] or "sin-alcaldia").lower(), fila["colonia"].lower()]
            ).replace(" ", "-")
            colonias.append({**fila, "id": identificador})
        colonias.sort(key=lambda fila: (-fila[metrica], fila["id"]))
        return {
            **datos,
            "colonias": colonias[:tope],
            "orden": metrica,
            "limite": tope,
            "devueltas": len(colonias[:tope]),
            "total_colonias": len(colonias),
            "filtros_activos": filtros.activos(),
        }


    def celdas(self, filtros: AtlasFiltros, version: str) -> dict[str, Any]:
        """Concentración por celda de la malla métrica estable (500 m UTM 14N)."""
        datos = self.repo.celdas(version, filtros)
        celdas = datos["celdas"]
        ubicables = datos["ubicables"]
        mayores = datos["mayores"]
        for celda in mayores:
            celda["porcentaje"] = round(100 * celda["hechos"] / ubicables, 2) if ubicables else 0.0
        return {
            **datos,
            "celdas": celdas,
            "mayores": mayores,
            "denominador": {
                "descripcion": "hechos ubicables del conjunto filtrado (coord_estado = ok)",
                "valor": ubicables,
            },
            "filtros_activos": filtros.activos(),
        }

    # ------------------------------------------------------------------ tiempo
    def tiempo(self, filtros: AtlasFiltros, version: str, agregacion: str) -> dict[str, Any]:
        serie = self.repo.por_periodo(version, filtros, agregacion)
        matriz = self.repo.matriz_dia_hora(version, filtros)
        return {
            "agregacion": agregacion,
            "serie": serie,
            "matriz": {
                "dias": DIAS_SEMANA,
                "horas": list(range(24)),
                "celdas": matriz,
                "total_hechos": sum(celda["hechos"] for celda in matriz),
            },
            "distribuciones": {
                "tipo_evento": self.repo.distribucion(version, filtros, "tipo_evento_norm"),
                "vialidad": self.repo.distribucion(version, filtros, "vialidad_norm"),
                "interseccion": self.repo.distribucion(version, filtros, "tipo_interseccion_norm"),
                "semaforizada": self._semaforizada(version, filtros),
            },
            # Cruces reales sobre el mismo registro de hecho (sin uniones).
            "cruces": {
                "tipos_entorno": _matriz_cruce(
                    self.repo.cruce(version, filtros, "tipo_evento_norm", "vialidad_norm")
                ),
                "interseccion_semaforo": _cruce_semaforo(
                    self.repo.cruce(version, filtros, "tipo_interseccion_norm", "semaforizada")
                ),
            },
            "filtros_activos": filtros.activos(),
        }

    def _semaforizada(self, version: str, filtros: AtlasFiltros) -> list[dict[str, Any]]:
        filas = self.repo.distribucion(version, filtros, "semaforizada")
        etiquetas = {True: "Semaforizada", False: "Sin semáforo", None: "Sin dato"}
        return [
            {**fila, "valor": etiquetas.get(fila["valor"], "Sin dato")}
            for fila in filas
        ]

    # ---------------------------------------------------- personas y vehículos
    def personas(self, filtros: AtlasFiltros, version: str) -> dict[str, Any]:
        resumen = self.repo.resumen(version, filtros)
        hechos = resumen.get("hechos", 0) or 0

        def con_total(filas: list[dict[str, Any]], etiqueta: str) -> list[dict[str, Any]]:
            """Une los valores desconocidos en una sola fila «Sin dato».

            Los archivos usan el texto vacío, ``None`` y la cadena «Sin dato» para
            el mismo caso; separarlos mostraría dos filas «Sin dato» distintas.
            """
            agrupado: dict[str, dict[str, Any]] = {}
            for fila in filas:
                valor = fila["valor"]
                desconocido = valor in (None, "") or str(valor).strip().lower() in {
                    "sin dato",
                    "sd",
                    "na",
                }
                clave = "__sin_dato__" if desconocido else str(valor)
                actual = agrupado.get(clave)
                if actual is None:
                    agrupado[clave] = {
                        "valor": None if desconocido else valor,
                        "hechos": fila["hechos"],
                        "lesionadas": fila["lesionadas"],
                        "fallecidas": fila["fallecidas"],
                    }
                else:
                    actual["hechos"] += fila["hechos"]
                    actual["lesionadas"] += fila["lesionadas"]
                    actual["fallecidas"] += fila["fallecidas"]
            ordenado = sorted(agrupado.values(), key=lambda item: item["hechos"], reverse=True)
            return [
                {
                    **fila,
                    "etiqueta": fila["valor"] if fila["valor"] not in (None, "") else "Sin dato",
                    "porcentaje": _pct(fila["hechos"], hechos),
                    "denominador": hechos,
                    "lesionadas_en_esos_hechos": fila["lesionadas"],
                    "fallecidas_en_esos_hechos": fila["fallecidas"],
                    "metrica": etiqueta,
                }
                for fila in ordenado
            ]

        grupos_edad = []
        edades = self.repo.edades(version, filtros)
        for etiqueta, desde, hasta in GRUPOS_EDAD:
            acumulado = [
                fila for fila in edades if fila["edad"] is not None and desde <= fila["edad"] <= hasta
            ]
            grupos_edad.append(
                {
                    "etiqueta": etiqueta,
                    "hechos": sum(fila["hechos"] for fila in acumulado),
                    "lesionadas": sum(fila["lesionadas"] for fila in acumulado),
                    "fallecidas": sum(fila["fallecidas"] for fila in acumulado),
                    "porcentaje": _pct(sum(fila["hechos"] for fila in acumulado), hechos),
                    "denominador": hechos,
                }
            )
        sin_edad = [fila for fila in edades if fila["edad"] is None]
        grupos_edad.append(
            {
                "etiqueta": "Sin dato de edad",
                "hechos": sum(fila["hechos"] for fila in sin_edad),
                "lesionadas": sum(fila["lesionadas"] for fila in sin_edad),
                "fallecidas": sum(fila["fallecidas"] for fila in sin_edad),
                "porcentaje": _pct(sum(fila["hechos"] for fila in sin_edad), hechos),
                "denominador": hechos,
            }
        )

        return {
            "total_hechos": hechos,
            "cobertura": {
                "con_sexo": resumen.get("con_sexo", 0) or 0,
                "con_edad": resumen.get("con_edad", 0) or 0,
                "con_tipo_usuario": resumen.get("con_tipo_usuario", 0) or 0,
                "con_vehiculo": resumen.get("con_vehiculo", 0) or 0,
                "porcentaje_con_sexo": _pct(resumen.get("con_sexo", 0) or 0, hechos),
                "porcentaje_con_edad": _pct(resumen.get("con_edad", 0) or 0, hechos),
                "porcentaje_con_tipo_usuario": _pct(resumen.get("con_tipo_usuario", 0) or 0, hechos),
                "porcentaje_con_vehiculo": _pct(resumen.get("con_vehiculo", 0) or 0, hechos),
            },
            "por_tipo_usuario": con_total(
                self.repo.distribucion(version, filtros, "personas_tipo_usuario"),
                "hechos con ese tipo de persona registrado; las personas son las del hecho completo",
            ),
            "por_sexo": con_total(
                self.repo.distribucion(version, filtros, "personas_sexo"),
                "hechos con ese sexo registrado",
            ),
            "por_edad": grupos_edad,
            "por_vehiculo": con_total(
                self.repo.distribucion(version, filtros, "vehiculo_tipo"),
                "hechos con ese tipo de vehículo registrado",
            ),
            "cruce_edad_sexo": self._cruce_edad_sexo(version, filtros),
            "filtros_activos": filtros.activos(),
        }

    # ------------------------------------------------------------------- tabla

    # ------------------------------------------- cruce edad × sexo (persona principal)
    def _cruce_edad_sexo(self, version: str, filtros: AtlasFiltros) -> dict[str, Any]:
        """Cruce real `personas_sexo` × `personas_edad` del **mismo** documento.

        Ambos campos son escalares del registro del hecho y describen a la misma
        persona principal del expediente, así que se agrupan en una sola pasada:
        no hay uniones que puedan duplicar hechos ni se multiplican marginales.
        """
        filas = self.repo.cruce(version, filtros, "personas_sexo", "personas_edad")
        desconocidos = {None, "", "sin dato", "sd", "na"}
        grupos: dict[str, dict[str, Any]] = {}
        sin_edad = {"hechos": 0, "lesionadas": 0, "fallecidas": 0}
        sin_sexo = {"hechos": 0, "lesionadas": 0, "fallecidas": 0}
        cobertura = {"hechos": 0, "con_edad_y_sexo": 0, "sin_edad": 0, "sin_sexo": 0}

        def rango_edad(edad: Any) -> str:
            try:
                valor = int(edad)
            except (TypeError, ValueError):
                return "Sin edad registrada"
            for etiqueta, desde, hasta in GRUPOS_EDAD:
                if desde <= valor <= hasta:
                    return etiqueta
            return "Sin edad registrada"

        for fila in filas:
            sexo = fila["fila"]
            sexo_desconocido = sexo is None or str(sexo).strip().lower() in desconocidos
            grupo_etiqueta = "Sin dato de sexo" if sexo_desconocido else str(sexo)
            edad_etiqueta = rango_edad(fila["columna"])
            edad_desconocida = edad_etiqueta == "Sin edad registrada"
            cobertura["hechos"] += fila["hechos"]
            if sexo_desconocido:
                for clave in ("hechos", "lesionadas", "fallecidas"):
                    sin_sexo[clave] += fila[clave]
            if edad_desconocida:
                for clave in ("hechos", "lesionadas", "fallecidas"):
                    sin_edad[clave] += fila[clave]
            if not sexo_desconocido and not edad_desconocida:
                cobertura["con_edad_y_sexo"] += fila["hechos"]
            grupo = grupos.setdefault(grupo_etiqueta, {"etiqueta": grupo_etiqueta, "celdas": {}})
            celda = grupo["celdas"].setdefault(
                edad_etiqueta, {"etiqueta": edad_etiqueta, "hechos": 0, "lesionadas": 0, "fallecidas": 0}
            )
            for clave in ("hechos", "lesionadas", "fallecidas"):
                celda[clave] += fila[clave]

        orden_sexo = {"Hombre": 0, "Mujer": 1}
        salida = []
        for etiqueta, grupo in grupos.items():
            celdas = sorted(
                grupo["celdas"].values(),
                key=lambda celda: (
                    next((i for i, (nombre, _, _) in enumerate(GRUPOS_EDAD) if nombre == celda["etiqueta"]), 99),
                    celda["etiqueta"],
                ),
            )
            salida.append({"sexo": etiqueta, "celdas": celdas, "hechos": sum(c["hechos"] for c in celdas)})
        salida.sort(key=lambda grupo: (orden_sexo.get(grupo["sexo"], 5), -grupo["hechos"], grupo["sexo"]))
        cobertura["sin_edad"] = sin_edad["hechos"]
        cobertura["sin_sexo"] = sin_sexo["hechos"]
        return {
            "grupos": salida,
            "sin_edad": sin_edad,
            "sin_sexo": sin_sexo,
            "cobertura": cobertura,
            "unidad": "persona principal del expediente (campos personas_sexo y personas_edad)",
        }
    def tabla(self, filtros: AtlasFiltros, version: str, orden: str, descendente: bool, pagina: int, tamano: int) -> dict[str, Any]:
        datos = self.repo.tabla(
            version, filtros, orden=orden, descendente=descendente, pagina=pagina, tamano=tamano
        )
        total = datos["total"]
        return {
            "filas": datos["filas"],
            "total": total,
            "pagina": pagina,
            "tamano": tamano,
            "paginas": max(1, (total + tamano - 1) // tamano) if total else 1,
            "filtros_activos": filtros.activos(),
        }

    def evento(self, version: str, folio: str) -> dict[str, Any] | None:
        return self.repo.evento(version, folio)

    # ---------------------------------------------------------------- comparar
    def comparar(self, filtros: AtlasFiltros, version: str, alcaldias: list[str]) -> dict[str, Any]:
        """Comparación de dos alcaldías con el mismo periodo, métricas y escalas."""
        if len(alcaldias) != 2:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="La comparación necesita exactamente dos alcaldías (?comparar=A&comparar=B).",
            )
        resultado = []
        for alcaldia in alcaldias:
            sub = AtlasFiltros(
                desde=filtros.desde,
                hasta=filtros.hasta,
                alcaldias=[alcaldia],
                tipos_evento=filtros.tipos_evento,
                consecuencias=filtros.consecuencias,
                tipos_usuario=filtros.tipos_usuario,
                tipos_vehiculo=filtros.tipos_vehiculo,
                vialidades=filtros.vialidades,
                semaforizada=filtros.semaforizada,
                sexos=filtros.sexos,
                edad_min=filtros.edad_min,
                edad_max=filtros.edad_max,
            )
            datos = self.repo.resumen(version, sub)
            hechos = datos.get("hechos", 0) or 0
            composicion = self.repo.distribucion(version, sub, "tipo_evento_norm")
            resultado.append(
                {
                    "alcaldia": alcaldia,
                    "hechos": hechos,
                    "lesionadas": datos.get("lesionadas", 0) or 0,
                    "fallecidas": datos.get("fallecidas", 0) or 0,
                    "con_fallecimientos": datos.get("con_fallecimientos", 0) or 0,
                    "lesionadas_por_hecho": _ratio(datos.get("lesionadas", 0) or 0, hechos),
                    "composicion_tipo_evento": [
                        {
                            "valor": fila["valor"],
                            "hechos": fila["hechos"],
                            "porcentaje": _pct(fila["hechos"], hechos),
                        }
                        for fila in composicion
                    ],
                }
            )

        maximo = max((fila["hechos"] for fila in resultado), default=0) or 1
        for fila in resultado:
            fila["escala_hechos"] = round(fila["hechos"] / maximo, 4)
        return {
            "alcaldias": resultado,
            "nota": (
                "Las dos alcaldías comparten periodo, métricas y escala. El volumen absoluto y la "
                "composición porcentual se muestran por separado: una alcaldía puede tener más "
                "hechos y, a la vez, una composición distinta."
            ),
            "filtros_activos": filtros.activos(),
        }

    # --------------------------------------------------------------- exportar
    def exportar(self, filtros: AtlasFiltros, version: str, importacion: dict[str, Any]) -> tuple[str, str]:
        """CSV del subconjunto filtrado, con fuente, periodo, filtros y versión."""
        fuente = self._fuente(version, importacion)
        activos = filtros.activos()
        periodo_eventos = (
            f"{(fuente['periodo'] or {}).get('desde', 'sin dato')} a "
            f"{(fuente['periodo'] or {}).get('hasta', 'sin dato')}"
        )
        cabecera_comentario = [
            "# Atlas vial · Ciudad de México — exportación del subconjunto filtrado",
            f"# Fuente: {fuente['titulo']} · {fuente['portal']} · {fuente['licencia']}",
            f"# Versión de datos: {version} · última sincronización: {fuente['ultima_sincronizacion']}",
            f"# Periodo de los eventos en el archivo: {periodo_eventos}",
            f"# Periodo consultado: {(filtros.desde.isoformat() if filtros.desde else 'inicio')} a "
            f"{(filtros.hasta.isoformat() if filtros.hasta else 'fin')}",
            "# Filtros aplicados: "
            + ("ninguno" if not activos else "; ".join(f"{a['etiqueta']}={', '.join(map(str, a['valores']))}" for a in activos)),
            "# Columnas: folio, fecha, hora, alcaldia, tipo_evento, ubicacion, personas_lesionadas, "
            "personas_fallecidas, consecuencia, latitud, longitud, estado_coordenada",
            "# Los campos de personas son los publicados por la fuente; no se derivan ni se imputan.",
        ]
        columnas = [
            "folio", "fecha", "hora", "alcaldia", "tipo_evento", "ubicacion",
            "personas_lesionadas", "personas_fallecidas", "consecuencia",
            "latitud", "longitud", "estado_coordenada",
        ]
        filas = self.repo.filas_exportar(version, filtros, self.settings.max_export_rows)
        partes = list(cabecera_comentario)
        partes.append(",".join(columnas))
        for fila in filas:
            partes.append(
                ",".join(
                    [
                        _csv(fila["folio"]),
                        _csv(fila["fecha"]),
                        _csv(fila["hora"]),
                        _csv(fila["alcaldia"]),
                        _csv(fila["tipo_evento"]),
                        _csv(fila["ubicacion"]),
                        _csv(fila["lesionadas"]),
                        _csv(fila["fallecidas"]),
                        _csv(fila["consecuencia"]),
                        _csv(fila["lat"]),
                        _csv(fila["lon"]),
                        _csv(fila["coord_estado"]),
                    ]
                )
            )
        nombre = f"atlas-vial-{version}.csv"
        return "\n".join(partes) + "\n", nombre


def _pct(valor: int | float, total: int | float) -> float | None:
    if not total:
        return None
    return round(100 * valor / total, 2)


def _ratio(valor: int | float, total: int | float) -> float | None:
    if not total:
        return None
    return round(valor / total, 2)


def _acumular(destino: dict[str, int], origen: dict[str, Any]) -> None:
    for clave in ("hechos", "lesionadas", "fallecidas"):
        destino[clave] = destino.get(clave, 0) + (origen.get(clave) or 0)


def _matriz_cruce(filas: list[dict[str, Any]]) -> dict[str, Any]:
    """Matriz tipo de evento × clasificación vial a partir del cruce real.

    El orden de filas y columnas se calcula **solo con hechos** para que no cambie
    al cambiar de métrica en la interfaz. Las combinaciones con algún valor no
    publicado se devuelven aparte: nunca se suman a una celda conocida.
    """
    totales_fila: dict[str, int] = {}
    totales_columna: dict[str, int] = {}
    for fila in filas:
        if fila["fila"]:
            totales_fila[fila["fila"]] = totales_fila.get(fila["fila"], 0) + fila["hechos"]
        if fila["columna"]:
            totales_columna[fila["columna"]] = totales_columna.get(fila["columna"], 0) + fila["hechos"]

    etiquetas_filas = sorted(totales_fila, key=lambda clave: (-totales_fila[clave], clave))
    etiquetas_columnas = sorted(totales_columna, key=lambda clave: (-totales_columna[clave], clave))
    indice_fila = {etiqueta: posicion for posicion, etiqueta in enumerate(etiquetas_filas)}
    indice_columna = {etiqueta: posicion for posicion, etiqueta in enumerate(etiquetas_columnas)}

    celdas: list[dict[str, Any]] = []
    sin_columna: dict[int, dict[str, int]] = {}
    sin_fila: dict[int, dict[str, int]] = {}
    sin_ambos: dict[str, int] = {"hechos": 0, "lesionadas": 0, "fallecidas": 0}
    cobertura = {"hechos": 0, "con_ambas": 0, "sin_columna": 0, "sin_fila": 0, "sin_ambos": 0}

    for fila in filas:
        etiqueta_fila = fila["fila"]
        etiqueta_columna = fila["columna"]
        cobertura["hechos"] += fila["hechos"]
        if etiqueta_fila and etiqueta_columna:
            cobertura["con_ambas"] += fila["hechos"]
            celdas.append(
                {
                    "fila": indice_fila[etiqueta_fila],
                    "columna": indice_columna[etiqueta_columna],
                    "hechos": fila["hechos"],
                    "lesionadas": fila["lesionadas"],
                    "fallecidas": fila["fallecidas"],
                }
            )
        elif etiqueta_fila:
            cobertura["sin_columna"] += fila["hechos"]
            _acumular(sin_columna.setdefault(indice_fila[etiqueta_fila], {}), fila)
        elif etiqueta_columna:
            cobertura["sin_fila"] += fila["hechos"]
            _acumular(sin_fila.setdefault(indice_columna[etiqueta_columna], {}), fila)
        else:
            cobertura["sin_ambos"] += fila["hechos"]
            _acumular(sin_ambos, fila)

    return {
        "filas": etiquetas_filas,
        "columnas": etiquetas_columnas,
        "celdas": sorted(celdas, key=lambda celda: (celda["fila"], celda["columna"])),
        "sin_columna": [
            {"fila": posicion, **valores} for posicion, valores in sorted(sin_columna.items())
        ],
        "sin_fila": [
            {"columna": posicion, **valores} for posicion, valores in sorted(sin_fila.items())
        ],
        "sin_ambos": sin_ambos,
        "cobertura": cobertura,
    }


def _cruce_semaforo(filas: list[dict[str, Any]]) -> dict[str, Any]:
    """Tipo de intersección × semáforo registrado, con las tres medidas.

    Cada celda del cruce trae hechos, personas lesionadas y personas fallecidas
    del **mismo** conjunto filtrado (una sola agregación, sin uniones). Las tres
    medidas se acumulan por separado: nunca se derivan sumas de personas a partir
    del conteo de hechos.
    """
    MEDIDAS = ("hechos", "lesionadas", "fallecidas")
    totales: dict[str, dict[str, int]] = {}
    for fila in filas:
        if not fila["fila"]:
            continue
        destino = totales.setdefault(fila["fila"], {medida: 0 for medida in MEDIDAS})
        for medida in MEDIDAS:
            destino[medida] += fila[medida] or 0

    etiquetas = sorted(totales, key=lambda clave: (-totales[clave]["hechos"], clave))
    acumulado: dict[str, dict[str, dict[str, int]]] = {
        etiqueta: {
            "con": {medida: 0 for medida in MEDIDAS},
            "sin": {medida: 0 for medida in MEDIDAS},
            "sin_dato": {medida: 0 for medida in MEDIDAS},
        }
        for etiqueta in etiquetas
    }
    cobertura: dict[str, dict[str, int]] = {
        medida: {"total": 0, "conocido": 0, "sin_dato": 0} for medida in MEDIDAS
    }

    for fila in filas:
        etiqueta = fila["fila"]
        condicion = (
            "con" if fila["columna"] is True else "sin" if fila["columna"] is False else "sin_dato"
        )
        for medida in MEDIDAS:
            valor = fila[medida] or 0
            cobertura[medida]["total"] += valor
            if etiqueta:
                acumulado[etiqueta][condicion][medida] += valor
            if condicion == "sin_dato":
                cobertura[medida]["sin_dato"] += valor
            else:
                cobertura[medida]["conocido"] += valor

    return {
        "filas": etiquetas,
        "series": [
            {"fila": posicion, "etiqueta": etiqueta, "medidas": acumulado[etiqueta]}
            for posicion, etiqueta in enumerate(etiquetas)
        ],
        "cobertura": cobertura,
    }

def _iso(valor: Any) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, date):
        return valor.isoformat()
    if hasattr(valor, "isoformat"):
        return valor.isoformat()
    return str(valor)


def _csv(valor: Any) -> str:
    if valor is None:
        return ""
    texto = str(valor)
    if any(caracter in texto for caracter in [",", '"', "\n"]):
        return '"' + texto.replace('"', '""') + '"'
    return texto


def rango_fechas(desde: date, hasta: date) -> list[str]:
    """Lista de días ISO del rango (para marcar huecos sin inventar valores)."""
    dias = []
    actual = desde
    while actual <= hasta:
        dias.append(actual.isoformat())
        actual += timedelta(days=1)
    return dias


