"""Sembrador de una versión de DEMOSTRACIÓN para verificar la interfaz.

Este script **no** forma parte de la aplicación y **no** usa datos reales: genera un
conjunto pequeño y coherente de eventos ficticios con el único fin de comprobar que
las cuatro vistas, el embudo, la matriz de cohortes, la tabla y la exportación se
dibujan y se comportan como se espera cuando hay datos publicados.

Reglas de seguridad:

* solo escribe en una base cuyo nombre termine en ``_demo`` o ``_test``;
* publica con el mismo código de producción (``ingesta.sincronizar``), así que la
  validación, las reglas del embudo y las cohortes se ejecutan de verdad;
* la versión publicada queda marcada con origen ``demostracion`` para que no se
  confunda con una ingesta real.

Uso:
    set MONGODB_DB=hugo_portfolio_demo
    python -m tests.sembrar_demo
"""

from __future__ import annotations

import os
import sys

from app.config import get_settings
from app.db.mongo import close_database, get_database, init_database
from app.marketing import esquema
from app.marketing.ingesta import sincronizar

#: Canales, dispositivos y países del conjunto de demostración.
CANALES = [
    ("google", "organic"),
    ("facebook", "cpc"),
    ("direct", ""),
]
DISPOSITIVOS = ["desktop", "mobile"]
PAISES = ["United States", "India", "United Kingdom"]

#: Cuatro semanas completas de observación (lunes a domingo).
SEMANAS = [
    ["2020-11-02", "2020-11-03", "2020-11-04", "2020-11-05", "2020-11-06", "2020-11-07", "2020-11-08"],
    ["2020-11-09", "2020-11-10", "2020-11-11", "2020-11-12", "2020-11-13", "2020-11-14", "2020-11-15"],
    ["2020-11-16", "2020-11-17", "2020-11-18", "2020-11-19", "2020-11-20", "2020-11-21", "2020-11-22"],
    ["2020-11-23", "2020-11-24", "2020-11-25", "2020-11-26", "2020-11-27", "2020-11-28", "2020-11-29"],
]
DIAS = [dia for semana in SEMANAS for dia in semana]

#: Conversión por canal (solo para que las vistas tengan diferencias visibles).
CONVERSION = {
    "google / organic": 0.14,
    "facebook / cpc": 0.06,
    "direct / Sin información": 0.22,
}

ARTICULOS = [
    ("Google Sticker", "Accessories", 3.5),
    ("Google Tote", "Bags", 18.0),
    ("Google Hoodie", "Apparel", 55.0),
    ("Google Bottle", "Accessories", 22.0),
    ("Google Pen", "Stationery", 6.0),
]


def canal(fuente: str, medio: str) -> str:
    return f"{fuente} / {esquema.normalizar(medio)}"


