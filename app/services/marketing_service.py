"""Servicio del tablero «Marketing y clientes».

Traduce el repositorio a las respuestas de la API y aplica las definiciones: qué es
un usuario, una sesión, una compra, una conversión y cómo se leen las cohortes.
Cuando **no hay una versión publicada** (porque no hubo acceso a BigQuery), cada
respuesta lo dice explícitamente y no incluye ninguna cifra: es preferible un
estado vacío honesto a un cero o a un dato inventado.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from time import monotonic
from typing import Any

from fastapi import HTTPException, status

from app.marketing import esquema
from app.marketing.ingesta import cobertura_muestra, estado_publico
from app.config import Settings
from app.models.marketing import (
    AVISOS,
    DEFINICIONES,
    DIMENSIONES_COHORTE,
    EMBUDO_UNIDAD,
    EMBUDO_VENTANA_HORAS,
    MarketingFiltros,
)
from app.repositories.marketing_repository import MarketingRepository


class MarketingService:
    def __init__(self, repo: MarketingRepository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings

    # ------------------------------------------------------------------ caché
    #: Caché en proceso por versión, endpoint y filtros normalizados. Se invalida
    #: sola al cambiar la versión publicada (la clave incluye la versión).
    _CACHE: dict[str, tuple[float, Any]] = {}

    def _cache_leer(self, clave: str) -> Any | None:
        from time import monotonic

        entrada = MarketingService._CACHE.get(clave)
        if not entrada:
            return None
        creado, valor = entrada
        if monotonic() - creado > self.settings.marketing_cache_ttl_s:
            MarketingService._CACHE.pop(clave, None)
            return None
        return valor

    def _cache_guardar(self, clave: str, valor: Any) -> Any:
        from time import monotonic

        if self.settings.marketing_cache_ttl_s > 0:
            MarketingService._CACHE[clave] = (monotonic(), valor)
            if len(MarketingService._CACHE) > 128:
                MarketingService._CACHE.clear()
        return valor

    def _clave_cache(self, endpoint: str, filtros: MarketingFiltros, extra: str = "") -> str:
        version = self.repo.version_publicada() or "sin-version"
        activos = filtros.activos() or []
        return f"{version}|{endpoint}|{extra}|{activos}"

    def desde_cache(self, endpoint: str, filtros: MarketingFiltros, calcular, extra: str = "") -> tuple[Any, bool]:
        """Devuelve la respuesta y si vino de caché (para medir en las cabeceras)."""
        desde = monotonic()
        clave = self._clave_cache(endpoint, filtros, extra)
        valor = self._cache_leer(clave)
        if valor is not None:
            return valor, True
        resultado = calcular()
        self._cache_guardar(clave, resultado)
        self.ultimo_calculo_ms = round((monotonic() - desde) * 1000, 1)
        return resultado, False

    # ------------------------------------------------------------------ base
    def version_actual(self) -> tuple[str, dict[str, Any]]:
        version = self.repo.version_publicada()
        if not version:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Todavía no hay una versión publicada del conjunto GA4. "
                    "Ejecuta `python -m scripts.importar_marketing` con acceso a "
                    "BigQuery; el tablero no muestra cifras hasta entonces."
                ),
            )
        importacion = self.repo.importacion(version) or {}
        return version, importacion

    def _sin_datos(self, base: dict[str, Any]) -> dict[str, Any]:
        """Respuesta uniforme cuando no hay datos: sin cifras, con motivo y vía."""
        estado = estado_publico(self.repo._db)
        base.update(
            {
                "datos_disponibles": False,
                "version": None,
                "motivo": estado.get("motivo") or "No hay una versión publicada.",
                "fuente": estado.get("fuente"),
                "acceso_bigquery": estado.get("acceso_bigquery"),
                "avisos": AVISOS,
            }
        )
        return base

    def _disponible(self) -> str | None:
        return self.repo.version_publicada()

    # ---------------------------------------------------------------- catálogo
    def catalogo(self, filtros: MarketingFiltros) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos(
                {
                    "definiciones": DEFINICIONES,
                    "filtros": {
                        "canales": [],
                        "dispositivos": [],
                        "paises": [],
                        "periodo": esquema.PERIODO_DOCUMENTADO,
                    },
                    "capacidades": {},
                    "dias_observados": 0,
                }
            )

        _, importacion = self.version_actual()
        valores = self.repo.valores_filtro(version)
        periodo = valores.get("periodo") or {}
        return {
            "datos_disponibles": True,
            "version": version,
            "fuente": {
                "conjunto": esquema.TABLAS,
                "documentacion": esquema.DOCUMENTACION,
                "esquema": esquema.ESQUEMA_OFICIAL,
                "licencia": esquema.LICENCIA,
                "condicion": esquema.CONDICION,
                "periodo_documentado": esquema.PERIODO_DOCUMENTADO,
                "origen": importacion.get("origen"),
                "publicado_en": importacion.get("publicado_en"),
            },
            "periodo": {
                "desde": periodo.get("desde"),
                "hasta": periodo.get("hasta"),
                "consulta_desde": filtros.desde.isoformat() if filtros.desde else periodo.get("desde"),
                "consulta_hasta": filtros.hasta.isoformat() if filtros.hasta else periodo.get("hasta"),
            },
            "filtros": {
                "canales": valores.get("canales", []),
                "dispositivos": valores.get("dispositivos", []),
                "paises": valores.get("paises", []),
                "periodo": periodo,
                "dimensiones_cohorte": [
                    {"valor": clave, "etiqueta": etiqueta}
                    for clave, etiqueta in DIMENSIONES_COHORTE.items()
                ],
            },
            "capacidades": importacion.get("capacidades") or {},
            "dias_observados": valores.get("dias_observados", 0),
            "definiciones": DEFINICIONES,
            "avisos": AVISOS,
            "validacion": importacion.get("validacion") or {},
            "esquema": importacion.get("esquema") or {},
        }

    # ------------------------------------------------------------------ resumen
    def resumen(self, filtros: MarketingFiltros) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos({"indicadores": None, "serie": [], "comparacion": None})

        _, importacion = self.version_actual()
        facetas = self.repo.facetas_usuarios(version, filtros)
        metricas = self.repo.metricas(version, filtros)
        usuarios = facetas["usuarios"]
        compradores = facetas["compradores"]
        serie = self.repo.serie(version, filtros, facetas=facetas)
        compras = metricas.get("compras", 0)
        ingresos = metricas.get("ingresos_usd", 0.0)

        indicadores = {
            "eventos": {
                "valor": metricas.get("eventos", 0),
                "base": "eventos registrados en el rango filtrado",
            },
            "usuarios": {
                "valor": usuarios,
                "base": "usuarios distintos (user_pseudo_id), contados una sola vez",
            },
            "sesiones": {
                "valor": metricas.get("sesiones", 0),
                "base": "sesiones distintas (usuario + ga_session_id)",
            },
            "compradores": {
                "valor": compradores,
                "base": "usuarios distintos con al menos una compra",
            },
            "compras": {
                "valor": compras,
                "base": "compras deduplicadas por usuario + transacción",
            },
            "ingresos_usd": {
                "valor": round(float(ingresos), 2),
                "base": "suma de purchase_revenue_in_usd sobre las compras deduplicadas",
                "moneda": "USD",
            },
            "conversion": {
                "valor": _pct(compradores, usuarios),
                "numerador": compradores,
                "denominador": usuarios,
                "unidad": "usuario",
                "base": "compradores ÷ usuarios del mismo rango y filtros",
            },
            "ticket_medio_usd": {
                "valor": round(ingresos / compras, 2) if compras else None,
                "base": "ingresos ÷ compras (solo compras con importe)",
                "moneda": "USD",
            },
        }

        # La comparación con el periodo anterior ya no forma parte de la vista: el
        # resumen muestra el mapa de desempeño de canales. El método se conserva
        # para consultas puntuales, pero no se calcula en cada petición.
        comparacion = None

        total_tabla = self.repo.tabla(version, filtros, orden="fecha", descendente=True, pagina=1, tamano=1)
        canales = self.repo.adquisicion(version, filtros, facetas=facetas)
        conversion_fuente = [
            {
                "canal": fila["canal"],
                "usuarios": fila.get("usuarios", 0),
                "compradores": fila.get("compradores", 0),
                "compras": fila.get("compras", 0),
                "ingresos_usd": fila.get("ingresos_usd", 0.0),
                "conversion": _pct(fila.get("compradores", 0), fila.get("usuarios", 0)),
                "base_reducida": fila.get("usuarios", 0) < UMBRAL_BASE_REDUCIDA,
            }
            for fila in canales
        ]
        sankey = _sankey(self.repo.sankey_sesiones(version, filtros))
        lollipop = {
            "filas": conversion_fuente,
            "referencia_global": {
                "valor": _pct(compradores, usuarios),
                "compradores": compradores,
                "usuarios": usuarios,
                "denominador": "usuarios únicos del rango y filtros",
            },
            "umbral_base_reducida": UMBRAL_BASE_REDUCIDA,
            "definicion": (
                "Compradores únicos ÷ usuarios únicos por canal de adquisición; la "
                "referencia global se calcula con sus propios numerador y denominador, "
                "no promediando tasas por fuente."
            ),
        }
        return {
            "datos_disponibles": True,
            "version": version,
            "indicadores": indicadores,
            "serie": [
                {
                    **fila,
                    "conversion": _pct(fila.get("compradores", 0), fila.get("usuarios", 0)),
                }
                for fila in serie
            ],
            "sankey": sankey,
            "conversion_fuente": lollipop,
            "conversion_global": {
                "valor": _pct(compradores, usuarios),
                "numerador": compradores,
                "denominador": usuarios,
                "unidad": "usuario",
            },
            "comparacion": comparacion,
            "cobertura": {
                "eventos_sin_sesion": metricas.get("eventos_sin_sesion", 0),
                "compras_sin_importe": metricas.get("compras_sin_importe", 0),
                "dias_con_datos": len(serie),
                "eventos_en_tabla": total_tabla.get("total", 0),
                "eventos_registrados": metricas.get("eventos", 0),
                # Conciliación visible: cada indicador frente al total que suman
                # las gráficas del propio rango. Si alguno no cuadra, el tablero lo
                # dice en lugar de mostrar dos cifras distintas sin avisar.
                "consistencia": _consistencia(indicadores, canales, sankey, serie),
                "muestra": {
                    "usuarios": usuarios,
                    "unidad": "usuario",
                    "describen": "la muestra publicada",
                },
            },
            "periodo": {
                "desde": serie[0]["fecha"] if serie else None,
                "hasta": serie[-1]["fecha"] if serie else None,
                "documentado": esquema.PERIODO_DOCUMENTADO,
            },
            "filtros_activos": filtros.activos(),
            "avisos": AVISOS,
        }

    def _comparacion(
        self,
        version: str,
        importacion: dict[str, Any],
        filtros: MarketingFiltros,
        actuales: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Compara con el periodo anterior **solo si la ventana existe completa**."""
        ventana = filtros.periodo_anterior()
        if not ventana:
            return None
        inicio, fin = ventana
        periodo = (importacion or {}).get("periodo") or {}
        desde_datos = periodo.get("desde")
        hasta_datos = periodo.get("hasta")
        if not desde_datos or not hasta_datos:
            return None
        if inicio.isoformat() < desde_datos or fin.isoformat() > hasta_datos:
            return {
                "valida": False,
                "motivo": (
                    "El periodo anterior de igual longitud queda fuera de los datos "
                    f"observados ({desde_datos} a {hasta_datos}); no se compara."
                ),
            }

        anteriores = MarketingFiltros(
            desde=inicio,
            hasta=fin,
            canales=filtros.canales,
            dispositivos=filtros.dispositivos,
            paises=filtros.paises,
        )
        metricas = self.repo.metricas(version, anteriores)
        usuarios = self.repo.usuarios_distintos(version, anteriores)
        compradores = self.repo.usuarios_distintos(version, anteriores, solo_compradores=True)
        return {
            "valida": True,
            "desde": inicio.isoformat(),
            "hasta": fin.isoformat(),
            "indicadores": {
                "eventos": metricas.get("eventos", 0),
                "usuarios": usuarios,
                "sesiones": metricas.get("sesiones", 0),
                "compradores": compradores,
                "compras": metricas.get("compras", 0),
                "ingresos_usd": round(float(metricas.get("ingresos_usd") or 0), 2),
                "conversion": _pct(compradores, usuarios),
            },
            "variacion": {
                "eventos": _variacion(actuales["eventos"]["valor"], metricas.get("eventos", 0)),
                "usuarios": _variacion(actuales["usuarios"]["valor"], usuarios),
                "compras": _variacion(actuales["compras"]["valor"], metricas.get("compras", 0)),
                "ingresos_usd": _variacion(
                    actuales["ingresos_usd"]["valor"], float(metricas.get("ingresos_usd") or 0)
                ),
            },
        }

    # -------------------------------------------------------------- adquisición
    def adquisicion(self, filtros: MarketingFiltros) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos({"canales": [], "totales": None})

        filas = self.repo.adquisicion(version, filtros)
        total_usuarios = sum(fila.get("usuarios", 0) for fila in filas)
        canales = [
            {
                **fila,
                "conversion": _pct(fila.get("compradores", 0), fila.get("usuarios", 0)),
                "porcentaje_usuarios": _pct(fila.get("usuarios", 0), total_usuarios),
                "ingreso_por_comprador_usd": (
                    round(fila.get("ingresos_usd", 0) / fila["compradores"], 2)
                    if fila.get("compradores")
                    else None
                ),
            }
            for fila in filas
        ]
        categorias = (
            self.repo.productos(version, filtros, limite=12)["categorias"]
            if self._tiene_categorias(version)
            else []
        )

        return {
            "datos_disponibles": True,
            "version": version,
            "semantica": {
                "tipo": "adquisicion_de_usuario",
                "etiqueta": "Adquisición de usuario",
                "explicacion": DEFINICIONES["canal"],
                "nota": (
                    "El conjunto no documenta atribución por sesión, así que estas "
                    "cifras describen de dónde llegó cada persona la primera vez que "
                    "la vimos, no el canal de cada visita."
                ),
            },
            "canales": canales,
            "totales": {
                "usuarios": total_usuarios,
                "usuarios_unicos": self.repo.usuarios_distintos(version, filtros),
                "sesiones": sum(fila.get("sesiones", 0) for fila in filas),
                "compras": sum(fila.get("compras", 0) for fila in filas),
                "ingresos_usd": round(sum(fila.get("ingresos_usd", 0) for fila in filas), 2),
                "compradores": sum(fila.get("compradores", 0) for fila in filas),
            },
            "detalle": self.repo.detalle_canal(version, filtros) if len(filtros.canales) == 1 else None,
            "categorias": categorias,
            "categorias_disponibles": self._tiene_categorias(version),
            "filtros_activos": filtros.activos(),
            "avisos": AVISOS,
        }

    def _tiene_categorias(self, version: str) -> bool:
        importacion = self.repo.importacion(version) or {}
        capacidades = importacion.get("capacidades") or {}
        return bool(capacidades.get("categoria_producto"))

    # ---------------------------------------------------------------- productos
    def productos(self, filtros: MarketingFiltros, limite: int = 10) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos({"articulos": [], "categorias": []})

        datos = self.repo.productos(version, filtros, limite)
        afinidad = self.repo.afinidad_categorias(version, filtros)
        compras = self.repo.metricas(version, filtros).get("compras", 0)
        suma_categorias = sum(fila.get("compras", 0) for fila in datos["categorias"])
        return {
            "datos_disponibles": True,
            "version": version,
            "articulos": datos["articulos"],
            "categorias": datos["categorias"],
            "afinidad": afinidad,
            "conciliacion": {
                "compras_totales": compras,
                "suma_compras_por_categoria": suma_categorias,
                "solapamiento": suma_categorias - compras,
                "explicacion": (
                    "Una compra con artículos de dos categorías cuenta en las dos, así "
                    "que la suma por categoría puede superar las compras totales. No se "
                    "reparte el ingreso de la compra: los importes son de la línea."
                ),
            },
            "definiciones": {
                "compras": "compras distintas que incluyen al menos un artículo de la categoría",
                "articulos": "líneas de artículo",
                "unidades": "suma de item.quantity cuando la fuente lo trae",
                "ingresos_linea_usd": "suma de item_revenue_in_usd de la línea",
            },
            "filtros_activos": filtros.activos(),
            "avisos": AVISOS,
        }

    # -------------------------------------------------------------- conversión
    def conversion(self, filtros: MarketingFiltros) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos({"pasos": [], "por_canal": []})

        pasos = self.repo.embudo(version, filtros)
        inicial = pasos[0]["sesiones"] if pasos else 0
        anteriores: int | None = None
        detalle = []
        for paso in pasos:
            sesiones = paso.get("sesiones", 0)
            detalle.append(
                {
                    **paso,
                    "porcentaje_del_inicio": _pct(sesiones, inicial),
                    "porcentaje_del_paso_anterior": _pct(sesiones, anteriores) if anteriores else None,
                    "diferencia_con_el_paso_anterior": (
                        sesiones - anteriores if anteriores is not None else None
                    ),
                    # Lectura independiente del orden: sesiones que registraron el
                    # evento del paso y eventos totales de ese tipo.
                    "porcentaje_sesiones_con_evento": _pct(
                        paso.get("sesiones_evento", 0), inicial
                    ),
                    "eventos_por_sesion_con_evento": (
                        round(paso.get("eventos", 0) / paso["sesiones_evento"], 2)
                        if paso.get("sesiones_evento")
                        else None
                    ),
                }
            )
            anteriores = sesiones

        por_canal = self.repo.embudo_por_canal(version, filtros)
        canales: dict[str, dict[str, Any]] = {}
        for fila in por_canal:
            canal = fila["canal"]
            entrada = canales.setdefault(canal, {"canal": canal, "pasos": {}})
            entrada["pasos"][fila["paso"]] = fila["sesiones"]
        tabla_canales = []
        for canal, datos in canales.items():
            inicio = datos["pasos"].get(1, 0)
            final = datos["pasos"].get(max(datos["pasos"]) if datos["pasos"] else 1, 0)
            tabla_canales.append(
                {
                    "canal": canal,
                    "pasos": [datos["pasos"].get(paso["paso"], 0) for paso in pasos],
                    "sesiones_inicio": inicio,
                    "sesiones_compra": final,
                    "conversion_sesion": _pct(final, inicio),
                }
            )
        tabla_canales.sort(key=lambda fila: fila["sesiones_inicio"], reverse=True)

        return {
            "datos_disponibles": True,
            "version": version,
            "unidad": EMBUDO_UNIDAD,
            "ventana_horas": EMBUDO_VENTANA_HORAS,
            "criterio": (
                "Se exige que cada paso ocurra después del anterior dentro de la misma "
                f"sesión y que el último ocurra en las {EMBUDO_VENTANA_HORAS} horas "
                "siguientes al primero. Una sesión que no cumple el orden o la ventana "
                "no avanza de paso."
            ),
            "advertencia": (
                "Las diferencias entre pasos son diferencias entre sesiones que "
                "cumplieron el orden exigido; no son «abandono» ni una tasa de fuga "
                "medida en el sitio. Cada paso muestra además cuántas sesiones "
                "registraron el evento y cuántos eventos hay: si un paso casi no "
                "aparece en la muestra, el embudo se estrecha por eso y no porque el "
                "paso anterior se haya cumplido y luego se pierda."
            ),
            "pasos": detalle,
            "por_canal": tabla_canales,
            "por_dispositivo": [
                {
                    "valor": fila["valor"],
                    "pasos": [fila["pasos"].get(paso["paso"], 0) for paso in pasos],
                }
                for fila in self.repo.embudo_por_dimension(version, filtros, "dispositivo")
            ],
            "transiciones": _transiciones(self.repo.transiciones(version, filtros)),
            "perfil": _perfil(self.repo.perfil_conversion(version, filtros)),
            "friccion": _friccion(
                self.repo.perfil_conversion(version, filtros), filtros
            ),
            "compras_totales": (
                (self.version_actual()[1] or {}).get("cifras", {}).get("compras", 0)
            ),
            "nota_compras": (
                "Las sesiones que completan el embudo no tienen que coincidir con las "
                "compras totales: el embudo exige los cuatro pasos en orden dentro de "
                "una misma sesión y una ventana de 24 horas, mientras que una compra "
                "cuenta aunque llegue sin carrito previo o en otra sesión."
            ),
            "filtros_activos": filtros.activos(),
            "avisos": AVISOS,
        }

    # ----------------------------------------------------------------- cohortes
    def cohortes(self, filtros: MarketingFiltros, dimension: str) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos({"cohortes": [], "semanas": [], "celdas": []})

        if dimension not in DIMENSIONES_COHORTE:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Dimensión de cohorte no válida. Usa: "
                    + ", ".join(DIMENSIONES_COHORTE)
                ),
            )

        filas = self.repo.cohortes(version, filtros, dimension)
        _, importacion = self.version_actual()
        periodo = (importacion or {}).get("periodo") or {}
        ultimo_dia = _fecha(periodo.get("hasta"))

        # Las filas vienen por valor de la dimensión (un canal, un dispositivo…):
        # la matriz muestra la cohorte completa, así que se agregan. La definición
        # del valor de la cohorte es única por usuario, de modo que sumar no cuenta
        # dos veces a nadie.
        agregado: dict[tuple[str, int], dict[str, int]] = {}
        for fila in filas:
            clave = (fila["cohorte"], int(fila.get("indice") or 0))
            acumulado = agregado.setdefault(clave, {"usuarios": 0, "compradores": 0})
            acumulado["usuarios"] += int(fila.get("usuarios") or 0)
            acumulado["compradores"] += int(fila.get("compradores") or 0)

        tamano = {
            cohorte: datos["usuarios"]
            for (cohorte, indice), datos in agregado.items()
            if indice == 0
        }

        cohortes = sorted(tamano)
        celdas: list[dict[str, Any]] = []
        maximo = 0
        for (cohorte, indice), datos in sorted(agregado.items()):
            maximo = max(maximo, indice)
            usuarios = datos["usuarios"]
            compradores = datos["compradores"]
            base = tamano.get(cohorte) or 0
            estado_ventana = _ventana(cohorte, indice, ultimo_dia)
            celdas.append(
                {
                    "cohorte": cohorte,
                    "indice": indice,
                    "usuarios": usuarios,
                    "compradores": compradores,
                    "retencion": _pct(usuarios, base) if estado_ventana == "completa" else None,
                    "recompra": _pct(compradores, base) if estado_ventana == "completa" else None,
                    "ventana": estado_ventana,
                }
            )

        curvas, puntos_cohorte = _curvas_y_puntos(filas, ultimo_dia)
        return {
            "datos_disponibles": True,
            "version": version,
            "dimension": dimension,
            "etiqueta_dimension": DIMENSIONES_COHORTE[dimension],
            "unidad": "usuario",
            "granularidad": "semana",
            "definicion": DEFINICIONES["cohorte"],
            "curvas": curvas,
            "puntos": puntos_cohorte,
            "umbral_base_reducida": UMBRAL_BASE_REDUCIDA,
            "criterio_seleccion": (
                "Por omisión se muestran los cinco valores con más usuarios elegibles "
                "para S1 (la base que se usa como denominador en esa semana). El resto "
                "se puede consultar en el buscador, con su base."
            ),
            "cohortes": [
                {
                    "cohorte": cohorte,
                    "usuarios": tamano.get(cohorte, 0),
                    "semana_iso": _semana_iso(cohorte),
                }
                for cohorte in cohortes
            ],
            "semanas": list(range(0, maximo + 1)),
            "celdas": celdas,
            "valores_disponibles": self.repo.valores_cohorte(version, dimension),
            "ultimo_dia_observado": periodo.get("hasta"),
            "filtros_activos": filtros.activos(),
            "avisos": AVISOS,
        }

    # -------------------------------------------------------------------- tabla
    def tabla(
        self,
        filtros: MarketingFiltros,
        *,
        orden: str,
        descendente: bool,
        pagina: int,
        tamano: int,
    ) -> dict[str, Any]:
        """El modelo compacto no almacena eventos: no hay tabla de detalle.

        Se responde con ``disponible: false`` y el motivo, en lugar de cifras de una
        muestra que ya no existe en Atlas.
        """
        version = self._disponible()
        return self._sin_datos(
            {
                "filas": [],
                "total": 0,
                "paginas": 0,
                "pagina": pagina,
                "tamano": tamano,
                "orden": orden,
                "sentido": "desc" if descendente else "asc",
                "disponible": False,
            }
        ) | {
            "disponible": False,
            "motivo": (
                "El modelo compacto guarda solo resultados agrupados: el detalle de "
                "eventos ya no se almacena en Atlas."
            ),
            "version": version,
            "muestra": {"eventos_en_tabla": 0, "eventos_registrados": 0, "cobertura": 0},
        }

    def exportar(self, filtros: MarketingFiltros) -> tuple[str, str]:
        """CSV con los **agregados del tablero**: serie diaria, canales y totales.

        El modelo compacto no guarda eventos uno a uno, así que exportar «todas las
        filas del extracto» ya no sería cierto. Se exporta lo que la vista muestra
        —y con las mismas cifras—: una fila por día y una por canal de adquisición,
        más el total del rango. La cabecera declara fuente, muestra, método, filtros
        y definiciones, y dice explícitamente que no hay detalle de eventos.
        """
        version = self._disponible()
        if not version:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No hay datos publicados que exportar.",
            )

        _, importacion = self.version_actual()
        datos = self.resumen(filtros)
        if not datos.get("datos_disponibles"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No hay datos publicados que exportar.",
            )

        observed = (importacion or {}).get("periodo") or {}
        rango = datos.get("periodo") or {}
        activos = filtros.activos()
        cobertura = cobertura_muestra((importacion or {}).get("muestra"))
        muestra = cobertura["muestra_publicada"]
        poblacion = cobertura["poblacion_original"]
        cabecera = [
            "# Marketing y clientes — exportación de los agregados del tablero",
            f"# Fuente: {esquema.TABLAS} · {esquema.DOCUMENTACION}",
            f"# Condición: {esquema.CONDICION}",
            f"# Versión de datos: {version} · publicada: {(importacion or {}).get('publicado_en')}",
            f"# Periodo observado en el conjunto: {observed.get('desde')} a {observed.get('hasta')}",
            f"# Periodo exportado (tras filtros): {rango.get('desde')} a {rango.get('hasta')}",
            f"# Muestra publicada: {muestra.get('usuarios')} usuarios de "
            f"{poblacion.get('usuarios')} del periodo "
            f"({muestra.get('porcentaje_efectivo')} %); unidad de muestreo: "
            f"{muestra.get('unidad')}; semilla «{muestra.get('semilla')}».",
            f"# Método de selección: {cobertura['metodo'].get('predicado')}",
            "# Las cifras describen la muestra publicada: no se extrapolan a la población.",
            "# Filtros aplicados: "
            + ("ninguno" if not activos else "; ".join(
                f"{a['etiqueta']}={', '.join(map(str, a['valores']))}" for a in activos
            )),
            "# Contenido: agregados. bloque=serie → una fila por día; bloque=canal → una por "
            "canal de adquisición del usuario; bloque=total → el rango completo.",
            "# No incluye eventos uno a uno: el modelo compacto no los guarda. La afinidad de "
            "categorías no depende de los filtros y se consulta en /productos.",
            "# Definiciones: usuario = user_pseudo_id distinto; sesión = usuario + ga_session_id; "
            "comprador = usuario distinto con ≥1 compra; compra = purchase deduplicado por "
            "usuario + transacción; ingresos = purchase_revenue_in_usd (USD) de esas compras; "
            "conversion = compradores ÷ usuarios (porcentaje).",
            "# Sin identificadores de personas.",
        ]
        columnas = [
            "bloque",
            "clave",
            "eventos",
            "sesiones",
            "usuarios",
            "compradores",
            "compras",
            "ingresos_usd",
            "conversion",
        ]
        partes = list(cabecera)
        partes.append(",".join(columnas))

        def fila(bloque: str, clave: Any, eventos=0, sesiones=0, usuarios=0, compradores=0,
                 compras=0, ingresos=0.0, conversion=None) -> str:
            return ",".join(
                _csv(valor)
                for valor in (
                    bloque,
                    clave,
                    eventos,
                    sesiones,
                    usuarios,
                    compradores,
                    compras,
                    round(float(ingresos or 0), 2),
                    conversion,
                )
            )

        for dia in datos["serie"]:
            partes.append(
                fila(
                    "serie",
                    dia.get("fecha"),
                    dia.get("eventos"),
                    dia.get("sesiones"),
                    dia.get("usuarios"),
                    dia.get("compradores"),
                    dia.get("compras"),
                    dia.get("ingresos_usd"),
                    dia.get("conversion"),
                )
            )
        for canal in (datos["conversion_fuente"] or {}).get("filas") or []:
            partes.append(
                fila(
                    "canal",
                    canal.get("canal"),
                    None,
                    None,
                    canal.get("usuarios"),
                    canal.get("compradores"),
                    canal.get("compras"),
                    canal.get("ingresos_usd"),
                    canal.get("conversion"),
                )
            )
        indicadores = datos["indicadores"]
        partes.append(
            fila(
                "total",
                "rango filtrado",
                indicadores["eventos"]["valor"],
                indicadores["sesiones"]["valor"],
                indicadores["usuarios"]["valor"],
                indicadores["compradores"]["valor"],
                indicadores["compras"]["valor"],
                indicadores["ingresos_usd"]["valor"],
                indicadores["conversion"]["valor"],
            )
        )
        return "\n".join(partes) + "\n", f"marketing-clientes-{version}.csv"


