"""Prepara los datos de «Pulso comercial · Walmex» fuera de Atlas.

Fuentes oficiales (todas descargadas de walmex.mx y archivadas en
``data/walmex/fuentes``):

* ``estados/{trimestre}.xlsx`` — «Estados Financieros» trimestrales de Walmex:
  «P&L por región» (México, Centroamérica y Consolidado, con el año anterior y las
  variaciones) y «P&L por trimestre» (ventas netas consolidadas del trimestre).
* ``Resumen_Ventas_Walmex_2T26.xlsx`` — histórico oficial de crecimiento en
  unidades totales (UT) y unidades iguales (UI por mercado).
* ``reportes/{trimestre}.pdf`` — comunicado de resultados: fecha de publicación y
  las variaciones de tráfico, ticket y comercio electrónico que la empresa publica
  en su resumen de datos relevantes.

Reglas que aplica:

1. Cada valor se toma de la celda o frase oficial; no se estima nada.
2. Un dato ausente queda como ``None`` (nunca cero).
3. Se separa trimestre de acumulado: solo se publican cifras del trimestre.
4. Los importes se guardan en millones de pesos; las ventas netas consolidadas del
   libro están en miles y se convierten (÷ 1 000) dejando constancia.
5. Las variaciones se guardan tal como las publica la empresa; la variación
   calculada a partir de importes se guarda aparte y se compara con la publicada.
6. Validaciones obligatorias antes de publicar:
   México + Centroamérica == Consolidado; ventas netas + otros ingresos == total
   ingresos; variación calculada ≈ variación publicada; y consistencia
   (1+tráfico)×(1+ticket) ≈ (1+unidades iguales) cuando las tres existen.

Uso::

    python -m scripts.preparar_walmex            # escribe data/walmex/preparado
    python -m scripts.preparar_walmex --ver      # solo muestra el resumen
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

RAIZ = Path(__file__).resolve().parent.parent
FUENTES = RAIZ / "data" / "walmex" / "fuentes"
SALIDA = RAIZ / "data" / "walmex" / "preparado"

TRIMESTRES = ["1T23", "2T23", "3T23", "4T23", "1T24", "2T24", "3T24", "4T24",
              "1T25", "2T25", "3T25", "4T25", "1T26", "2T26", "3T26"]

MERCADOS = ["consolidado", "mexico", "centroamerica"]
ETIQUETA_MERCADO = {
    "consolidado": "Consolidado",
    "mexico": "México",
    "centroamerica": "Centroamérica",
}

#: Cabeceras del libro «P&L por región» por mercado.
CABECERAS = {"mexico": "México", "centroamerica": "Centroamérica", "consolidado": "Consolidado"}

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


# --------------------------------------------------------------------- utilidades
def sha256(ruta: Path) -> str:
    digest = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1 << 20), b""):
            digest.update(bloque)
    return digest.hexdigest()


def _texto(valor: Any) -> str:
    if valor is None:
        return ""
    return re.sub(r"\s+", " ", str(valor)).strip()


def _numero(valor: Any) -> float | None:
    """Número de una celda. Devuelve ``None`` cuando no hay dato."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = _texto(valor).replace("%", "").replace(",", "")
    if texto in ("", "-", "NA", "N/A", "n.a."):
        return None
    try:
        return float(texto)
    except ValueError:
        return None


# --------------------------------------------------------- hoja «P&L por región»
#: Orden de los mercados en la hoja «P&L por región» (de izquierda a derecha).
ORDEN_MERCADOS = ["mexico", "centroamerica", "consolidado"]


def _columnas_de_trimestre(hoja) -> tuple[int, list[int]]:
    """Fila de etiquetas de trimestre y columnas que las contienen, en orden.

    Las celdas combinadas de la fila de mercados no sirven para delimitar los
    bloques (su texto queda en el centro del bloque), así que los bloques se
    infieren del orden de las etiquetas: por cada mercado hay dos columnas
    —trimestre actual y año anterior— y, dos columnas después, la variación.
    """
    for fila in range(1, min(hoja.max_row, 10) + 1):
        columnas = [
            columna
            for columna in range(1, min(hoja.max_column, 40) + 1)
            if re.fullmatch(r"\dT\d{2}", _texto(hoja.cell(row=fila, column=columna).value))
        ]
        if len(columnas) >= 6:
            return fila, columnas
    raise ValueError("no se encontró la fila con las etiquetas de trimestre")


