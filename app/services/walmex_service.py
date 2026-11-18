"""Servicio del tablero «Pulso comercial · Walmex».

Ventas, crecimiento y desempeño comercial de Walmart de México y Centroamérica con
documentos oficiales. No es un tablero de estados financieros: publica ingresos,
crecimiento (nominal, moneda constante y mismas tiendas), tráfico y ticket por
trimestre y mercado, con la fuente de cada cifra.

Reglas que sostiene este servicio:

* todo sale de la versión activa de Atlas; si no hay versión, responde sin datos;
* los importes y las variaciones nunca se mezclan: cada indicador declara unidad y base;
* un dato no publicado se devuelve como ``None`` con su estado, nunca como cero;
* la identidad México + Centroamérica = Consolidado se comprueba en cada respuesta;
* tráfico y ticket se muestran como variaciones publicadas, no como descomposición.
"""

from __future__ import annotations

from time import monotonic
from typing import Any

from fastapi import HTTPException, status

from app.config import Settings
from app.models.walmex import (
    INDICADORES_IMPORTE,
    MERCADOS,
    WalmexFiltros,
    normalizar_lista,
    orden_periodo,
)
from app.repositories.walmex_repository import WalmexRepository

ETIQUETA_MERCADO = {"consolidado": "Consolidado", "mexico": "México", "centroamerica": "Centroamérica"}
ETIQUETA_INDICADOR = {
    "total_ingresos": "Total de ingresos",
    "total_ingresos_anio_anterior": "Ingresos del año anterior",
    "ventas_netas": "Ventas netas",
    "crecimiento_anual": "Crecimiento anual",
    "mismas_tiendas": "Unidades iguales",
    "trafico": "Tráfico",
    "ticket": "Ticket",
}
UNIDAD = {
    "total_ingresos": "millones_mxn",
    "total_ingresos_anio_anterior": "millones_mxn",
    "ventas_netas": "millones_mxn",
    "crecimiento_anual": "porcentaje",
    "mismas_tiendas": "porcentaje",
    "trafico": "porcentaje",
    "ticket": "porcentaje",
}

#: Bases preferidas para mostrar el crecimiento anual según lo que publique la empresa.
PREFERENCIA_CRECIMIENTO = (
    "nominal_publicado",
    "nominal_calculado",
    "moneda_constante_publicado",
)

NOTA_PUBLICACION = (
    "Cifras de los comunicados trimestrales y de los estados financieros publicados por "
    "Walmart de México y Centroamérica en walmex.mx. Los importes están en millones de pesos "
    "nominales y las variaciones son frente al mismo trimestre del año anterior."
)

#: Indicadores de variación que sirven el mapa de calor y las series de motores.
INDICADORES_MATRIZ = ("crecimiento_anual", "mismas_tiendas", "trafico", "ticket")