# --------------------------------------------------------------------- ayudas
def _pct(valor: Any, total: Any) -> float | None:
    try:
        valor = float(valor or 0)
        total = float(total or 0)
    except (TypeError, ValueError):
        return None
    if not total:
        return None
    return round(100 * valor / total, 2)


def _variacion(actual: Any, anterior: Any) -> float | None:
    try:
        actual = float(actual or 0)
        anterior = float(anterior or 0)
    except (TypeError, ValueError):
        return None
    if not anterior:
        return None
    return round(100 * (actual - anterior) / anterior, 2)


def _fecha(valor: Any) -> date | None:
    if not valor:
        return None
    if isinstance(valor, date):
        return valor
    if isinstance(valor, datetime):
        return valor.date()
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


def _ventana(cohorte: str, indice: int, ultimo_dia: date | None) -> str:
    """«completa», «incompleta» o «sin_ventana» para una celda de cohorte."""
    inicio = _fecha(cohorte)
    if inicio is None or ultimo_dia is None:
        return "sin_ventana"
    fin_semana = inicio + timedelta(days=7 * indice + 6)
    if fin_semana <= ultimo_dia:
        return "completa"
    if inicio + timedelta(days=7 * indice) <= ultimo_dia:
        return "incompleta"
    return "sin_ventana"