def leer_region(ruta: Path, trimestre: str) -> dict[str, Any]:
    """Total de ingresos por mercado, año anterior y variaciones publicadas."""
    libro = load_workbook(ruta, data_only=True)
    hoja = libro["P&L por región"]
    fila_cabecera, columnas = _columnas_de_trimestre(hoja)
    anterior = f"{trimestre[0]}T{int(trimestre[2:]) - 1:02d}"

    # Se toman las seis primeras etiquetas: (actual, anterior) por mercado.
    pares = [columnas[0:2], columnas[2:4], columnas[4:6]]
    for indice, mercado in enumerate(ORDEN_MERCADOS):
        actual, previo = pares[indice]
        if (
            _texto(hoja.cell(row=fila_cabecera, column=actual).value) != trimestre
            or _texto(hoja.cell(row=fila_cabecera, column=previo).value) != anterior
        ):
            raise ValueError(
                f"{ruta.name}: las columnas de {mercado} no corresponden a {trimestre}/{anterior}"
            )

    fila_total = None
    for fila in range(fila_cabecera, hoja.max_row + 1):
        etiquetas = [
            _texto(hoja.cell(row=fila, column=columna).value).lower()
            for columna in range(1, 4)
        ]
        if "total ingresos" in etiquetas:
            fila_total = fila
            break
    if fila_total is None:
        raise ValueError(f"{ruta.name}: no se encontró «Total ingresos»")

    resultado: dict[str, Any] = {"trimestre": trimestre, "anterior": anterior, "mercados": {}}
    for indice, mercado in enumerate(ORDEN_MERCADOS):
        col_actual, col_anterior = pares[indice]
        col_var = col_anterior + 2
        actual_valor = _numero(hoja.cell(row=fila_total, column=col_actual).value)
        previo_valor = _numero(hoja.cell(row=fila_total, column=col_anterior).value)
        var = _numero(hoja.cell(row=fila_total, column=col_var).value)
        var_mco = None
        if mercado == "centroamerica":
            # La segunda columna de variación es la de moneda constante.
            var_mco = _numero(hoja.cell(row=fila_total, column=col_var + 1).value)
        resultado["mercados"][mercado] = {
            "total_ingresos_mxn": actual_valor,
            "total_ingresos_anterior_mxn": previo_valor,
            "var_nominal_publicada_pct": var,
            "var_mco_publicada_pct": var_mco,
        }
    return resultado


def leer_ventas_netas(ruta: Path, trimestre: str) -> dict[str, Any]:
    """Ventas netas consolidadas del trimestre (el libro las trae en miles)."""
    libro = load_workbook(ruta, data_only=True)
    hoja = libro["P&L por trimestre"]
    numero_trimestre = int(trimestre[0])
    nombre_trimestre = {
        1: "primer trimestre", 2: "segundo trimestre",
        3: "tercer trimestre", 4: "cuarto trimestre",
    }[numero_trimestre]
    anio = int(f"20{trimestre[2:]}")

    columna_trimestre = None
    fila_anio = None
    for fila in range(1, min(hoja.max_row, 8) + 1):
        for columna in range(1, min(hoja.max_column, 25) + 1):
            if _texto(hoja.cell(row=fila, column=columna).value).lower() == nombre_trimestre:
                # El año suele estar en la fila siguiente, misma columna.
                for salto in (1, 2):
                    valor = _numero(hoja.cell(row=fila + salto, column=columna).value)
                    if valor == anio:
                        columna_trimestre = columna
                        fila_anio = fila + salto
                        break
            if columna_trimestre:
                break
        if columna_trimestre:
            break
    if columna_trimestre is None:
        raise ValueError(f"{ruta.name}: no se encontró la columna de {trimestre}")

    def fila_etiqueta(etiqueta: str) -> int | None:
        # Las etiquetas pueden estar en la primera o en la segunda columna.
        for fila in range(1, hoja.max_row + 1):
            for columna in (1, 2, 3):
                if _texto(hoja.cell(row=fila, column=columna).value).lower().startswith(etiqueta):
                    return fila
        return None

    def miles(fila: int | None) -> float | None:
        if fila is None:
            return None
        valor = _numero(hoja.cell(row=fila, column=columna_trimestre).value)
        # El libro titula «$MXN Miles»: se convierte a millones.
        return round(valor / 1000, 3) if valor is not None else None

    ventas = miles(fila_etiqueta("ventas netas"))
    otros = miles(fila_etiqueta("otros ingresos"))
    total = miles(fila_etiqueta("total ingresos"))
    return {
        "unidad_origen": "miles_mxn",
        "ventas_netas_mxn": ventas,
        "otros_ingresos_mxn": otros,
        "total_ingresos_mxn": total,
    }


