"""Diagnóstico por capas de una cadena de conexión de MongoDB, sin exponer secretos.

Distingue explícitamente cinco capas, de modo que un fallo no se confunda con otro:

1. **configuración**: forma de la cadena, marcadores sin reemplazar, codificación de
   los caracteres especiales de la contraseña;
2. **DNS**: resolución de los registros ``SRV``/``TXT`` (o del host) del clúster;
3. **red / IP**: conexión TCP al puerto del servidor (una lista de accesos que no
   incluye la IP termina aquí, no en la autenticación);
4. **TLS**: negociación y verificación del certificado;
5. **autenticación / autorización**: ``ping`` con credenciales y permiso mínimo de
   lectura y escritura sobre la base.

Nunca devuelve la URI, el usuario ni la contraseña: solo longitudes, indicadores y
nombres de error.
"""

from __future__ import annotations

import re
import socket
import ssl
import time
from typing import Any
from urllib.parse import unquote

#: Códigos de MongoDB que corresponden a credenciales rechazadas.
CODIGOS_AUTENTICACION = {18, 8000}
#: Códigos que corresponden a permisos insuficientes sobre la base o el recurso.
CODIGOS_AUTORIZACION = {13}


def _partes(uri: str) -> dict[str, Any]:
    coincidencia = re.match(r"^(mongodb(?:\+srv)?://)([^@]*)@(.+)$", uri)
    if not coincidencia:
        return {"forma": False}
    esquema, credenciales, resto = coincidencia.groups()
    usuario, _, contrasena = credenciales.partition(":")
    host = resto.split("/")[0]
    consulta = resto.split("?", 1)[1] if "?" in resto else ""
    return {
        "forma": True,
        "esquema": esquema.rstrip("://"),
        "srv": esquema.startswith("mongodb+srv"),
        "usuario_largo": len(usuario),
        "contrasena_largo": len(contrasena),
        "marcadores_usuario": "<" in usuario or ">" in usuario,
        "marcadores_contrasena": "<" in contrasena or ">" in contrasena,
        "contrasena_codificada": bool(re.search(r"%[0-9A-Fa-f]{2}", contrasena)),
        "especiales": sorted({c for c in contrasena if c in ":/?#[]@!$&'()*+,;= %<>\""}),
        "host": host.split(",")[0].split(":")[0],
        "hosts": host.count(",") + 1,
        "parametros": sorted({p.split("=")[0] for p in consulta.split("&") if "=" in p}),
        "auth_source": "authSource" in consulta,
        "contrasena_decodificada_igual": unquote(contrasena) == contrasena,
    }