class WalmexService:
    _CACHE: dict[str, tuple[float, Any]] = {}

    def __init__(self, repo: WalmexRepository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings
        self.ultimo_calculo_ms = 0

    # ------------------------------------------------------------------ caché
    def _cache_leer(self, clave: str) -> Any | None:
        entrada = WalmexService._CACHE.get(clave)
        if not entrada:
            return None
        creado, valor = entrada
        if monotonic() - creado > self.settings.marketing_cache_ttl_s:
            WalmexService._CACHE.pop(clave, None)
            return None
        return valor

    def _cache_guardar(self, clave: str, valor: Any) -> Any:
        if self.settings.marketing_cache_ttl_s > 0:
            WalmexService._CACHE[clave] = (monotonic(), valor)
        return valor

    def desde_cache(self, endpoint: str, filtros: WalmexFiltros, calcular, extra: str = "") -> tuple[Any, bool]:
        version = self.repo.version_activa()
        clave = "|".join(
            [
                endpoint,
                version or "sin-version",
                ",".join(filtros.mercados),
                filtros.desde or "",
                filtros.hasta or "",
                filtros.indicador or "",
                filtros.base or "",
                ",".join(filtros.indicadores),
                extra,
            ]
        )
        guardado = self._cache_leer(clave)
        if guardado is not None:
            return guardado, True
        inicio = monotonic()
        datos = calcular()
        self.ultimo_calculo_ms = round((monotonic() - inicio) * 1000)
        return self._cache_guardar(clave, datos), False

    # ------------------------------------------------------------------ base
    def _disponible(self) -> str | None:
        version = self.repo.version_activa()
        if not version:
            return None
        if not self.repo.catalogo(version):
            return None
        return version

    def _sin_datos(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        base = {
            "datos_disponibles": False,
            "version": None,
            "motivo": (
                "No hay una versión publicada de «Pulso comercial · Walmex» en MongoDB Atlas. "
                "Publica una con `scripts.publicar_walmex --aplicar`."
            ),
            "filtros_activos": [],
            "avisos": [],
        }
        base.update(extra or {})
        return base

    def _validar(self, catalogo: dict[str, Any], filtros: WalmexFiltros) -> None:
        mercados = {fila["clave"] for fila in catalogo.get("mercados", [])}
        for mercado in filtros.mercados:
            if mercado not in mercados:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Mercado no publicado: {mercado}. Usa: {', '.join(sorted(mercados))}.",
                )
        periodos = [fila["periodo"] for fila in catalogo.get("periodos", [])]
        for periodo in (filtros.desde, filtros.hasta):
            if periodo and periodo not in periodos:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"Periodo no publicado: {periodo}. Disponibles: {periodos[0]} a {periodos[-1]}."
                    ),
                )
        if filtros.desde and filtros.hasta and orden_periodo(filtros.desde) > orden_periodo(filtros.hasta):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="El periodo inicial no puede ser posterior al final.",
            )
        if filtros.indicador and filtros.indicador not in ETIQUETA_INDICADOR:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Indicador no publicado: {filtros.indicador}.",
            )
        for indicador in filtros.indicadores:
            if indicador not in ETIQUETA_INDICADOR:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Indicador no publicado: {indicador}.",
                )
        combos = catalogo.get("combinaciones", [])
        solicitados = list(filtros.indicadores) or ([filtros.indicador] if filtros.indicador else [])
        for indicador in solicitados:
            # Basta con que el indicador exista para algún mercado pedido: los
            # mercados que no lo publican se devuelven como «no publicado».
            publicado = any(
                combo["indicador"] == indicador
                and (not filtros.mercados or combo["mercado"] in filtros.mercados)
                for combo in combos
            )
            if not publicado:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"El indicador «{indicador}» no está publicado para los mercados "
                        "seleccionados."
                    ),
                )

    # ----------------------------------------------------------------- estado
    def estado(self) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos()
        catalogo = self.repo.catalogo(version) or {}
        importacion = self.repo.importacion(version) or {}
        estado = self.repo.estado() or {}
        fuentes = self.repo.fuentes()
        periodos = catalogo.get("periodos", [])
        ultimo = periodos[-1] if periodos else {}
        return {
            "datos_disponibles": True,
            "version": version,
            "fuente": {
                "nombre": "Walmart de México y Centroamérica (Walmex)",
                "sitio": "https://www.walmex.mx/comunicados-y-eventos/comunicados-a-bmv.html",
                "documentos": len(fuentes),
                "ultima_publicacion": ultimo.get("fecha_publicacion"),
            },
            "periodo": {
                "ultimo": ultimo.get("periodo"),
                "etiqueta": ultimo.get("etiqueta"),
                "desde": periodos[0]["periodo"] if periodos else None,
                "hasta": ultimo.get("periodo"),
                "trimestres": len(periodos),
            },
            "publicacion": {
                "publicado_en": estado.get("publicado_en"),
                "extraid_en": importacion.get("creado_en"),
                "nota": (
                    "La fecha de publicación es la del comunicado oficial; la de extracción es "
                    "cuándo se preparó esta versión en el portafolio."
                ),
            },
            "modelo": importacion.get("modelo"),
            "persistencia": {
                "motor": "mongodb",
                "servicio": "MongoDB Atlas",
                "solo_agregados": True,
                "detalle": "no se publica detalle por tienda, formato, producto ni cliente",
            },
            "cobertura": catalogo.get("cobertura"),
            "notas": catalogo.get("notas", []),
            "avisos": catalogo.get("notas", []),
            "filtros_activos": [],
        }

    # ---------------------------------------------------------------- catálogo
    def catalogo(self, filtros: WalmexFiltros) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos({"indicadores": [], "mercados": [], "periodos": []})
        catalogo = self.repo.catalogo(version) or {}
        return {
            "datos_disponibles": True,
            "version": version,
            **catalogo,
            "filtros_activos": filtros.activos(),
        }

    # ----------------------------------------------------------------- resumen
    def resumen(self, filtros: WalmexFiltros) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos({"indicadores": [], "series": [], "consistencia": []})
        catalogo = self.repo.catalogo(version) or {}
        self._validar(catalogo, filtros)

        hechos = self.repo.hechos(version, filtros)
        if not hechos:
            return self._sin_datos({"indicadores": [], "series": [], "consistencia": []})

        periodos = sorted({hecho["periodo"] for hecho in hechos}, key=orden_periodo)
        seleccionado = periodos[-1]
        mercados = filtros.mercados or [mercado for mercado in MERCADOS if any(h["mercado"] == mercado for h in hechos)]

        def buscar(mercado: str, indicador: str, periodo: str) -> dict[str, Any] | None:
            candidatos = [
                hecho
                for hecho in hechos
                if hecho["mercado"] == mercado
                and hecho["indicador"] == indicador
                and hecho["periodo"] == periodo
            ]
            if not candidatos:
                return None
            for base in PREFERENCIA_CRECIMIENTO:
                for hecho in candidatos:
                    if hecho["base"] == base:
                        return hecho
            return candidatos[0]

        indicadores = []
        for indicador in (
            "total_ingresos",
            "crecimiento_anual",
            "mismas_tiendas",
            "trafico",
            "ticket",
        ):
            for mercado in mercados:
                hecho = buscar(mercado, indicador, seleccionado)
                valor = hecho["valor"] if hecho else None
                indicadores.append(
                    {
                        "clave": f"{indicador}:{mercado}",
                        "indicador": indicador,
                        "mercado": mercado,
                        "etiqueta": f"{ETIQUETA_INDICADOR[indicador]} · {ETIQUETA_MERCADO[mercado]}",
                        "valor": valor,
                        "unidad": UNIDAD[indicador],
                        "base": hecho["base"] if hecho else None,
                        "publicado": bool(hecho and hecho.get("publicado")),
                        "derivado": bool(hecho and hecho.get("derivado")),
                        "estado": "publicado" if hecho else "no_publicado",
                        "periodo": seleccionado,
                        "fuente": hecho["fuente"] if hecho else None,
                    }
                )

        series = []
        for mercado in mercados:
            ingresos = []
            for periodo in periodos:
                hecho = buscar(mercado, "total_ingresos", periodo)
                ingresos.append(
                    {"periodo": periodo, "valor": hecho["valor"] if hecho else None}
                )
            crecimiento = []
            for periodo in periodos:
                hecho = buscar(mercado, "crecimiento_anual", periodo)
                crecimiento.append(
                    {
                        "periodo": periodo,
                        "valor": hecho["valor"] if hecho else None,
                        "base": hecho["base"] if hecho else None,
                    }
                )
            # Ingresos del mismo trimestre del año anterior: la cascada compara
            # importes de la misma definición y escala, no porcentajes.
            anterior = []
            for periodo in periodos:
                hecho = buscar(mercado, "total_ingresos_anio_anterior", periodo)
                anterior.append(
                    {"periodo": periodo, "valor": hecho["valor"] if hecho else None}
                )
            series.append(
                {
                    "mercado": mercado,
                    "etiqueta": ETIQUETA_MERCADO[mercado],
                    "ingresos": ingresos,
                    "ingresos_anio_anterior": anterior,
                    "crecimiento": crecimiento,
                }
            )

        consistencia = _consistencia(hechos, periodos)
        return {
            "datos_disponibles": True,
            "version": version,
            "periodo_seleccionado": seleccionado,
            "periodos": periodos,
            "mercados": mercados,
            "indicadores": indicadores,
            "series": series,
            "consistencia": consistencia,
            "definiciones": {
                "importes": "millones de pesos mexicanos nominales del trimestre",
                "variaciones": "porcentaje frente al mismo trimestre del año anterior",
                "mismas_tiendas": "variación de ventas a unidades iguales que publica la empresa",
            },
            "notas": catalogo.get("notas", []),
            "filtros_activos": filtros.activos(),
            "avisos": [
                NOTA_PUBLICACION,
            ],
        }

    # ----------------------------------------------------------------- motores
    def motores(self, filtros: WalmexFiltros) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos({"paneles": []})
        catalogo = self.repo.catalogo(version) or {}
        self._validar(catalogo, filtros)
        hechos = self.repo.hechos(version, filtros)
        mercados = filtros.mercados or ["mexico", "centroamerica"]

        paneles = []
        for mercado in mercados:
            traficos = {
                hecho["periodo"]: hecho["valor"]
                for hecho in hechos
                if hecho["mercado"] == mercado and hecho["indicador"] == "trafico"
            }
            tickets = {
                hecho["periodo"]: hecho["valor"]
                for hecho in hechos
                if hecho["mercado"] == mercado and hecho["indicador"] == "ticket"
            }
            iguales = {
                hecho["periodo"]: hecho["valor"]
                for hecho in hechos
                if hecho["mercado"] == mercado and hecho["indicador"] == "mismas_tiendas"
            }
            puntos = [
                {
                    "periodo": periodo,
                    "trafico": traficos.get(periodo),
                    "ticket": tickets.get(periodo),
                    "mismas_tiendas": iguales.get(periodo),
                    "implicado": (
                        round(
                            ((1 + traficos[periodo] / 100) * (1 + tickets[periodo] / 100) - 1) * 100,
                            2,
                        )
                        if periodo in traficos and periodo in tickets
                        else None
                    ),
                }
                for periodo in sorted(set(traficos) | set(tickets), key=orden_periodo)
                if periodo in traficos and periodo in tickets
            ]
            periodos_sin_datos = sorted(
                (set(traficos) | set(tickets) | set(iguales)) - {punto["periodo"] for punto in puntos},
                key=orden_periodo,
            )
            paneles.append(
                {
                    "mercado": mercado,
                    "etiqueta": ETIQUETA_MERCADO[mercado],
                    "puntos": puntos,
                    "periodos_incompletos": periodos_sin_datos,
                }
            )

        return {
            "datos_disponibles": True,
            "version": version,
            "unidad": "porcentaje",
            "paneles": paneles,
            "definicion": (
                "Cada punto es un trimestre: en el eje horizontal la variación del tráfico y en "
                "el vertical la del ticket, ambas publicadas por la empresa. La línea une "
                "trimestres consecutivos y no implica causalidad."
            ),
            "advertencia": (
                "Tráfico y ticket son variaciones que publica la empresa; su producto no "
                "reproduce exactamente las unidades iguales en todos los trimestres, así que no "
                "se presentan como descomposición exacta del crecimiento."
            ),
            "notas": catalogo.get("notas", []),
            "filtros_activos": filtros.activos(),
            "avisos": [NOTA_PUBLICACION],
        }

    # --------------------------------------------------------------- desempeño
    def desempeno(self, filtros: WalmexFiltros) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos({"filas": [], "periodos": []})
        catalogo = self.repo.catalogo(version) or {}
        indicador = filtros.indicador or "crecimiento_anual"
        filtros_indicador = WalmexFiltros(
            mercados=filtros.mercados,
            desde=filtros.desde,
            hasta=filtros.hasta,
            indicador=indicador,
            base=filtros.base,
        )
        self._validar(catalogo, filtros_indicador)
        hechos = self.repo.hechos(version, filtros_indicador)

        bases = sorted({hecho["base"] for hecho in hechos})
        # Base por omisión: la publicada nominalmente, que cubre todos los mercados.
        preferidas = [
            "nominal_publicado",
            "unidades_iguales_publicado",
            "publicado_comunicado",
            "nominal_calculado",
            "moneda_constante_publicado",
        ]
        predeterminada = next((opcion for opcion in preferidas if opcion in bases), bases[0] if bases else None)
        base = filtros.base if filtros.base in bases else predeterminada
        seleccionados = [hecho for hecho in hechos if hecho["base"] == base]
        periodos = sorted({hecho["periodo"] for hecho in seleccionados}, key=orden_periodo)
        pedidos = filtros.mercados or list(MERCADOS)
        mercados = [m for m in MERCADOS if m in pedidos and any(h["mercado"] == m for h in seleccionados)]

        def celda(mercado: str, periodo: str) -> dict[str, Any]:
            hecho = next(
                (
                    fila
                    for fila in seleccionados
                    if fila["mercado"] == mercado and fila["periodo"] == periodo
                ),
                None,
            )
            if not hecho:
                return {"periodo": periodo, "valor": None, "estado": "no_publicado"}
            if hecho["valor"] == 0:
                return {
                    "periodo": periodo,
                    "valor": 0,
                    "estado": "cero",
                    "fuente": hecho["fuente"],
                }
            return {
                "periodo": periodo,
                "valor": hecho["valor"],
                "estado": "publicado",
                "fuente": hecho["fuente"],
                "publicado": hecho.get("publicado"),
                "derivado": hecho.get("derivado"),
            }

        filas = [
            {
                "mercado": mercado,
                "etiqueta": ETIQUETA_MERCADO[mercado],
                "celdas": [celda(mercado, periodo) for periodo in periodos],
            }
            for mercado in mercados
        ]
        ultimo = periodos[-1] if periodos else None
        ranking = sorted(
            [
                {
                    "mercado": fila["mercado"],
                    "etiqueta": fila["etiqueta"],
                    "valor": next(
                        (c["valor"] for c in fila["celdas"] if c["periodo"] == ultimo), None
                    ),
                }
                for fila in filas
                if any(c["periodo"] == ultimo and c["valor"] is not None for c in fila["celdas"])
            ],
            key=lambda entrada: (entrada["valor"] is None, -(entrada["valor"] or 0)),
        )

        return {
            "datos_disponibles": True,
            "version": version,
            "indicador": indicador,
            "etiqueta": ETIQUETA_INDICADOR.get(indicador, indicador),
            "unidad": UNIDAD.get(indicador, "porcentaje"),
            "base": base,
            "bases": bases,
            "periodos": periodos,
            "filas": filas,
            "periodo_ranking": ultimo,
            "ranking": ranking,
            "divergente": UNIDAD.get(indicador) == "porcentaje",
            "notas": catalogo.get("notas", []),
            "filtros_activos": filtros.activos(),
            "avisos": [NOTA_PUBLICACION],
        }

    # ------------------------------------------------------------------ matriz
    def matriz(self, filtros: WalmexFiltros) -> dict[str, Any]:
        """Indicadores de variación × mercado × trimestre en una sola lectura.

        Sirve al mapa de calor y a las series de motores: los mismos hechos
        agrupados, sin colecciones nuevas ni cálculos materializados. Cada celda
        declara su estado —publicado, cero o no publicado—, su base y su fuente.
        """
        version = self._disponible()
        if not version:
            return self._sin_datos(
                {"indicadores": [], "mercados": [], "periodos": [], "celdas": []}
            )
        catalogo = self.repo.catalogo(version) or {}
        pedidos = filtros.indicadores or list(INDICADORES_MATRIZ)
        filtros_matriz = WalmexFiltros(
            mercados=filtros.mercados,
            desde=filtros.desde,
            hasta=filtros.hasta,
            base=filtros.base,
            indicadores=pedidos,
        )
        self._validar(catalogo, filtros_matriz)
        hechos = self.repo.hechos(version, filtros_matriz)
        if not hechos:
            return self._sin_datos(
                {"indicadores": [], "mercados": [], "periodos": [], "celdas": []}
            )

        periodos = sorted({hecho["periodo"] for hecho in hechos}, key=orden_periodo)
        pedidos_mercados = filtros.mercados or ["mexico", "centroamerica"]
        mercados = [
            mercado
            for mercado in pedidos_mercados
            if any(hecho["mercado"] == mercado for hecho in hechos)
        ]

        def base_de(indicador: str) -> str | None:
            """Base publicada del indicador, con el mismo criterio que /desempeno."""
            disponibles = sorted(
                {hecho["base"] for hecho in hechos if hecho["indicador"] == indicador}
            )
            preferidas = (
                PREFERENCIA_CRECIMIENTO
                if indicador == "crecimiento_anual"
                else ("unidades_iguales_publicado",)
                if indicador == "mismas_tiendas"
                else ("publicado_comunicado",)
            )
            elegida = next((opcion for opcion in preferidas if opcion in disponibles), None)
            return elegida or (disponibles[0] if disponibles else None)

        bases = {indicador: base_de(indicador) for indicador in pedidos}
        anios = sorted({periodo[2:] for periodo in periodos})

        celdas: list[dict[str, Any]] = []
        for indicador in pedidos:
            base = bases[indicador]
            seleccionados = [
                hecho
                for hecho in hechos
                if hecho["indicador"] == indicador and hecho["base"] == base
            ]
            for mercado in mercados:
                for periodo in periodos:
                    hecho = next(
                        (
                            fila
                            for fila in seleccionados
                            if fila["mercado"] == mercado and fila["periodo"] == periodo
                        ),
                        None,
                    )
                    if not hecho:
                        celdas.append(
                            {
                                "indicador": indicador,
                                "mercado": mercado,
                                "periodo": periodo,
                                "valor": None,
                                "estado": "no_publicado",
                                "base": base,
                                "fuente": None,
                                "publicado": False,
                                "derivado": False,
                            }
                        )
                        continue
                    valor = hecho["valor"]
                    celdas.append(
                        {
                            "indicador": indicador,
                            "mercado": mercado,
                            "periodo": periodo,
                            "valor": valor,
                            "estado": "cero" if valor == 0 else "publicado",
                            "base": hecho["base"],
                            "fuente": hecho["fuente"],
                            "publicado": bool(hecho.get("publicado")),
                            "derivado": bool(hecho.get("derivado")),
                        }
                    )

        valores = [
            celda["valor"]
            for celda in celdas
            if celda["valor"] is not None and celda["estado"] == "publicado"
        ]
        maximo = max((abs(valor) for valor in valores), default=0.0)

        indicadores = [
            {
                "clave": indicador,
                "etiqueta": ETIQUETA_INDICADOR[indicador],
                "unidad": UNIDAD[indicador],
                "base": bases[indicador],
                "definicion": (
                    "variación de ventas a unidades iguales publicada por la empresa"
                    if indicador == "mismas_tiendas"
                    else "variación porcentual frente al mismo trimestre del año anterior"
                    if indicador in {"crecimiento_anual", "trafico", "ticket"}
                    else "importe publicado"
                ),
                "periodos_con_dato": len(
                    {
                        celda["periodo"]
                        for celda in celdas
                        if celda["indicador"] == indicador and celda["valor"] is not None
                    }
                ),
            }
            for indicador in pedidos
        ]

        return {
            "datos_disponibles": True,
            "version": version,
            "unidad": "porcentaje",
            "anios": anios,
            "indicadores": indicadores,
            "mercados": [
                {"clave": mercado, "etiqueta": ETIQUETA_MERCADO[mercado]} for mercado in mercados
            ],
            "periodos": periodos,
            "celdas": celdas,
            "dominio": {"min": -maximo, "max": maximo},
            "notas": catalogo.get("notas", []),
            "filtros_activos": filtros.activos(),
            "avisos": [NOTA_PUBLICACION],
        }

    # ------------------------------------------------------------------ fuentes
    def fuentes(self, filtros: WalmexFiltros) -> dict[str, Any]:
        version = self._disponible()
        if not version:
            return self._sin_datos({"fuentes": []})
        fuentes = self.repo.fuentes()
        periodos = sorted(
            {fuente.get("trimestre") for fuente in fuentes if fuente.get("trimestre")},
            key=orden_periodo,
        )
        if filtros.desde:
            periodos = [p for p in periodos if orden_periodo(p) >= orden_periodo(filtros.desde)]
        if filtros.hasta:
            periodos = [p for p in periodos if orden_periodo(p) <= orden_periodo(filtros.hasta)]
        return {
            "datos_disponibles": True,
            "version": version,
            "fuentes": [f for f in fuentes if f.get("trimestre") in periodos],
            "periodos": periodos,
            "filtros_activos": filtros.activos(),
        }

    # --------------------------------------------------------------- exportar
    def exportar(self, filtros: WalmexFiltros) -> tuple[str, str]:
        version = self._disponible()
        if not version:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No hay datos publicados que exportar.",
            )
        catalogo = self.repo.catalogo(version) or {}
        self._validar(catalogo, filtros)
        hechos = self.repo.hechos(version, filtros)
        importacion = self.repo.importacion(version) or {}
        periodos = sorted({hecho["periodo"] for hecho in hechos}, key=orden_periodo)
        activos = filtros.activos()
        cabecera = [
            "# Pulso comercial · Walmex — exportación de datos agrupados",
            "# Fuente: comunicados trimestrales y estados financieros de Walmex "
            "(https://www.walmex.mx/comunicados-y-eventos/comunicados-a-bmv.html)",
            f"# Versión publicada: {version} · periodos {periodos[0] if periodos else '-'} a "
            f"{periodos[-1] if periodos else '-'}",
            "# Unidades: millones_mxn = millones de pesos mexicanos nominales; porcentaje = "
            "variación frente al mismo trimestre del año anterior.",
            "# Origen: publicado = cifra del comunicado o de los estados financieros; "
            "derivado = cálculo aritmético sobre importes publicados (se indica la base).",
            "# Filtros: "
            + ("ninguno" if not activos else "; ".join(
                f"{a['etiqueta']}={', '.join(map(str, a['valores']))}" for a in activos
            )),
            "# No incluye detalle por tienda, formato, producto ni cliente.",
            "# Columnas: periodo,mercado,indicador,base,valor,unidad,origen,fuente",
        ]
        columnas = [
            "periodo",
            "mercado",
            "indicador",
            "base",
            "valor",
            "unidad",
            "origen",
            "fuente",
        ]
        partes = list(cabecera)
        partes.append(",".join(columnas))
        for hecho in hechos:
            fila = {
                "periodo": hecho.get("periodo"),
                "mercado": hecho.get("mercado"),
                "indicador": hecho.get("indicador"),
                "base": hecho.get("base"),
                "valor": hecho.get("valor"),
                "unidad": hecho.get("unidad"),
                "origen": "derivado" if hecho.get("derivado") else "publicado",
                "fuente": hecho.get("fuente"),
            }
            partes.append(",".join(_csv(fila[columna]) for columna in columnas))
        nombre = f"pulso-comercial-walmex-{version}.csv"
        return "\n".join(partes) + "\n", nombre


