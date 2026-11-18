"""Verificación de los endpoints del Atlas vial contra la API real.

Comprueba, con los datos publicados:
  · que los indicadores coinciden con la auditoría de los CSV;
  · que el mapa reparte los hechos sin perder ni duplicar registros;
  · que la serie, la matriz y las distribuciones cuadran con el total;
  · que la tabla y la exportación devuelven el mismo conjunto filtrado;
  · que los filtros se combinan como se documenta (OR dentro, AND entre).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8010/api/v1/atlas-vial"

# Cifras verificadas leyendo los cinco CSV completos (docs/atlas-vial/auditoria-fuentes.json).
ESPERADO = {
    "hechos": 30655,
    "lesionadas": 36266,
    "fallecidas": 533,
    "con_fallecimientos": 512,
}


def get(ruta: str, **parametros) -> dict:
    partes = []
    for clave, valor in parametros.items():
        if valor is None:
            continue
        if isinstance(valor, (list, tuple)):
            for item in valor:
                partes.append((clave, str(item)))
        else:
            partes.append((clave, str(valor)))
    consulta = urllib.parse.urlencode(partes)
    url = f"{BASE}{ruta}{'?' + consulta if consulta else ''}"
    with urllib.request.urlopen(url, timeout=60) as respuesta:  # noqa: S310
        return json.loads(respuesta.read().decode("utf-8"))


def get_texto(ruta: str, **parametros) -> str:
    consulta = urllib.parse.urlencode([(clave, str(valor)) for clave, valor in parametros.items()])
    url = f"{BASE}{ruta}{'?' + consulta if consulta else ''}"
    with urllib.request.urlopen(url, timeout=120) as respuesta:  # noqa: S310
        return respuesta.read().decode("utf-8-sig")


def comprobar(nombre: str, ok: bool, detalle: str = "") -> bool:
    print(f"{'OK  ' if ok else 'FALLA'} {nombre}{' :: ' + detalle if detalle else ''}")
    return ok


def main() -> int:
    resultados = []

    estado = get("/estado")
    resultados.append(comprobar("La API publica una versión real", estado.get("publicada") is True, str(estado.get("version"))))
    resultados.append(
        comprobar(
            "El periodo de los eventos es 2024",
            estado.get("periodo", {}).get("desde") == "2024-01-01"
            and estado.get("periodo", {}).get("hasta") == "2024-12-31",
            json.dumps(estado.get("periodo")),
        )
    )

    catalogo = get("/catalogo")
    resultados.append(
        comprobar(
            "El catálogo trae los cinco recursos con su huella",
            len(catalogo.get("fuente", {}).get("recursos", {})) == 5,
            str(sorted(catalogo.get("fuente", {}).get("recursos", {}))),
        )
    )
    dominios = catalogo.get("filtros", {})
    resultados.append(
        comprobar(
            "Los filtros declaran sus dominios reales",
            len(dominios.get("alcaldias", [])) == 16 and len(dominios.get("tipos_evento", [])) >= 5,
            f"{len(dominios.get('alcaldias', []))} alcaldías, {len(dominios.get('tipos_evento', []))} tipos",
        )
    )

    resumen = get("/resumen")
    indicadores = resumen["indicadores"]
    resultados.append(
        comprobar(
            "Hechos registrados coinciden con la auditoría",
            indicadores["hechos"]["valor"] == ESPERADO["hechos"],
            str(indicadores["hechos"]["valor"]),
        )
    )
    resultados.append(
        comprobar(
            "Personas lesionadas y fallecidas coinciden",
            indicadores["personas_lesionadas"]["valor"] == ESPERADO["lesionadas"]
            and indicadores["personas_fallecidas"]["valor"] == ESPERADO["fallecidas"],
            f"{indicadores['personas_lesionadas']['valor']} / {indicadores['personas_fallecidas']['valor']}",
        )
    )
    resultados.append(
        comprobar(
            "Hechos con fallecimientos coinciden",
            indicadores["hechos_con_fallecimientos"]["valor"] == ESPERADO["con_fallecimientos"],
            str(indicadores["hechos_con_fallecimientos"]["valor"]),
        )
    )
    cobertura = resumen["cobertura"]
    resultados.append(
        comprobar(
            "La cobertura de coordenadas se informa",
            cobertura["con_coordenadas"] + cobertura["sin_coordenadas"] + cobertura["fuera_de_cdmx"]
            == ESPERADO["hechos"],
            f"{cobertura['con_coordenadas']} con coordenadas, {cobertura['sin_coordenadas']} sin ellas",
        )
    )

    territorio = get("/territorio")
    suma_alcaldias = sum(fila["hechos"] for fila in territorio["alcaldias"])
    resultados.append(
        comprobar(
            "El mapa reparte todos los hechos sin duplicarlos",
            suma_alcaldias == ESPERADO["hechos"],
            f"{suma_alcaldias} de {ESPERADO['hechos']}",
        )
    )
    resultados.append(
        comprobar(
            "El mapa trae los puntos geolocalizados",
            territorio["puntos"]["total"] > 30000,
            str(territorio["puntos"]["total"]),
        )
    )

    geometria = get("/geometria")
    resultados.append(
        comprobar(
            "La geometría tiene 16 alcaldías y su atribución",
            len(geometria["features"]) == 16 and "licencia" in geometria["atribucion"],
            geometria["atribucion"]["licencia"],
        )
    )

    tiempo = get("/tiempo", agregacion="mes")
    suma_serie = sum(fila["hechos"] for fila in tiempo["serie"])
    resultados.append(
        comprobar("La serie mensual cuadra con el total", suma_serie == ESPERADO["hechos"], f"{suma_serie}"))
    resultados.append(
        comprobar(
            "La matriz día × hora cuadra con el total",
            tiempo["matriz"]["total_hechos"] + resumen["cobertura"]["sin_hora"] == ESPERADO["hechos"],
            f"{tiempo['matriz']['total_hechos']} con hora, {resumen['cobertura']['sin_hora']} sin ella",
        )
    )
    suma_tipos = sum(fila["hechos"] for fila in tiempo["distribuciones"]["tipo_evento"])
    resultados.append(
        comprobar("La distribución por tipo de evento cuadra", suma_tipos == ESPERADO["hechos"], f"{suma_tipos}")
    )

    personas = get("/personas")
    suma_usuarios = sum(fila["hechos"] for fila in personas["por_tipo_usuario"])
    suma_sexos = sum(fila["hechos"] for fila in personas["por_sexo"])
    resultados.append(
        comprobar(
            "Los desgloses de personas cuentan hechos, no multiplican víctimas",
            suma_usuarios == ESPERADO["hechos"] and suma_sexos == ESPERADO["hechos"],
            f"{suma_usuarios} hechos por tipo de persona y {suma_sexos} por sexo",
        )
    )
    suma_edad = sum(fila["hechos"] for fila in personas["por_edad"])
    resultados.append(
        comprobar("Los grupos de edad cubren todos los hechos", suma_edad == ESPERADO["hechos"], f"{suma_edad}")
    )

    tabla = get("/tabla", tamano=5)
    resultados.append(
        comprobar(
            "La tabla pagina y declara el total",
            tabla["total"] == ESPERADO["hechos"] and len(tabla["filas"]) == 5 and tabla["paginas"] > 1,
            f"{len(tabla['filas'])} filas de {tabla['total']}",
        )
    )

    # Filtros: una alcaldía concreta y combinación de dos dimensiones.
    una = get("/resumen", alcaldia="Cuauhtémoc")
    resultados.append(
        comprobar(
            "El filtro por alcaldía reduce el conjunto",
            0 < una["indicadores"]["hechos"]["valor"] < ESPERADO["hechos"],
            f"Cuauhtémoc: {una['indicadores']['hechos']['valor']} hechos",
        )
    )
    dos_alcaldias = get("/resumen", alcaldia=["Cuauhtémoc", "Iztapalapa"])
    suma_dos = dos_alcaldias["indicadores"]["hechos"]["valor"]
    iztapalapa = get("/resumen", alcaldia="Iztapalapa")["indicadores"]["hechos"]["valor"]
    resultados.append(
        comprobar(
            "Dentro de una dimensión los valores se suman (OR)",
            suma_dos == una["indicadores"]["hechos"]["valor"] + iztapalapa,
            f"{suma_dos} = {una['indicadores']['hechos']['valor']} + {iztapalapa}",
        )
    )
    combinado = get("/resumen", alcaldia="Cuauhtémoc", tipo_evento="Choque")
    resultados.append(
        comprobar(
            "Entre dimensiones distintas se aplica AND",
            combinado["indicadores"]["hechos"]["valor"] <= una["indicadores"]["hechos"]["valor"] and combinado["indicadores"]["hechos"]["valor"] > 0,
            f"Choque en Cuauhtémoc: {combinado['indicadores']['hechos']['valor']}",
        )
    )
    fallecidos = get("/resumen", consecuencia="con_fallecimientos")
    resultados.append(
        comprobar(
            "El filtro por consecuencia cuadra con el indicador",
            fallecidos["indicadores"]["hechos"]["valor"] == ESPERADO["con_fallecimientos"],
            str(fallecidos["indicadores"]["hechos"]["valor"]),
        )
    )

    comparacion = get("/comparar", comparar=["Cuauhtémoc", "Iztapalapa"])
    resultados.append(
        comprobar(
            "La comparación devuelve dos alcaldías con la misma escala",
            len(comparacion["alcaldias"]) == 2
            and all("composicion_tipo_evento" in fila for fila in comparacion["alcaldias"]),
            " + ".join(fila["alcaldia"] for fila in comparacion["alcaldias"]),
        )
    )

    csv = get_texto("/exportar", alcaldia="Cuauhtémoc")
    lineas = [linea for linea in csv.splitlines() if linea and not linea.startswith("#")]
    filas_csv = len(lineas) - 1
    resultados.append(
        comprobar(
            "La exportación trae el mismo subconjunto que el resumen",
            filas_csv == una["indicadores"]["hechos"]["valor"],
            f"{filas_csv} filas exportadas",
        )
    )
    resultados.append(
        comprobar(
            "La exportación documenta fuente, periodo, filtros y versión",
            "Fuente:" in csv and "Versión de datos" in csv and "Filtros aplicados" in csv,
            "cabecera documentada",
        )
    )

    print(f"\n{sum(1 for resultado in resultados if resultado)}/{len(resultados)} comprobaciones correctas")
    return 0 if all(resultados) else 1


if __name__ == "__main__":
    raise SystemExit(main())