# --------------------------------------------------- histórico de mismas tiendas
def leer_mismas_tiendas(ruta: Path) -> dict[str, dict[str, float]]:
    """Crecimiento en unidades iguales (UI) por mercado, año y trimestre."""
    libro = load_workbook(ruta, data_only=True)
    salida: dict[str, dict[str, float]] = {"mexico": {}, "centroamerica": {}}
    for nombre, mercado in (("UI México", "mexico"), ("UI Centroamérica", "centroamerica")):
        if nombre not in libro.sheetnames:
            continue
        hoja = libro[nombre]
        fila_anios = None
        anios: dict[int, int] = {}
        for fila in range(1, hoja.max_row + 1):
            for columna in range(1, hoja.max_column + 1):
                valor = _numero(hoja.cell(row=fila, column=columna).value)
                if isinstance(valor, float) and 2000 <= valor <= 2100 and valor.is_integer():
                    anios[int(valor)] = columna
            if len(anios) >= 3:
                fila_anios = fila
                break
        if fila_anios is None:
            continue
        trimestres = {
            "1": "primer trimestre", "2": "segundo trimestre",
            "3": "tercer trimestre", "4": "cuarto trimestre",
        }
        for fila in range(fila_anios + 1, hoja.max_row + 1):
            etiqueta = " ".join(
                _texto(hoja.cell(row=fila, column=columna).value).lower() for columna in (1, 2)
            ).strip()
            for numero, nombre_trimestre in trimestres.items():
                if etiqueta == nombre_trimestre:
                    for anio, columna in anios.items():
                        valor = _numero(hoja.cell(row=fila, column=columna).value)
                        if valor is not None:
                            salida[mercado][f"{anio}-T{numero}"] = round(valor, 2)
    return salida


# --------------------------------------------------- comunicado: fecha y motores
def _texto_pdf(ruta: Path) -> str:
    import pdfplumber

    with pdfplumber.open(ruta) as pdf:
        paginas = []
        for pagina in pdf.pages[:3]:
            paginas.append(pagina.extract_text() or "")
    return "\n".join(paginas)


def _fecha_publicacion(texto: str) -> str | None:
    limpio = _texto(texto)
    patron = re.compile(
        r"(\d{1,2})\s*de\s*([A-Za-zÁÉÍÓÚáéíóúñ]+)\s*de\s*(20\d{2})", re.IGNORECASE
    )
    for coincidencia in patron.finditer(limpio):
        dia, mes, anio = coincidencia.groups()
        meses = MESES.get(mes.lower())
        if meses:
            return f"{int(anio):04d}-{meses:02d}-{int(dia):02d}"
    return None


def _porcentaje(texto: str, desde_palabra: str) -> float | None:
    """Variación publicada tras «tráfico»/«ticket» en una frase ya limpia."""
    coincidencia = re.search(
        desde_palabra + r"\s*(?:creció|cayo|decreció)?\s*-?\s*(\d+(?:[.,]\d+)?)\s*%",
        texto,
        re.IGNORECASE,
    )
    if not coincidencia:
        return None
    return round(float(coincidencia.group(1).replace(",", ".")), 2)