class FuenteDemo:
    """Fuente que entrega filas ya agregadas del conjunto de demostración."""

    nombre = "demostracion"

    def __init__(self) -> None:
        self.usuarios = self._usuarios()
        self.resumen, self.usuarios_filas, self.embudo = self._agregados()
        self.cohortes = self._cohortes()
        self.categorias = self._categorias()
        self.eventos = self._eventos()

    # ------------------------------------------------------------- construcción
    def _usuarios(self) -> list[dict]:
        """Usuarios con inicio escalonado por semana y actividad decreciente."""
        usuarios = []
        for i in range(90):
            fuente, medio = CANALES[i % len(CANALES)]
            inicio = i % len(SEMANAS)
            dias: list[str] = []
            for semana in range(inicio, len(SEMANAS)):
                # Retención aproximada del 60 %: la actividad decae con el tiempo.
                if (i + semana * 2) % 5 == 0:
                    continue
                dias.append(SEMANAS[semana][(i + semana) % 7])
                if (i + semana) % 3 == 0:
                    dias.append(SEMANAS[semana][(i + semana + 3) % 7])
            if not dias:
                # Todo usuario tiene al menos una actividad observada.
                dias = [SEMANAS[inicio][0]]
            usuarios.append(
                {
                    "id": f"demo-{i:03d}",
                    "fuente": fuente,
                    "medio": medio,
                    "canal": canal(fuente, medio),
                    "dispositivo": DISPOSITIVOS[i % len(DISPOSITIVOS)],
                    "pais": PAISES[(i // 2) % len(PAISES)],
                    "dias": sorted(set(dias)),
                }
            )
        return usuarios

    def _agregados(self) -> tuple[list[dict], list[dict], list[dict]]:
        resumen: dict[tuple, dict] = {}
        usuarios_filas: list[dict] = []
        embudo: dict[tuple, dict] = {}
        sesiones_por_usuario: dict[str, int] = {}

        for indice, usuario in enumerate(self.usuarios):
            for dia in usuario["dias"]:
                clave = (dia, usuario["dispositivo"], usuario["pais"], usuario["fuente"], usuario["medio"])
                fila = resumen.setdefault(
                    clave,
                    {
                        "fecha": dia,
                        "dispositivo": usuario["dispositivo"],
                        "pais": usuario["pais"],
                        "region": "demo",
                        "fuente": usuario["fuente"],
                        "medio": usuario["medio"],
                        "eventos": 0,
                        "usuarios": 0,
                        "sesiones": 0,
                        "eventos_sin_sesion": 0,
                        "compras": 0,
                        "compradores": 0,
                        "ingresos_usd": 0.0,
                        "compras_sin_importe": 0,
                    },
                )
                sesiones = 1 + (indice + len(dia)) % 3
                sesiones_por_usuario[usuario["id"]] = sesiones_por_usuario.get(usuario["id"], 0) + sesiones
                fila["usuarios"] += 1
                fila["sesiones"] += sesiones
                fila["eventos"] += sesiones * 3

                compro = (indice * 7 + len(dia)) % 100 < int(CONVERSION[usuario["canal"]] * 100)
                if compro:
                    importe = round(35 + (indice % 5) * 12.5, 2)
                    fila["compras"] += 1
                    fila["compradores"] += 1
                    fila["ingresos_usd"] = round(fila["ingresos_usd"] + importe, 2)

                usuarios_filas.append(
                    {
                        "fecha": dia,
                        "dispositivo": usuario["dispositivo"],
                        "pais": usuario["pais"],
                        "region": "demo",
                        "fuente": usuario["fuente"],
                        "medio": usuario["medio"],
                        "user_pseudo_id": usuario["id"],
                        "compro": 1 if compro else 0,
                    }
                )

                clave_embudo = (dia, usuario["dispositivo"], usuario["pais"], usuario["fuente"], usuario["medio"])
                acumulado = embudo.setdefault(clave_embudo, {1: 0, 2: 0, 3: 0, 4: 0})
                acumulado[1] += sesiones
                acumulado[2] += max(1, round(sesiones * 0.62))
                acumulado[3] += max(1, round(sesiones * 0.31))
                acumulado[4] += 1 if compro else 0

        filas_embudo = [
            {
                "fecha": clave[0],
                "dispositivo": clave[1],
                "pais": clave[2],
                "fuente": clave[3],
                "medio": clave[4],
                "paso": paso,
                "sesiones": sesiones,
            }
            for clave, pasos in embudo.items()
            for paso, sesiones in pasos.items()
        ]
        return list(resumen.values()), usuarios_filas, filas_embudo

    def _cohortes(self) -> list[dict]:
        from datetime import date

        semanas = sorted({_lunes(dia) for dia in DIAS})
        filas: dict[tuple, dict] = {}
        for usuario in self.usuarios:
            primera = min(usuario["dias"])
            cohorte = _lunes(primera)
            activos = {_lunes(dia) for dia in usuario["dias"]}
            for semana in semanas:
                if semana < cohorte:
                    continue
                indice = (date.fromisoformat(semana) - date.fromisoformat(cohorte)).days // 7
                for dimension, valor in (
                    ("canal", usuario["canal"]),
                    ("dispositivo", usuario["dispositivo"]),
                    ("pais", usuario["pais"]),
                ):
                    clave = (cohorte, indice, dimension, valor)
                    fila = filas.setdefault(
                        clave, {"cohorte": cohorte, "indice": indice, "dimension": dimension, "valor": valor,
                                "usuarios": 0, "compradores": 0}
                    )
                    if semana in activos:
                        fila["usuarios"] += 1
                        if (len(usuario["id"]) + indice) % 5 == 0:
                            fila["compradores"] += 1
        return list(filas.values())

    def _categorias(self) -> list[dict]:
        filas = []
        for indice, usuario in enumerate(self.usuarios):
            articulo, categoria, _precio = ARTICULOS[indice % len(ARTICULOS)]
            filas.append(
                {
                    "categoria": categoria,
                    "dispositivo": usuario["dispositivo"],
                    "pais": usuario["pais"],
                    "fuente": usuario["fuente"],
                    "medio": usuario["medio"],
                    "eventos": len(usuario["dias"]) * 2,
                    "compras": len(usuario["dias"]),
                }
            )
        return filas

    def _eventos(self) -> list[dict]:
        eventos = []
        nombres = ["view_item", "add_to_cart", "begin_checkout", "purchase"]
        for indice, usuario in enumerate(self.usuarios):
            for posicion, dia in enumerate(usuario["dias"][:2]):
                nombre = nombres[(indice + posicion) % len(nombres)]
                articulo, categoria, precio = ARTICULOS[indice % len(ARTICULOS)]
                eventos.append(
                    {
                        "fecha": dia,
                        "hora": 8 + (indice + posicion) % 12,
                        "event_name": nombre,
                        "dispositivo": usuario["dispositivo"],
                        "pais": usuario["pais"],
                        "fuente": usuario["fuente"],
                        "medio": usuario["medio"],
                        "articulo": articulo,
                        "categoria": categoria,
                        "ingresos_usd": precio if nombre == "purchase" else 0.0,
                    }
                )
        return eventos

    # ----------------------------------------------------------------- consultas
    def ejecutar(self, sql: str, max_filas: int = 200_000) -> list[dict]:
        if "INFORMATION_SCHEMA" in sql:
            return [
                {"column_name": columna, "data_type": tipo}
                for columna, tipo in {
                    **esquema.REQUERIDAS_BASE,
                    **esquema.REQUERIDAS_COMPRA,
                    "items.item_category": "STRING",
                }.items()
            ]
        if "SELECT 1" in sql:
            return [{"ok": 1}]
        if "ROW_NUMBER()" in sql:
            return self.eventos
        if "MAX(IF(event_name = 'purchase', 1, 0)) AS compro" in sql:
            return self.usuarios_filas
        if "AS paso" in sql:
            return self.embudo
        if "'canal' AS dimension" in sql:
            return self.cohortes
        if "item.item_category" in sql:
            return self.categorias
        return self.resumen


def _lunes(dia: str) -> str:
    from datetime import date, timedelta

    fecha = date.fromisoformat(dia)
    return (fecha - timedelta(days=fecha.weekday())).isoformat()


def main() -> int:
    settings = get_settings()
    if not (settings.mongodb_db.endswith("_demo") or settings.mongodb_db.endswith("_test")):
        print(
            "Por seguridad, este sembrador solo escribe en bases cuyo nombre termine en "
            f"'_demo' o '_test' (recibido: {settings.mongodb_db!r}).",
            file=sys.stderr,
        )
        return 2

    print(f"Base de demostración: {settings.mongodb_db}")
    init_database(settings)
    try:
        base = get_database()
        resultado = sincronizar(base.db, fuente=FuenteDemo(), forzar=True)
        print(f"exito={resultado.exito} version={resultado.version} publicada={resultado.publicada}")
        print(f"motivo: {resultado.motivo}")
        if resultado.cifras:
            print(f"cifras: {resultado.cifras}")
        return 0 if resultado.exito else 1
    finally:
        close_database()


if __name__ == "__main__":
    os.environ.setdefault("LOG_LEVEL", "warning")
    raise SystemExit(main())