def _csv(valor: Any) -> str:
    if valor is None:
        return ""
    texto = str(valor)
    if any(caracter in texto for caracter in [",", '"', "\n"]):
        return '"' + texto.replace('"', '""') + '"'
    return texto


def _consistencia(hechos: list[dict[str, Any]], periodos: list[str]) -> list[dict[str, Any]]:
    """Comprobaciones aritméticas que el tablero muestra con sus cifras."""

    def valor(mercado: str, indicador: str, periodo: str, base: str | None = None) -> float | None:
        for hecho in hechos:
            if (
                hecho["mercado"] == mercado
                and hecho["indicador"] == indicador
                and hecho["periodo"] == periodo
                and (base is None or hecho["base"] == base)
            ):
                return hecho["valor"]
        return None

    filas = []
    for periodo in periodos:
        mexico = valor("mexico", "total_ingresos", periodo)
        centro = valor("centroamerica", "total_ingresos", periodo)
        consolidado = valor("consolidado", "total_ingresos", periodo)
        suma = round((mexico or 0) + (centro or 0), 3)
        filas.append(
            {
                "periodo": periodo,
                "prueba": "México + Centroamérica = Consolidado",
                "izquierda": suma,
                "derecha": consolidado,
                "diferencia": round(suma - (consolidado or 0), 3),
                "cuadra": bool(consolidado is not None and abs(suma - consolidado) <= 1),
                "unidad": "millones_mxn",
            }
        )
    return filas