def _semana_iso(valor: str) -> str:
    fecha = _fecha(valor)
    if fecha is None:
        return str(valor)
    anio, semana, _ = fecha.isocalendar()
    return f"{anio}-S{semana:02d}"


def _csv(valor: Any) -> str:
    if valor is None:
        return ""
    texto = str(valor)
    if any(caracter in texto for caracter in [",", '"', "\n"]):
        return '"' + texto.replace('"', '""') + '"'
    return texto

#: Etiquetas de los tramos de tiempo entre dos etapas del embudo.
_TRAMOS = {
    0: "menos de 15 min",
    0.25: "15 a 30 min",
    0.5: "30 a 60 min",
    1: "1 a 2 h",
    2: "2 a 4 h",
    4: "4 a 8 h",
    8: "8 a 24 h",
    24: "más de 24 h",
}


def _limite_tramo(clave: Any) -> float | None:
    """Límite inferior del tramo, venga como número o como texto del agregado."""
    if isinstance(clave, bool):
        return None
    if isinstance(clave, (int, float)):
        return float(clave)
    if isinstance(clave, str):
        texto = clave.strip().lower()
        for limite in sorted(_TRAMOS, reverse=True):
            if texto.startswith(f"{limite:g}") or texto.startswith(f"{int(limite)}"):
                return float(limite)
    return None


