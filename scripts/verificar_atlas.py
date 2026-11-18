"""Comprobación de la conexión del tablero a MongoDB (Atlas), por capas, y consumo.

Distingue configuración, DNS, red/IP, TLS y autenticación/autorización, informa el
espacio que ocupa cada base del clúster (el M0 es compartido y tiene 512 MB) y no
imprime nunca la URI ni las credenciales.

Uso:
    python -m scripts.verificar_atlas            # URI efectiva del tablero + consumo
    python -m scripts.verificar_atlas --sin-permisos   # solo hasta autenticación
    python -m scripts.verificar_atlas --sin-consumo    # sin recorrer las bases
"""

from __future__ import annotations

import argparse
import json
import re

from pymongo import MongoClient

from app.config import get_settings
from app.db.diagnostico_mongo import diagnosticar

SALIDA_OK = 0
SALIDA_PENDIENTE = 3

#: Límite del plan gratuito (M0). Es del clúster entero, no de una base.
LIMITE_M0_MIB = 512


def _uri_efectiva() -> tuple[str, str]:
    ajustes = get_settings()
    uri = ajustes.marketing_mongodb_uri or ajustes.mongodb_uri
    base = ajustes.marketing_mongodb_db or ajustes.mongodb_db
    origen = "MARKETING_MONGODB_URI" if ajustes.marketing_mongodb_uri else "MONGODB_URI"
    return uri, base, origen


def informar_consumo(uri: str, base_propia: str) -> None:
    """Espacio por base del clúster, sin escribir nada y sin revelar credenciales.

    Solo se leen estadísticas: no se consulta ni se modifica ninguna colección de
    los otros proyectos que comparten el clúster.
    """
    cliente = MongoClient(uri, serverSelectionTimeoutMS=10_000, compressors="zlib")
    try:
        filas = []
        for nombre in cliente.list_database_names():
            if nombre in ("admin", "local", "config"):
                continue
            try:
                st = cliente[nombre].command("dbStats", scale=1)
            except Exception:  # sin permiso sobre esa base: se omite, no se falla
                filas.append((nombre, None, None))
                continue
            filas.append(
                (
                    nombre,
                    (st.get("dataSize", 0) + st.get("indexSize", 0)) / 1048576,
                    st.get("storageSize", 0) / 1048576,
                )
            )
        filas.sort(key=lambda fila: -(fila[1] or 0))
        print(f"\nConsumo del clúster (límite del plan gratuito: {LIMITE_M0_MIB} MiB)")
        print(f"  {'base':24s} {'lógico MiB':>11s} {'disco MiB':>10s}")
        total = 0.0
        for nombre, logico, disco in filas:
            if logico is None:
                print(f"  {nombre:24s} {'sin acceso':>11s}")
                continue
            total += logico
            marca = "  (este portafolio)" if nombre == base_propia else ""
            print(f"  {nombre:24s} {logico:11.2f} {disco:10.2f}{marca}")
        print(f"  {'TOTAL':24s} {total:11.2f}")
        print(
            f"  Reserva frente a {LIMITE_M0_MIB} MiB: {LIMITE_M0_MIB - total:.1f} MiB "
            "(el resto de bases es de otros proyectos: no se toca)"
        )
    finally:
        cliente.close()


def main() -> int:
    argumentos = argparse.ArgumentParser(description="Verificación de MongoDB para el tablero")
    argumentos.add_argument(
        "--sin-permisos", action="store_true", help="No comprueba lectura/escritura."
    )
    argumentos.add_argument(
        "--sin-consumo", action="store_true", help="No recorre las bases del clúster."
    )
    opciones = argumentos.parse_args()

    uri, base, origen = _uri_efectiva()
    ajustes = get_settings()
    resto = uri.split("@", 1)[1] if "@" in uri else uri
    host = resto.split("/")[0].split(",")[0].split("?")[0]
    print(f"Variable efectiva: {origen}")
    print(f"Esquema: {uri.split(':', 1)[0]} | host: {host} | base: {base}")
    print(f"TLS: {'mongodb+srv (obligatorio)' if uri.startswith('mongodb+srv') else 'no'}")
    print(f"URI propia de marketing: {'sí' if ajustes.marketing_mongodb_uri else 'no'}")

    resultado = diagnosticar(uri, base, comprobar_permisos=not opciones.sin_permisos)
    print(f"\nEstado: {resultado['estado'].upper()} — {resultado['mensaje']}")

    capas = resultado.get("capas") or {}
    for nombre in ("configuracion", "dns", "red", "tls", "autenticacion", "permisos"):
        if nombre not in capas:
            continue
        capa = capas[nombre]
        marca = "OK  " if capa.get("ok") else "FALLA"
        detalle = {k: v for k, v in capa.items() if k != "ok"}
        if nombre == "configuracion" and detalle.get("problemas"):
            print(f"  {marca} {nombre}: " + "; ".join(detalle["problemas"]))
        elif detalle:
            print(f"  {marca} {nombre}: {json.dumps(detalle, ensure_ascii=False)}")
        else:
            print(f"  {marca} {nombre}")

    if resultado["estado"] == "configuracion":
        print(
            "\nEdita backend/.env o .env (raíz) y sustituye los marcadores <…> de la URI por "
            "el usuario y la contraseña reales del clúster. No los envíes por el chat.\n"
            "Si la contraseña tiene caracteres especiales, codifícalos "
            "(@ -> %40, : -> %3A, / -> %2F, # -> %23, % -> %25)."
        )
        m = re.match(r"^mongodb(?:\+srv)?://([^@]*)@", uri)
        if m:
            usuario, _, contrasena = m.group(1).partition(":")
            if "<" in usuario or ">" in usuario or "<" in contrasena or ">" in contrasena:
                print("  (detectados marcadores «<» y «>» en usuario o contraseña)")
        return SALIDA_PENDIENTE

    if resultado["estado"] != "ok":
        return SALIDA_PENDIENTE
    if not opciones.sin_consumo:
        informar_consumo(uri, base)
    return SALIDA_OK


if __name__ == "__main__":
    raise SystemExit(main())