def diagnosticar(uri: str, db_name: str, *, comprobar_permisos: bool = True) -> dict[str, Any]:
    """Devuelve el estado de cada capa, con el primer fallo identificado."""
    partes = _partes(uri)
    if not partes.get("forma"):
        return {
            "estado": "configuracion",
            "mensaje": "La cadena no tiene la forma mongodb://usuario:contraseña@host/base.",
            "capas": {"configuracion": {"ok": False}},
        }

    capas: dict[str, Any] = {}

    # 1) Configuración ---------------------------------------------------------
    problemas: list[str] = []
    if partes["marcadores_usuario"] or partes["marcadores_contrasena"]:
        problemas.append(
            "el usuario o la contraseña conservan los marcadores «<» y «>» de la plantilla"
        )
    marcadores = partes["marcadores_usuario"] or partes["marcadores_contrasena"]
    if partes["especiales"] and not partes["contrasena_codificada"] and not marcadores:
        problemas.append(
            "la contraseña tiene caracteres especiales sin codificar (%40, %3A, %23…)"
        )
    if not partes["usuario_largo"] or not partes["contrasena_largo"]:
        problemas.append("falta el usuario o la contraseña")
    capas["configuracion"] = {
        "ok": not problemas,
        "esquema": partes["esquema"],
        "hosts": partes["hosts"],
        "auth_source_explicito": partes["auth_source"],
        "contrasena_codificada": partes["contrasena_codificada"],
        "problemas": problemas,
    }
    if problemas:
        return {
            "estado": "configuracion",
            "mensaje": problemas[0],
            "capas": capas,
        }

    # 2) DNS ------------------------------------------------------------------
    destinos: list[tuple[str, int]] = []
    try:
        import dns.resolver

        if partes["srv"]:
            respuesta = dns.resolver.resolve(f"_mongodb._tcp.{partes['host']}", "SRV", lifetime=8)
            destinos = sorted({(str(r.target).rstrip("."), int(r.port)) for r in respuesta})
            try:
                dns.resolver.resolve(partes["host"], "TXT", lifetime=8)
            except Exception:  # noqa: BLE001 - los opcionales no bloquean
                pass
        else:
            puerto = int(partes["host"].split(":")[1]) if ":" in partes["host"] else 27017
            destinos = [(partes["host"].split(":")[0], puerto)]
        capas["dns"] = {"ok": bool(destinos), "destinos": len(destinos)}
    except Exception as error:  # noqa: BLE001
        capas["dns"] = {"ok": False, "error": type(error).__name__}
        return {"estado": "dns", "mensaje": f"No se resolvió el clúster ({type(error).__name__}).", "capas": capas}

    # 3) Red / IP -------------------------------------------------------------
    alcanzables = 0
    for destino, puerto in destinos:
        try:
            with socket.create_connection((destino, puerto), timeout=8):
                alcanzables += 1
        except OSError:
            continue
    capas["red"] = {"ok": alcanzables > 0, "alcanzables": alcanzables, "total": len(destinos)}
    if not alcanzables:
        return {
            "estado": "red",
            "mensaje": (
                "El puerto 27017 no acepta conexiones desde este equipo: revisa la lista "
                "de accesos (IP) del clúster o el cortafuegos."
            ),
            "capas": capas,
        }

    # 4) TLS ------------------------------------------------------------------
    try:
        contexto = ssl.create_default_context()
        destino, puerto = destinos[0]
        inicio = time.perf_counter()
        with socket.create_connection((destino, puerto), timeout=8) as crudo:
            with contexto.wrap_socket(crudo, server_hostname=destino) as tls:
                version = tls.version()
        capas["tls"] = {"ok": True, "version": version, "ms": round((time.perf_counter() - inicio) * 1000)}
    except Exception as error:  # noqa: BLE001
        capas["tls"] = {"ok": False, "error": type(error).__name__}
        return {"estado": "tls", "mensaje": f"Falló la negociación TLS ({type(error).__name__}).", "capas": capas}

    # 5) Autenticación y permisos --------------------------------------------
    from pymongo import MongoClient
    from pymongo.errors import OperationFailure, PyMongoError, ServerSelectionTimeoutError

    try:
        cliente = MongoClient(
            uri, serverSelectionTimeoutMS=8_000, connectTimeoutMS=8_000, appname="diagnostico-mongo"
        )
        inicio = time.perf_counter()
        cliente.admin.command("ping")
        capas["autenticacion"] = {"ok": True, "ms": round((time.perf_counter() - inicio) * 1000)}
    except OperationFailure as error:
        codigo = getattr(error, "code", None)
        nombre = (getattr(error, "details", None) or {}).get("codeName")
        tipo = "autorizacion" if codigo in CODIGOS_AUTORIZACION else "autenticacion"
        capas["autenticacion"] = {"ok": False, "codigo": codigo, "nombre": nombre}
        return {
            "estado": tipo,
            "mensaje": (
                "Las credenciales fueron rechazadas (bad auth): revisa el usuario y la "
                "contraseña del usuario de base de datos del clúster."
                if tipo == "autenticacion"
                else "El usuario existe pero no tiene permisos sobre esta base."
            ),
            "capas": capas,
        }
    except ServerSelectionTimeoutError as error:
        capas["autenticacion"] = {"ok": False, "error": type(error).__name__}
        return {
            "estado": "red",
            "mensaje": "No se pudo seleccionar un servidor del clúster (red o lista de accesos).",
            "capas": capas,
        }
    except PyMongoError as error:
        capas["autenticacion"] = {"ok": False, "error": type(error).__name__}
        return {"estado": "desconocido", "mensaje": str(error)[:160], "capas": capas}

    if not comprobar_permisos:
        cliente.close()
        return {"estado": "ok", "mensaje": "Conexión y autenticación correctas.", "capas": capas}

    try:
        base = cliente[db_name]
        colecciones = base.list_collection_names()
        marca = "_diagnostico_marketing"
        base[marca].insert_one({"ok": 1})
        leidos = base[marca].count_documents({})
        # La colección de prueba no se deja en el clúster: el M0 es compartido y
        # cada colección vacía ocupa su espacio de nombres.
        base.drop_collection(marca)
        capas["permisos"] = {"ok": True, "lectura": True, "escritura": True, "colecciones": len(colecciones)}
        return {
            "estado": "ok",
            "mensaje": "Conexión, autenticación y permisos de lectura y escritura correctos.",
            "capas": capas,
        }
    except OperationFailure as error:
        codigo = getattr(error, "code", None)
        capas["permisos"] = {"ok": False, "codigo": codigo}
        return {
            "estado": "autorizacion" if codigo in CODIGOS_AUTORIZACION else "autenticacion",
            "mensaje": "El usuario se autentica pero no puede leer o escribir en la base.",
            "capas": capas,
        }
    finally:
        cliente.close()