def _transiciones(filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tramos de tiempo entre etapas consecutivas, con su base declarada."""
    salida: list[dict[str, Any]] = []
    for fila in filas:
        tramos = []
        acumulado: dict[float | None, int] = {}
        for cubo in fila.get("buckets") or []:
            limite = _limite_tramo(cubo.get("_id"))
            acumulado[limite] = acumulado.get(limite, 0) + int(cubo.get("sesiones") or 0)
        for limite, sesiones in sorted(
            acumulado.items(), key=lambda par: (par[0] is None, par[0] or 0)
        ):
            tramos.append(
                {
                    "tramo": _TRAMOS.get(limite, "más de 24 h") if limite is not None else "sin clasificar",
                    "horas": limite,
                    "sesiones": sesiones,
                }
            )
        salida.append(
            {
                "paso": fila.get("paso"),
                "base": sum(tramo["sesiones"] for tramo in tramos),
                "tramos": tramos,
            }
        )
    return salida


def _curvas_y_puntos(
    filas: list[dict[str, Any]], ultimo_dia: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Curvas de retención por segmento y puntos emparejados por cohorte.

    La base de cada semana son solo las cohortes que **ya podían** observarla: no
    se incluyen cohortes cuya semana aún no había terminado y no se promedian
    porcentajes (se suman usuarios y se divide por la base elegible).
    """
    # (cohorte, valor) -> indice -> usuarios/compradores
    celdas: dict[tuple[str, str], dict[int, dict[str, int]]] = {}
    tamano: dict[str, int] = {}
    valores: set[str] = set()
    for fila in filas:
        cohorte = str(fila.get("cohorte"))
        indice = int(fila.get("indice") or 0)
        valor = str(fila.get("valor") or "Sin información")
        usuarios = int(fila.get("usuarios") or 0)
        compradores = int(fila.get("compradores") or 0)
        valores.add(valor)
        celdas.setdefault((cohorte, valor), {}).setdefault(
            indice, {"usuarios": 0, "compradores": 0}
        )
        celdas[(cohorte, valor)][indice]["usuarios"] += usuarios
        celdas[(cohorte, valor)][indice]["compradores"] += compradores
        if indice == 0:
            tamano[cohorte] = tamano.get(cohorte, 0) + usuarios

    cohortes = sorted(tamano)
    elegibles: dict[int, list[str]] = {
        indice: [
            cohorte
            for cohorte in cohortes
            if _ventana(cohorte, indice, ultimo_dia) == "completa"
        ]
        for indice in range(0, 60)
    }

    curvas: list[dict[str, Any]] = []
    for valor in sorted(valores):
        indices = sorted(
            {indice for (cohorte, v), datos in celdas.items() if v == valor for indice in datos}
        )
        puntos = []
        for indice in indices:
            validas = elegibles.get(indice, [])
            # Denominador del **propio segmento**: usuarios de este valor en la semana 0
            # de las cohortes que podían observar este índice. Con la base de toda la
            # cohorte, S0 mostraba la cuota del país dentro de la cohorte (p. ej. 12 %)
            # en vez del 100 % de su propia base.
            base = sum(
                celdas.get((cohorte, valor), {}).get(0, {}).get("usuarios", 0)
                for cohorte in validas
            )
            usuarios = sum(
                celdas.get((cohorte, valor), {}).get(indice, {}).get("usuarios", 0)
                for cohorte in validas
            )
            compradores = sum(
                celdas.get((cohorte, valor), {}).get(indice, {}).get("compradores", 0)
                for cohorte in validas
            )
            puntos.append(
                {
                    "indice": indice,
                    "base": base,
                    "usuarios": usuarios,
                    "compradores": compradores,
                    "retencion": _pct(usuarios, base) if base else None,
                    "compra": _pct(compradores, base) if base else None,
                }
            )
        por_indice = {punto["indice"]: punto for punto in puntos}
        base_s0 = por_indice.get(0, {}).get("base", 0)
        base_s1 = por_indice.get(1, {}).get("base", 0)
        curvas.append(
            {
                "valor": valor,
                "puntos": puntos,
                "base_s0": base_s0,
                "base_s1": base_s1,
                "base_reducida": (base_s1 or base_s0) < UMBRAL_BASE_REDUCIDA,
            }
        )

    puntos_cohorte: list[dict[str, Any]] = []
    for cohorte in cohortes:
        base = tamano.get(cohorte, 0)
        semana_1 = {"usuarios": 0, "compradores": 0}
        for valor in valores:
            datos = celdas.get((cohorte, valor), {}).get(1)
            if datos:
                semana_1["usuarios"] += datos["usuarios"]
                semana_1["compradores"] += datos["compradores"]
        puntos_cohorte.append(
            {
                "cohorte": cohorte,
                "base": base,
                "volvieron": semana_1["usuarios"],
                "compraron": semana_1["compradores"],
                "retorno": _pct(semana_1["usuarios"], base) if base else None,
                "compra": _pct(semana_1["compradores"], base) if base else None,
                "ventana": _ventana(cohorte, 1, ultimo_dia),
            }
        )
    return curvas, puntos_cohorte


#: Base mínima (usuarios) por debajo de la cual una tasa se marca como inestable.
UMBRAL_BASE_REDUCIDA = 100

#: Etiquetas de los resultados de sesión del diagrama de adquisición.
SIN_COMPRA = "Sin compra observada"
CON_COMPRA = "Con compra registrada"


def _sankey(datos: dict[str, Any]) -> dict[str, Any]:
    """Nodos y enlaces de sesiones únicas: fuente → dispositivo → resultado."""
    filas = datos.get("filas") or []
    fuentes = sorted({fila["canal"] for fila in filas})
    dispositivos = sorted({fila["dispositivo"] for fila in filas})
    total = int(datos.get("total_sesiones") or 0)

    sesiones_fuente = {canal: 0 for canal in fuentes}
    sesiones_dispositivo = {dispositivo: 0 for dispositivo in dispositivos}
    enlaces_fuente = {}
    enlaces_dispositivo = {}
    for fila in filas:
        sesiones = fila["sesiones"]
        canal = fila["canal"]
        dispositivo = fila["dispositivo"]
        resultado = CON_COMPRA if fila["con_compra"] else SIN_COMPRA
        sesiones_fuente[canal] += sesiones
        sesiones_dispositivo[dispositivo] += sesiones
        enlaces_fuente[(canal, dispositivo)] = enlaces_fuente.get((canal, dispositivo), 0) + sesiones
        enlaces_dispositivo[(dispositivo, resultado)] = (
            enlaces_dispositivo.get((dispositivo, resultado), 0) + sesiones
        )

    nodos = [
        {"id": f"f:{canal}", "etiqueta": canal, "columna": 0, "sesiones": sesiones_fuente[canal]}
        for canal in fuentes
    ] + [
        {"id": f"d:{dispositivo}", "etiqueta": dispositivo, "columna": 1, "sesiones": sesiones_dispositivo[dispositivo]}
        for dispositivo in dispositivos
    ] + [
        {"id": f"r:{resultado}", "etiqueta": resultado, "columna": 2, "sesiones": datos.get("sesiones_con_compra", 0) if resultado == CON_COMPRA else total - int(datos.get("sesiones_con_compra") or 0)}
        for resultado in (CON_COMPRA, SIN_COMPRA)
    ]

    def enlace(origen: str, destino: str, sesiones: int, desde: int) -> dict[str, Any]:
        return {
            "origen": origen,
            "destino": destino,
            "columna": desde,
            "sesiones": sesiones,
            "pct_origen": _pct(sesiones, sesiones_fuente.get(origen[2:], 0)) if desde == 0 else _pct(sesiones, sesiones_dispositivo.get(origen[2:], 0)),
            "pct_total": _pct(sesiones, total),
        }

    enlaces = [
        enlace(f"f:{canal}", f"d:{dispositivo}", sesiones, 0)
        for (canal, dispositivo), sesiones in sorted(enlaces_fuente.items())
    ] + [
        enlace(f"d:{dispositivo}", f"r:{resultado}", sesiones, 1)
        for (dispositivo, resultado), sesiones in sorted(enlaces_dispositivo.items())
    ]

    salida_columna_0 = sum(item["sesiones"] for item in enlaces if item["columna"] == 0)
    salida_columna_1 = sum(item["sesiones"] for item in enlaces if item["columna"] == 1)
    return {
        "nodos": nodos,
        "enlaces": enlaces,
        "total_sesiones": total,
        "sesiones_con_compra": int(datos.get("sesiones_con_compra") or 0),
        "sesiones_sin_compra": total - int(datos.get("sesiones_con_compra") or 0),
        "sesiones_dispositivo_distinto": int(datos.get("sesiones_dispositivo_distinto") or 0),
        "conciliacion": {
            "unidad": "sesión",
            "salida_fuentes": salida_columna_0,
            "salida_dispositivos": salida_columna_1,
            "cuadra": salida_columna_0 == total and salida_columna_1 == total,
            "regla_dispositivo": (
                "El dispositivo es el de la primera actividad del usuario; las sesiones "
                "con otro dispositivo se atribuyen al de adquisición y se cuentan aparte."
            ),
        },
        "definicion": (
            "Distribución de sesiones únicas por canal de adquisición del usuario, "
            "dispositivo y resultado observado. No es un recorrido de navegación ni "
            "atribución multicanal."
        ),
    }


def _perfil(datos: dict[str, Any]) -> dict[str, Any]:
    """Tasas de avance por canal (N2/N1, N3/N2, N4/N3 y N4/N1)."""
    canales = []
    for fila in datos.get("canales") or []:
        pasos = fila.get("pasos") or {}
        n1, n2, n3, n4 = (int(pasos.get(paso, 0)) for paso in (1, 2, 3, 4))
        canales.append(
            {
                "canal": fila["canal"],
                "n1": n1,
                "n2": n2,
                "n3": n3,
                "n4": n4,
                "tasas": {
                    "n2_n1": _pct(n2, n1),
                    "n3_n2": _pct(n3, n2),
                    "n4_n3": _pct(n4, n3),
                    "n4_n1": _pct(n4, n1),
                },
                "monotono": n1 >= n2 >= n3 >= n4,
                "base_reducida": n1 < UMBRAL_BASE_REDUCIDA,
            }
        )
    global_pasos = datos.get("global") or {}
    g1, g2, g3, g4 = (int(global_pasos.get(paso, 0)) for paso in (1, 2, 3, 4))
    return {
        "canales": canales,
        "global": {
            "n1": g1,
            "n2": g2,
            "n3": g3,
            "n4": g4,
            "tasas": {
                "n2_n1": _pct(g2, g1),
                "n3_n2": _pct(g3, g2),
                "n4_n3": _pct(g4, g3),
                "n4_n1": _pct(g4, g1),
            },
        },
        "umbral_base_reducida": UMBRAL_BASE_REDUCIDA,
        "definicion": (
            "N1…N4 son sesiones que cumplen el embudo en orden y dentro de la ventana. "
            "Cada tasa usa su propio denominador; un denominador cero se devuelve como "
            "ausencia de tasa, no como 0 %."
        ),
    }

def _consistencia(
    indicadores: dict[str, Any],
    canales: list[dict[str, Any]],
    sankey: dict[str, Any],
    serie: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cada indicador frente al total que suman las gráficas del mismo rango.

    No se corrige nada aquí: se compara y se publica el resultado para que el
    tablero pueda mostrar que las tarjetas y los gráficos hablan del mismo
    conjunto. Un ``cuadra: false`` es una señal para revisar, no un cero silencioso.
    """

    def valor(clave: str) -> int:
        return int((indicadores.get(clave) or {}).get("valor") or 0)

    def suma(filas: list[dict[str, Any]], campo: str) -> int:
        return sum(int(fila.get(campo) or 0) for fila in filas)

    pruebas = [
        (
            "eventos",
            "Eventos",
            valor("eventos"),
            suma(serie, "eventos"),
            "serie diaria",
        ),
        (
            "sesiones",
            "Sesiones",
            valor("sesiones"),
            int(sankey.get("total_sesiones") or 0),
            "Sankey de adquisición",
        ),
        (
            "usuarios",
            "Usuarios",
            valor("usuarios"),
            suma(canales, "usuarios"),
            "ranking de canales",
        ),
        (
            "compradores",
            "Compradores",
            valor("compradores"),
            suma(canales, "compradores"),
            "ranking de canales",
        ),
        (
            "compras",
            "Compras",
            valor("compras"),
            suma(canales, "compras"),
            "ranking de canales",
        ),
    ]
    return [
        {
            "clave": clave,
            "etiqueta": etiqueta,
            "indicador": indicador,
            "grafica": grafica,
            "grafica_total": total,
            "diferencia": total - indicador,
            "cuadra": total == indicador,
        }
        for clave, etiqueta, indicador, total, grafica in pruebas
    ]


#: Transiciones del embudo con sus índices de paso (1 → 2 → 3 → 4).
TRANSICIONES_FRICCION = (
    {"clave": "vista_carrito", "etiqueta": "Vista → Carrito", "desde": 1, "hasta": 2},
    {"clave": "carrito_pago", "etiqueta": "Carrito → Pago", "desde": 2, "hasta": 3},
    {"clave": "pago_compra", "etiqueta": "Pago → Compra", "desde": 3, "hasta": 4},
)


def _tasa(numerador: int, denominador: int) -> float | None:
    """Tasa entre 0 y 1; ``None`` cuando no hay base (no se dibuja cero)."""
    if not denominador:
        return None
    return numerador / denominador


def _friccion(datos: dict[str, Any], filtros: MarketingFiltros) -> dict[str, Any]:
    """Brecha de avance y volumen sin avance por canal y transición.

    La referencia se calcula con la **suma de numeradores y denominadores** del
    universo de comparación (todos los canales de los filtros activos), nunca
    promediando tasas por canal.
    """
    canales = datos.get("canales") or []
    global_pasos = datos.get("global") or {}
    n_global = {paso: int(global_pasos.get(paso, 0)) for paso in (1, 2, 3, 4)}

    referencia: dict[str, Any] = {}
    for transicion in TRANSICIONES_FRICCION:
        numerador = n_global[transicion["hasta"]]
        denominador = n_global[transicion["desde"]]
        referencia[transicion["clave"]] = {
            "etiqueta": transicion["etiqueta"],
            "numerador": numerador,
            "denominador": denominador,
            "tasa": _tasa(numerador, denominador),
            "sin_avance": max(0, denominador - numerador),
        }

    filas = []
    for canal in canales:
        pasos = canal.get("pasos") or {}
        n = {paso: int(pasos.get(paso, 0)) for paso in (1, 2, 3, 4)}
        transiciones = []
        for transicion in TRANSICIONES_FRICCION:
            numerador = n[transicion["hasta"]]
            denominador = n[transicion["desde"]]
            tasa = _tasa(numerador, denominador)
            base_global = referencia[transicion["clave"]]["tasa"]
            brecha = None
            if tasa is not None and base_global is not None:
                brecha = round(100 * (tasa - base_global), 1)
            transiciones.append(
                {
                    "clave": transicion["clave"],
                    "etiqueta": transicion["etiqueta"],
                    "numerador": numerador,
                    "denominador": denominador,
                    "tasa": round(tasa, 6) if tasa is not None else None,
                    "sin_avance": max(0, denominador - numerador),
                    "brecha_pp": brecha,
                    "base_pequena": denominador < UMBRAL_BASE_REDUCIDA,
                }
            )
        filas.append(
            {
                "canal": canal["canal"],
                "entrada": n[1],
                "n": n,
                "transiciones": transiciones,
                "base_pequena": n[1] < UMBRAL_BASE_REDUCIDA,
            }
        )

    universo = {
        "canales": len(canales),
        "sesiones_entrada": n_global[1],
        "filtro_canal_activo": bool(filtros.canales),
        "descripcion": (
            "Referencia calculada con los totales de los canales incluidos en los filtros "
            + ("de canal aplicados." if filtros.canales else "activos (todos los canales).")
        ),
    }
    return {
        "transiciones": [dict(transicion) for transicion in TRANSICIONES_FRICCION],
        "referencia": referencia,
        "canales": filas,
        "universo": universo,
        "umbral_base_reducida": UMBRAL_BASE_REDUCIDA,
        "definicion": (
            "Cada transición usa su propio denominador: sesiones que alcanzan el paso anterior "
            "bajo las reglas del embudo. La brecha es 100 × (tasa del canal − tasa de "
            "referencia) en puntos porcentuales. Las sesiones sin avance no son ventas perdidas "
            "ni abandono definitivo."
        ),
    }