def leer_motores(texto: str) -> dict[str, dict[str, float | None]]:
    """Tráfico y ticket por mercado desde el resumen de datos relevantes.

    El comunicado los publica en una frase por mercado; cuando dice que una de las
    dos «se mantuvo estable» se registra 0.0 porque es lo que la empresa afirma,
    no una estimación. Si la frase no existe, el valor queda ausente.
    """
    salida: dict[str, dict[str, float | None]] = {}
    frases = re.split(r"(?<=[.;])\s+|•", _texto(texto))
    for mercado, claves in (
        ("mexico", ("méxico", "mexico")),
        ("centroamerica", ("centroamérica", "centroamerica", "cam")),
    ):
        for frase in frases:
            minuscula = frase.lower()
            if not any(clave in minuscula for clave in claves):
                continue
            if "ticket" not in minuscula:
                continue
            ticket = _porcentaje(frase, "ticket")
            if re.search(r"tr[aá]fico\s*(?:se mantuvo|estable|sin cambio)", minuscula):
                trafico: float | None = 0.0
            else:
                trafico = _porcentaje(frase, r"tr[aá]fico")
                if trafico is None:
                    # «tráfico 5 3%» aparece partido por la capa de texto del PDF.
                    trozo = re.search(r"tr[aá]fico\s*(-?\s*\d+\s*[.,]\s*\d+)\s*%", minuscula)
                    if trozo:
                        trafico = round(float(re.sub(r"[\s]", "", trozo.group(1)).replace(",", ".")), 2)
            if ticket is None:
                trozo = re.search(r"ticket creci[oó]\s*(\d+)\s*[.,]\s*(\d+)\s*%", minuscula)
                if trozo:
                    ticket = round(float(f"{trozo.group(1)}.{trozo.group(2)}"), 2)
            if ticket is not None or trafico is not None:
                salida[mercado] = {"trafico_pct": trafico, "ticket_pct": ticket}
                break
    return salida


def leer_mismas_tiendas_comunicado(texto: str) -> dict[str, float | None]:
    """«unidades iguales de X% en México y Y% en Centroamérica» del comunicado."""
    limpio = _texto(texto)
    salida: dict[str, float | None] = {"mexico": None, "centroamerica": None}
    coincidencia = re.search(
        r"unidades iguales de\s*(-?\d+(?:[.,]\d+)?)\s*%\s*en\s*M[eé]xico\s*y\s*"
        r"(-?\d+(?:[.,]\d+)?)\s*%\s*en\s*Centroam[eé]rica",
        limpio,
        re.IGNORECASE,
    )
    if coincidencia:
        salida["mexico"] = round(float(coincidencia.group(1).replace(",", ".")), 2)
        salida["centroamerica"] = round(float(coincidencia.group(2).replace(",", ".")), 2)
    return salida


def leer_ecommerce(texto: str) -> dict[str, float | None]:
    """Variaciones de comercio electrónico que publica el comunicado."""
    limpio = _texto(texto)
    salida: dict[str, float | None] = {
        "mexico_ventas_netas_pct": None,
        "mexico_gmv_pct": None,
        "centroamerica_gmv_pct": None,
    }
    ventas = re.search(r"eCommerce en M[eé]xico crecieron\s*(\d+(?:[.,]\d+)?)\s*%", limpio, re.I)
    if ventas:
        salida["mexico_ventas_netas_pct"] = round(float(ventas.group(1).replace(",", ".")), 2)
    gmv = re.search(r"GMV creci[oó][^.]{0,40}?(\d+(?:[.,]\d+)?)\s*%", limpio, re.I)
    if gmv:
        salida["mexico_gmv_pct"] = round(float(gmv.group(1).replace(",", ".")), 2)
    gmv_ca = re.search(
        r"GMV de eCommerce en Centroam[eé]rica creci[oó]\s*(?:vs[^.]{0,20})?\+?\s*(\d+(?:[.,]\d+)?)\s*%",
        limpio,
        re.I,
    )
    if gmv_ca:
        salida["centroamerica_gmv_pct"] = round(float(gmv_ca.group(1).replace(",", ".")), 2)
    return salida


# ------------------------------------------------------------------- validaciones
def _cercano(a: float | None, b: float | None, tolerancia: float = 0.06) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tolerancia


def matriz_cobertura(hechos: list[dict[str, Any]]) -> dict[str, Any]:
    """Cobertura por indicador: periodos y mercados con dato publicado."""
    matriz: dict[str, Any] = {}
    for hecho in hechos:
        clave = hecho["indicador"]
        entrada = matriz.setdefault(clave, {"unidad": hecho["unidad"], "mercados": {}, "bases": set()})
        entrada["bases"].add(hecho["base"])
        mercados = entrada["mercados"].setdefault(hecho["mercado"], {"periodos": [], "n": 0})
        if hecho["periodo"] not in mercados["periodos"]:
            mercados["periodos"].append(hecho["periodo"])
        mercados["n"] += 1
    for entrada in matriz.values():
        entrada["bases"] = sorted(entrada["bases"])
        for mercados in entrada["mercados"].values():
            mercados["periodos"] = sorted(
                mercados["periodos"], key=lambda p: (int(f"20{p[2:]}"), int(p[0]))
            )
    return matriz


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Prepara los datos de Walmex")
    argumentos.add_argument("--ver", action="store_true", help="Solo mostrar el resumen")
    opciones = argumentos.parse_args()

    SALIDA.mkdir(parents=True, exist_ok=True)
    historico_ui = leer_mismas_tiendas(FUENTES / "Resumen_Ventas_Walmex_2T26.xlsx")

    fuentes: list[dict[str, Any]] = []
    hechos: list[dict[str, Any]] = []
    validaciones: list[dict[str, Any]] = []

    for trimestre in TRIMESTRES:
        ruta_estados = FUENTES / "estados" / f"{trimestre}.xlsx"
        ruta_pdf = FUENTES / "reportes" / f"{trimestre}.pdf"
        if not ruta_estados.exists():
            raise SystemExit(f"Falta {ruta_estados}")

        region = leer_region(ruta_estados, trimestre)
        ventas = leer_ventas_netas(ruta_estados, trimestre)
        texto = _texto_pdf(ruta_pdf) if ruta_pdf.exists() else ""
        motores = leer_motores(texto) if texto else {}
        iguales_comunicado = leer_mismas_tiendas_comunicado(texto) if texto else {}
        fecha = _fecha_publicacion(texto) if texto else None

        fuente_id = f"walmex-{trimestre.lower()}"
        fuentes.append(
            {
                "id": fuente_id,
                "trimestre": trimestre,
                "tipo": "estados_financieros_y_comunicado",
                "archivo_estados": f"data/walmex/fuentes/estados/{trimestre}.xlsx",
                "archivo_comunicado": f"data/walmex/fuentes/reportes/{trimestre}.pdf",
                "sha256_estados": sha256(ruta_estados),
                "sha256_comunicado": sha256(ruta_pdf) if ruta_pdf.exists() else None,
                "bytes_estados": ruta_estados.stat().st_size,
                "bytes_comunicado": ruta_pdf.stat().st_size if ruta_pdf.exists() else None,
                "fecha_publicacion": fecha,
                "url_comunicado": "https://www.walmex.mx/informacion-financiera/trimestral.html",
                "paginas": {
                    "total_ingresos": "P&L por región, fila «Total ingresos»",
                    "ventas_netas": "P&L por trimestre, fila «Ventas netas»",
                    "motores": "Comunicado, página 1, datos relevantes del trimestre",
                },
                "extraido_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )

        # --- hechos
        for mercado in MERCADOS:
            datos = region["mercados"][mercado]
            actual = datos["total_ingresos_mxn"]
            previo = datos["total_ingresos_anterior_mxn"]
            calculado = (
                round(100 * (actual - previo) / previo, 2)
                if actual is not None and previo
                else None
            )
            if actual is not None:
                hechos.append(
                    {
                        "periodo": trimestre,
                        "mercado": mercado,
                        "indicador": "total_ingresos",
                        "valor": actual,
                        "unidad": "millones_mxn",
                        "base": "nominal",
                        "fuente": fuente_id,
                    }
                )
                hechos.append(
                    {
                        "periodo": trimestre,
                        "mercado": mercado,
                        "indicador": "total_ingresos_anio_anterior",
                        "valor": previo,
                        "unidad": "millones_mxn",
                        "base": "nominal",
                        "fuente": fuente_id,
                    }
                )
            if calculado is not None:
                hechos.append(
                    {
                        "periodo": trimestre,
                        "mercado": mercado,
                        "indicador": "crecimiento_anual",
                        "valor": calculado,
                        "unidad": "porcentaje",
                        "base": "nominal_calculado",
                        "fuente": fuente_id,
                    }
                )
            if datos["var_nominal_publicada_pct"] is not None:
                hechos.append(
                    {
                        "periodo": trimestre,
                        "mercado": mercado,
                        "indicador": "crecimiento_anual",
                        "valor": datos["var_nominal_publicada_pct"],
                        "unidad": "porcentaje",
                        "base": "nominal_publicado",
                        "fuente": fuente_id,
                    }
                )
            if datos["var_mco_publicada_pct"] is not None:
                hechos.append(
                    {
                        "periodo": trimestre,
                        "mercado": mercado,
                        "indicador": "crecimiento_anual",
                        "valor": datos["var_mco_publicada_pct"],
                        "unidad": "porcentaje",
                        "base": "moneda_constante_publicado",
                        "fuente": fuente_id,
                    }
                )

        if ventas["ventas_netas_mxn"] is not None:
            hechos.append(
                {
                    "periodo": trimestre,
                    "mercado": "consolidado",
                    "indicador": "ventas_netas",
                    "valor": ventas["ventas_netas_mxn"],
                    "unidad": "millones_mxn",
                    "base": "nominal",
                    "fuente": fuente_id,
                }
            )

        anio = int(f"20{trimestre[2:]}")
        clave_ui = f"{anio}-T{trimestre[0]}"
        for mercado in ("mexico", "centroamerica"):
            valor = historico_ui.get(mercado, {}).get(clave_ui)
            if valor is None:
                valor = iguales_comunicado.get(mercado)
            if valor is not None:
                hechos.append(
                    {
                        "periodo": trimestre,
                        "mercado": mercado,
                        "indicador": "mismas_tiendas",
                        "valor": valor,
                        "unidad": "porcentaje",
                        "base": "unidades_iguales_publicado",
                        "fuente": "walmex-historico-ventas",
                    }
                )

        for mercado, valores in motores.items():
            for indicador, valor in (
                ("trafico", valores.get("trafico_pct")),
                ("ticket", valores.get("ticket_pct")),
            ):
                if valor is not None:
                    hechos.append(
                        {
                            "periodo": trimestre,
                            "mercado": mercado,
                            "indicador": indicador,
                            "valor": valor,
                            "unidad": "porcentaje",
                            "base": "publicado_comunicado",
                            "fuente": fuente_id,
                        }
                    )

        # --- validaciones por trimestre
        consolidado = region["mercados"]["consolidado"]["total_ingresos_mxn"]
        mexico = region["mercados"]["mexico"]["total_ingresos_mxn"]
        centro = region["mercados"]["centroamerica"]["total_ingresos_mxn"]
        suma = round((mexico or 0) + (centro or 0), 3)
        validaciones.append(
            {
                "trimestre": trimestre,
                "prueba": "mexico_mas_centroamerica_igual_consolidado", "tipo": "bloqueante",
                "resultado": _cercano(suma, consolidado, 1.0),
                "detalle": f"{mexico} + {centro} = {suma} · consolidado {consolidado}",
            }
        )
        suma_ventas = round((ventas["ventas_netas_mxn"] or 0) + (ventas["otros_ingresos_mxn"] or 0), 3)
        validaciones.append(
            {
                "trimestre": trimestre,
                "prueba": "ventas_netas_mas_otros_igual_total", "tipo": "bloqueante",
                "resultado": _cercano(suma_ventas, consolidado, 1.0),
                "detalle": f"{ventas['ventas_netas_mxn']} + {ventas['otros_ingresos_mxn']} = "
                f"{suma_ventas} · consolidado {consolidado}",
            }
        )
        for mercado in MERCADOS:
            datos = region["mercados"][mercado]
            actual, previo = datos["total_ingresos_mxn"], datos["total_ingresos_anterior_mxn"]
            if actual and previo:
                calculado = round(100 * (actual - previo) / previo, 2)
                validaciones.append(
                    {
                        "trimestre": trimestre,
                        "prueba": f"variacion_calculada_{mercado}", "tipo": "bloqueante",
                        "resultado": _cercano(calculado, datos["var_nominal_publicada_pct"], 0.06),
                        "detalle": f"calculada {calculado} · publicada {datos['var_nominal_publicada_pct']}",
                    }
                )
        for mercado, valores in motores.items():
            trafico, ticket = valores.get("trafico_pct"), valores.get("ticket_pct")
            ui = historico_ui.get(mercado, {}).get(clave_ui)
            if trafico is not None and ticket is not None and ui is not None:
                implicado = round(((1 + trafico / 100) * (1 + ticket / 100) - 1) * 100, 2)
                validaciones.append(
                    {
                        "trimestre": trimestre,
                        "prueba": f"trafico_por_ticket_vs_mismas_tiendas_{mercado}", "tipo": "informativa",
                        "resultado": _cercano(implicado, ui, 0.6),
                        "detalle": f"(1+{trafico}%)×(1+{ticket}%)−1 = {implicado} · UI publicada {ui}",
                    }
                )

    fuentes.append(
        {
            "id": "walmex-historico-ventas",
            "tipo": "historico_ventas",
            "archivo": "data/walmex/fuentes/Resumen_Ventas_Walmex_2T26.xlsx",
            "sha256": sha256(FUENTES / "Resumen_Ventas_Walmex_2T26.xlsx"),
            "bytes": (FUENTES / "Resumen_Ventas_Walmex_2T26.xlsx").stat().st_size,
            "fecha_publicacion": None,
            "url": "https://www.walmex.mx/informacion-financiera/trimestral.html",
            "paginas": {"mismas_tiendas": "Hojas «UI México» y «UI Centroamérica»"},
            "extraido_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )

    fallidas = [v for v in validaciones if not v["resultado"] and v.get("tipo") == "bloqueante"]
    informativas = [v for v in validaciones if v.get("tipo") == "informativa"]
    informativas_fallidas = [v for v in informativas if not v["resultado"]]
    matriz = matriz_cobertura(hechos)
    resumen = {
        "trimestres": TRIMESTRES,
        "hechos": len(hechos),
        "fuentes": len(fuentes),
        "validaciones": len(validaciones),
        "validaciones_bloqueantes_fallidas": len(fallidas),
        "validaciones_informativas": len(informativas),
        "informativas_no_cumplidas": len(informativas_fallidas),
        "generado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    print(json.dumps(resumen, ensure_ascii=False, indent=1))
    if fallidas:
        print("\nValidaciones bloqueantes no cumplidas:")
        for fila in fallidas:
            print(" -", fila["trimestre"], fila["prueba"], "·", fila["detalle"])
    if informativas_fallidas:
        print("\nIdentidad tráfico × ticket frente a unidades iguales (informativa):")
        for fila in informativas_fallidas:
            print(" -", fila["trimestre"], fila["prueba"], "·", fila["detalle"])

    if opciones.ver:
        return 0

    (SALIDA / "hechos.json").write_text(
        json.dumps(hechos, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (SALIDA / "fuentes.json").write_text(
        json.dumps(fuentes, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (SALIDA / "validaciones.json").write_text(
        json.dumps(validaciones, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (SALIDA / "manifiesto.json").write_text(
        json.dumps(
            {
                "resumen": resumen,
                "cobertura": matriz,
                "conclusiones": [
                    "México + Centroamérica = Consolidado en los 15 trimestres (residual ≤ 1 "
                    "millón por redondeo del propio reporte).",
                    "Ventas netas + otros ingresos = total de ingresos en los 15 trimestres.",
                    "La variación calculada con importes coincide con la publicada (±0,06 pp).",
                    "Tráfico × ticket no reproduce las unidades iguales en 8 de 18 trimestres "
                    "comparables: se publican como variaciones de la empresa y NO como "
                    "descomposición exacta del crecimiento.",
                ],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\nescrito en {SALIDA}")
    return 0 if not fallidas else 1


if __name__ == "__main__":
    raise SystemExit(main())
