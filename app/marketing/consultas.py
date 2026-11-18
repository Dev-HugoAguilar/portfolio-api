"""Consultas SQL contra el conjunto público de GA4 en BigQuery.

Todas las consultas se ejecutan **una vez por ingesta** sobre el rango completo
del conjunto; los filtros del tablero se aplican después, en MongoDB, sobre los
agregados que produce este archivo. Así el tablero nunca consulta BigQuery desde
el navegador y las cifras de tarjetas, gráficas, tabla y exportación salen del
mismo conjunto.

Reglas que las consultas hacen cumplir:

* **Usuario** = `user_pseudo_id` distinto y no vacío.
* **Sesión** = `user_pseudo_id` + `ga_session_id` (parámetro de evento
  documentado). Los eventos sin `ga_session_id` se cuentan aparte y se informan.
* **Compra** = evento `purchase` con `ecommerce.transaction_id` (clave
  `usuario|transacción`; si falta el identificador se usa `usuario|ts` y se
  informa). Así un evento repetido no cuenta dos veces.
* **Ingresos** = `ecommerce.purchase_revenue_in_usd` sumado **solo** sobre las
  compras ya deduplicadas.
* **Embudo** = sesiones, con pasos **en orden estricto** y una **ventana de 24 h**
  desde el primer paso.
* **Cohortes** = semana ISO de la **primera actividad observada** dentro del
  conjunto; las semanas sin ventana suficiente se publican como `null`.
"""

from __future__ import annotations

from app.marketing import esquema

TABLAS = esquema.TABLAS

#: Dimnensiones comunes a todas las consultas (se normalizan en Python también).
DIMS = (
    "dispositivo",
    "pais",
    "region",
    "fuente",
    "medio",
)

#: Expresión de la clave de sesión (NULL si el parámetro no existe).
CLAVE_SESION = (
    "IF(user_pseudo_id IS NULL OR user_pseudo_id = '' OR session_id IS NULL, NULL, "
    "CONCAT(user_pseudo_id, '|', CAST(session_id AS STRING)))"
)

#: Expresión de la clave de compra deduplicada.
CLAVE_COMPRA = """
  IF(
    user_pseudo_id IS NULL OR user_pseudo_id = '',
    NULL,
    IF(
      transaction_id IS NOT NULL AND transaction_id != '',
      CONCAT(user_pseudo_id, '|', transaction_id),
      CONCAT(user_pseudo_id, '|ts|', CAST(event_timestamp AS STRING))
    )
  )
"""


#: Muestreo determinista por usuario (unidad de muestreo: ``user_pseudo_id``).
#: El mismo hash con la misma semilla selecciona siempre los mismos usuarios, así
#: que la muestra es reproducible y todas las entidades comparten el mismo
#: subconjunto (historias completas, sin mezclar agregados de la población).
MUESTRA: dict[str, Any] = {"semilla": "ga4-muestra-2026", "porcentaje": 0.0}


def fijar_muestra(semilla: str, porcentaje: float) -> dict[str, Any]:
    """Fija la semilla y el porcentaje de la muestra (0 = conjunto completo)."""
    MUESTRA["semilla"] = str(semilla or "ga4-muestra-2026")
    MUESTRA["porcentaje"] = max(0.0, min(100.0, float(porcentaje or 0)))
    return dict(MUESTRA)


def muestra() -> dict[str, Any]:
    """Configuración vigente de la muestra, para el manifiesto."""
    return dict(MUESTRA)


def _predicado_muestra() -> str:
    """Condición SQL que selecciona los usuarios de la muestra."""
    porcentaje = float(MUESTRA["porcentaje"])
    if porcentaje <= 0 or porcentaje >= 100:
        return ""
    # Base 10 000: permite fracciones de 0,01 % con un entero exacto y estable.
    umbral = max(1, round(porcentaje * 100))
    semilla = str(MUESTRA["semilla"]).replace("'", "''")
    return (
        "  AND MOD(ABS(FARM_FINGERPRINT(CONCAT('"
        + semilla
        + "|', IFNULL(user_pseudo_id, '')))), 10000) < "
        + str(umbral)
        + "\n"
    )


def _normalizar(columna: str) -> str:
    return f"COALESCE(NULLIF(TRIM({columna}), ''), '{esquema.SIN_INFORMACION}')"


def base_cte(desde: str, hasta: str) -> str:
    """CTE con las columnas normalizadas que usan todas las consultas.

    Las dimensiones de persona (dispositivo, país, región, fuente y medio) se toman
    de la **primera actividad observada** de cada usuario en el periodo. Así cada
    persona pertenece a una sola combinación y las cifras por dimensión suman el
    total; es la misma regla de atribución que usan las cohortes y el tablero.
    """
    return f"""
WITH cruda AS (
  SELECT
    PARSE_DATE('%Y%m%d', event_date) AS fecha,
    event_timestamp,
    event_name,
    user_pseudo_id,
    device.category AS dispositivo,
    geo.country AS pais,
    geo.region AS region,
    traffic_source.source AS fuente,
    traffic_source.medium AS medio,
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = '{esquema.PARAMETRO_SESION}') AS session_id,
    ecommerce.transaction_id AS transaction_id,
    ecommerce.purchase_revenue_in_usd AS ingresos_usd,
    items
  FROM `{TABLAS}`
  WHERE _TABLE_SUFFIX BETWEEN '{desde}' AND '{hasta}'
{_predicado_muestra()})
, atribucion AS (
  SELECT
    user_pseudo_id,
    ARRAY_AGG(
      STRUCT(dispositivo, pais, region, fuente, medio)
      ORDER BY event_timestamp
      LIMIT 1
    )[OFFSET(0)] AS inicio
  FROM cruda
  WHERE user_pseudo_id IS NOT NULL AND user_pseudo_id != ''
  GROUP BY 1
)
, base AS (
  SELECT
    c.fecha,
    c.event_timestamp,
    c.event_name,
    c.user_pseudo_id,
    {_normalizar('p.inicio.dispositivo')} AS dispositivo,
    {_normalizar('p.inicio.pais')} AS pais,
    {_normalizar('p.inicio.region')} AS region,
    {_normalizar('p.inicio.fuente')} AS fuente,
    {_normalizar('p.inicio.medio')} AS medio,
    c.session_id,
    c.transaction_id,
    c.ingresos_usd,
    c.items
  FROM cruda c
  LEFT JOIN atribucion p USING (user_pseudo_id)
)
"""


def cte_sesiones(desde: str, hasta: str) -> str:
    """Sesiones identificadas, con su primera y última marca de tiempo."""
    return (
        base_cte(desde, hasta)
        + f"""
, con_sesion AS (
  SELECT
    fecha, dispositivo, pais, region, fuente, medio,
    {CLAVE_SESION} AS clave_sesion,
    event_timestamp,
    event_name,
    user_pseudo_id,
    ingresos_usd,
    {CLAVE_COMPRA} AS clave_compra
  FROM base
)
, sesiones AS (
  SELECT
    fecha, dispositivo, pais, region, fuente, medio,
    clave_sesion,
    user_pseudo_id,
    COUNT(*) AS eventos,
    MIN(event_timestamp) AS inicio,
    MAX(event_timestamp) AS fin
  FROM con_sesion
  WHERE clave_sesion IS NOT NULL
  GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
)
"""
    )


def sql_usuarios(desde: str, hasta: str) -> str:
    """Una fila por usuario, día y combinación de dimensiones.

    Permite contar **usuarios distintos** con exactitud para cualquier filtro y
    cualquier rango de fechas (sumar los usuarios de cada día contaría dos veces a
    quien vuelve). La ingesta sustituye `user_pseudo_id` por un hash con sal: el
    identificador no se guarda ni se publica.
    """
    return (
        base_cte(desde, hasta)
        + """
SELECT
  fecha, dispositivo, pais, region, fuente, medio, user_pseudo_id,
  MAX(IF(event_name = 'purchase', 1, 0)) AS compro
FROM base
WHERE user_pseudo_id IS NOT NULL AND user_pseudo_id != ''
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY fecha
"""
    )


def sql_esquema() -> str:
    """Columnas reales de la tabla, para validar antes de calcular.

    Se consulta la vista de la **región** (``region-US``) porque el conjunto vive en
    otro proyecto; con la vista del dataset, BigQuery no resuelve las columnas para
    conjuntos públicos de otro proyecto.
    """
    return f"""
SELECT column_name, data_type
FROM `{esquema.PROYECTO}.{esquema.REGION}`.INFORMATION_SCHEMA.COLUMNS
WHERE table_schema = '{esquema.CONJUNTO}'
  AND table_name = 'events_20201101'
ORDER BY column_name
"""


def sql_resumen_dias(desde: str, hasta: str) -> str:
    """Una fila por día y combinación de dimensiones con actividad observada.

    Devuelve eventos, usuarios y sesiones. Las compras y los ingresos **no** se
    agregan aquí: se cuentan en ``sql_compras`` con deduplicación sobre todo el
    periodo, porque una compra que aparece en dos días se contaría dos veces al
    sumar los días.
    """
    return (
        base_cte(desde, hasta)
        + f"""
, con_claves AS (
  SELECT
    fecha, dispositivo, pais, region, fuente, medio,
    user_pseudo_id,
    {CLAVE_SESION} AS clave_sesion
  FROM base
)
SELECT
  fecha, dispositivo, pais, region, fuente, medio,
  COUNT(*) AS eventos,
  COUNT(DISTINCT IF(user_pseudo_id != '', user_pseudo_id, NULL)) AS usuarios,
  COUNT(DISTINCT clave_sesion) AS sesiones,
  COUNTIF(clave_sesion IS NULL) AS eventos_sin_sesion
FROM con_claves
GROUP BY 1, 2, 3, 4, 5, 6
ORDER BY fecha
"""
    )


def sql_compras(desde: str, hasta: str) -> str:
    """Una fila por compra **deduplicada sobre todo el periodo**.

    La clave es usuario + transacción (y usuario + marca de tiempo cuando el evento
    no trae ``transaction_id``). Se guarda en su propia colección para que compras,
    compradores e ingresos se cuenten siempre sin doble conteo, se filtren por
    cualquier rango y se comparen con la fuente con una consulta de control.
    """
    return (
        base_cte(desde, hasta)
        + f"""
, claves AS (
  SELECT
    {CLAVE_COMPRA} AS clave_compra,
    user_pseudo_id,
    fecha,
    ingresos_usd
  FROM base
  WHERE event_name = 'purchase'
)
, compras AS (
  SELECT
    clave_compra,
    ANY_VALUE(user_pseudo_id) AS user_pseudo_id,
    MIN(fecha) AS fecha,
    MAX(ingresos_usd) AS ingresos_usd
  FROM claves
  WHERE clave_compra IS NOT NULL
  GROUP BY clave_compra
)
SELECT
  c.fecha,
  c.user_pseudo_id,
  c.ingresos_usd,
  b.dispositivo,
  b.pais,
  b.region,
  b.fuente,
  b.medio
FROM compras c
JOIN (SELECT DISTINCT user_pseudo_id, dispositivo, pais, region, fuente, medio FROM base) b
  USING (user_pseudo_id)
ORDER BY c.fecha
"""
    )


def sql_embudo(desde: str, hasta: str) -> str:
    """Sesiones que alcanzan cada paso del embudo, en orden y dentro de 24 h.

    La unidad es la **sesión**: se toma la primera vez que ocurre cada paso dentro
    de la sesión y se exige que cada paso sea posterior al anterior y que el
    último ocurra dentro de las 24 h siguientes al primero. Si no se cumple el
    orden o la ventana, la sesión no avanza de paso.
    """
    pasos = [paso["clave"] for paso in esquema.EMBUDO]
    horas = esquema.VENTANA_EMBUDO_HORAS
    return (
        cte_sesiones(desde, hasta)
        + f"""
, marcas AS (
  SELECT
    fecha, dispositivo, pais, region, fuente, medio, clave_sesion,
    MIN(IF(event_name = '{pasos[0]}', event_timestamp, NULL)) AS t1,
    MIN(IF(event_name = '{pasos[1]}', event_timestamp, NULL)) AS t2,
    MIN(IF(event_name = '{pasos[2]}', event_timestamp, NULL)) AS t3,
    MIN(IF(event_name = '{pasos[3]}', event_timestamp, NULL)) AS t4,
    -- Volumen real de cada paso: se publica junto a las sesiones para que se vea
    -- si el conjunto trae pocos eventos de un paso (esta muestra los trae).
    COUNTIF(event_name = '{pasos[0]}') AS e1,
    COUNTIF(event_name = '{pasos[1]}') AS e2,
    COUNTIF(event_name = '{pasos[2]}') AS e3,
    COUNTIF(event_name = '{pasos[3]}') AS e4
  FROM con_sesion
  WHERE clave_sesion IS NOT NULL
  GROUP BY 1, 2, 3, 4, 5, 6, 7
)
, validadas AS (
  SELECT
    fecha, dispositivo, pais, region, fuente, medio, clave_sesion,
    (t1 IS NOT NULL) AS paso1,
    (t1 IS NOT NULL AND t2 IS NOT NULL AND t2 >= t1
      AND TIMESTAMP_DIFF(TIMESTAMP_MICROS(t2), TIMESTAMP_MICROS(t1), HOUR) <= {horas}) AS paso2,
    (t1 IS NOT NULL AND t2 IS NOT NULL AND t3 IS NOT NULL AND t2 >= t1 AND t3 >= t2
      AND TIMESTAMP_DIFF(TIMESTAMP_MICROS(t3), TIMESTAMP_MICROS(t1), HOUR) <= {horas}) AS paso3,
    (t1 IS NOT NULL AND t2 IS NOT NULL AND t3 IS NOT NULL AND t4 IS NOT NULL
      AND t2 >= t1 AND t3 >= t2 AND t4 >= t3
      AND TIMESTAMP_DIFF(TIMESTAMP_MICROS(t4), TIMESTAMP_MICROS(t1), HOUR) <= {horas}) AS paso4,
    e1, e2, e3, e4
  FROM marcas
)
, pasos AS (
  SELECT fecha, dispositivo, pais, fuente, medio, 1 AS paso,
         paso1 AS alcanzado, IF(e1 > 0, 1, 0) AS con_evento, e1 AS eventos FROM validadas
  UNION ALL
  SELECT fecha, dispositivo, pais, fuente, medio, 2 AS paso,
         paso2, IF(e2 > 0, 1, 0), e2 FROM validadas
  UNION ALL
  SELECT fecha, dispositivo, pais, fuente, medio, 3 AS paso,
         paso3, IF(e3 > 0, 1, 0), e3 FROM validadas
  UNION ALL
  SELECT fecha, dispositivo, pais, fuente, medio, 4 AS paso,
         paso4, IF(e4 > 0, 1, 0), e4 FROM validadas
)
SELECT
  fecha, dispositivo, pais, fuente, medio, paso,
  -- Sesiones que alcanzan el paso con el orden y la ventana exigidos.
  COUNTIF(alcanzado) AS sesiones,
  -- Sesiones que registraron el evento del paso, sin exigir el orden.
  SUM(con_evento) AS sesiones_evento,
  SUM(eventos) AS eventos
FROM pasos
GROUP BY 1, 2, 3, 4, 5, 6
"""
    )


def sql_cohortes(desde: str, hasta: str) -> str:
    """Retención por semana ISO de la **primera actividad observada**.

    Devuelve, por cohorte, índice de semana y dimensión de definición
    («canal», «dispositivo» o «pais»), cuántos usuarios de la cohorte registraron
    actividad y cuántos compraron en esa semana. El tablero decide después qué
    celdas quedan fuera de la ventana de observación (no las rellena con cero).
    """
    return (
        base_cte(desde, hasta)
        + f"""
, primera AS (
  SELECT
    user_pseudo_id,
    ARRAY_AGG(
      STRUCT(
        fecha AS primer_dia,
        CONCAT(fuente, ' / ', medio) AS canal,
        dispositivo,
        pais
      )
      ORDER BY event_timestamp
      LIMIT 1
    )[OFFSET(0)] AS inicio
  FROM base
  WHERE user_pseudo_id IS NOT NULL AND user_pseudo_id != ''
  GROUP BY 1
)
, usuarios AS (
  SELECT
    user_pseudo_id,
    inicio.primer_dia AS primer_dia,
    inicio.canal AS canal,
    inicio.dispositivo AS dispositivo,
    inicio.pais AS pais,
    DATE_TRUNC(inicio.primer_dia, WEEK(MONDAY)) AS cohorte
  FROM primera
)
, actividad AS (
  SELECT
    u.user_pseudo_id,
    u.cohorte,
    u.canal,
    u.dispositivo,
    u.pais,
    DATE_TRUNC(b.fecha, WEEK(MONDAY)) AS semana,
    MAX(IF(b.event_name = 'purchase', 1, 0)) AS compro
  FROM base b
  JOIN usuarios u USING (user_pseudo_id)
  GROUP BY 1, 2, 3, 4, 5, 6
)
, con_indice AS (
  SELECT
    cohorte,
    DATE_DIFF(semana, cohorte, WEEK) AS indice,
    canal, dispositivo, pais,
    user_pseudo_id,
    compro
  FROM actividad
)
SELECT cohorte, indice, 'canal' AS dimension, canal AS valor,
       COUNT(DISTINCT user_pseudo_id) AS usuarios,
       COUNT(DISTINCT IF(compro = 1, user_pseudo_id, NULL)) AS compradores
FROM con_indice GROUP BY 1, 2, 3, 4
UNION ALL
SELECT cohorte, indice, 'dispositivo' AS dimension, dispositivo AS valor,
       COUNT(DISTINCT user_pseudo_id) AS usuarios,
       COUNT(DISTINCT IF(compro = 1, user_pseudo_id, NULL)) AS compradores
FROM con_indice GROUP BY 1, 2, 3, 4
UNION ALL
SELECT cohorte, indice, 'pais' AS dimension, pais AS valor,
       COUNT(DISTINCT user_pseudo_id) AS usuarios,
       COUNT(DISTINCT IF(compro = 1, user_pseudo_id, NULL)) AS compradores
FROM con_indice GROUP BY 1, 2, 3, 4
ORDER BY cohorte, indice
"""
    )


def sql_categorias(desde: str, hasta: str) -> str:
    """Categorías de producto con granularidades explícitas.

    Devuelve, por categoría y combinación de dimensiones:

    - ``compras``: compras **distintas** (clave usuario + transacción) que incluyen
      al menos un artículo de la categoría. Puede sumar más que las compras totales
      porque una compra con artículos de dos categorías aparece en las dos: es un
      conteo solapado y así se declara.
    - ``articulos``: líneas de artículo de la categoría.
    - ``unidades``: suma de ``item.quantity`` cuando la fuente lo trae.
    - ``ingresos_linea_usd``: suma de ``item.item_revenue_in_usd``. **No** se reparte
      el ingreso de la compra entre sus categorías.

    La fecha es la del primer evento observado de la compra, para que el filtro de
    periodo no cuente dos veces la misma transacción.
    """
    return (
        base_cte(desde, hasta)
        + f"""
, con_items AS (
  SELECT
    fecha, event_timestamp, event_name, user_pseudo_id,
    dispositivo, pais, fuente, medio,
    {CLAVE_COMPRA} AS clave_compra,
    COALESCE(NULLIF(TRIM(item.item_category), ''), '{esquema.SIN_INFORMACION}') AS categoria,
    item.item_name AS articulo,
    item.quantity AS unidades,
    item.item_revenue_in_usd AS ingresos_linea
  FROM base, UNNEST(items) AS item
)
, compras AS (
  SELECT
    clave_compra,
    MIN(fecha) AS fecha,
    ANY_VALUE(dispositivo) AS dispositivo,
    ANY_VALUE(pais) AS pais,
    ANY_VALUE(fuente) AS fuente,
    ANY_VALUE(medio) AS medio,
    COUNT(DISTINCT categoria) AS categorias
  FROM con_items
  WHERE event_name = 'purchase' AND clave_compra IS NOT NULL
  GROUP BY clave_compra
)
, compras_categoria AS (
  SELECT DISTINCT
    c.clave_compra, c.fecha, c.dispositivo, c.pais, c.fuente, c.medio, i.categoria
  FROM con_items i
  JOIN compras c USING (clave_compra)
  WHERE i.event_name = 'purchase'
)
, por_categoria AS (
  SELECT
    fecha, dispositivo, pais, fuente, medio, categoria,
    COUNT(*) AS compras
  FROM compras_categoria
  GROUP BY 1, 2, 3, 4, 5, 6
)
, lineas AS (
  SELECT
    fecha, dispositivo, pais, fuente, medio, categoria,
    COUNT(*) AS articulos,
    SUM(IF(unidades IS NULL, 0, unidades)) AS unidades,
    SUM(IF(ingresos_linea IS NULL, 0, ingresos_linea)) AS ingresos_linea_usd
  FROM con_items
  WHERE event_name = 'purchase'
  GROUP BY 1, 2, 3, 4, 5, 6
)
, eventos AS (
  SELECT
    fecha, dispositivo, pais, fuente, medio, categoria,
    COUNT(*) AS eventos
  FROM con_items
  GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT
  e.fecha, e.dispositivo, e.pais, e.fuente, e.medio, e.categoria,
  e.eventos,
  IFNULL(c.compras, 0) AS compras,
  IFNULL(l.articulos, 0) AS articulos,
  IFNULL(l.unidades, 0) AS unidades,
  IFNULL(l.ingresos_linea_usd, 0) AS ingresos_linea_usd
FROM eventos e
LEFT JOIN por_categoria c
  ON e.fecha = c.fecha AND e.dispositivo = c.dispositivo AND e.pais = c.pais
 AND e.fuente = c.fuente AND e.medio = c.medio AND e.categoria = c.categoria
LEFT JOIN lineas l
  ON e.fecha = l.fecha AND e.dispositivo = l.dispositivo AND e.pais = l.pais
 AND e.fuente = l.fuente AND e.medio = l.medio AND e.categoria = l.categoria
"""
    )


def sql_sesiones(desde: str, hasta: str) -> str:
    """Una fila por sesión, con los marcadores de tiempo de cada paso del embudo.

    Es la entidad con la que se reconstruyen los recorridos: guarda el inicio, el
    fin y la primera vez que ocurrió cada paso dentro de la sesión (o ``NULL`` si no
    ocurrió), además de los conteos. Así el embudo puede dibujarse con bandas
    proporcionales y comparar tiempos **sin** guardar todos los eventos.
    """
    pasos = [paso["clave"] for paso in esquema.EMBUDO]
    return (
        cte_sesiones(desde, hasta)
        + f"""
, dispositivos AS (
  -- Dispositivo observado en los eventos de la propia sesión: sirve para medir
  -- cuántas sesiones contradicen el dispositivo de adquisición del usuario.
  SELECT
    CONCAT(user_pseudo_id, '|', CAST(session_id AS STRING)) AS clave_sesion,
    ANY_VALUE(dispositivo) AS dispositivo_sesion,
    COUNT(DISTINCT dispositivo) AS dispositivos_distintos
  FROM cruda
  WHERE session_id IS NOT NULL AND user_pseudo_id IS NOT NULL AND user_pseudo_id != ''
  GROUP BY 1
)
, marcas AS (
  SELECT
    clave_sesion, fecha, user_pseudo_id,
    dispositivo, pais, region, fuente, medio,
    MIN(event_timestamp) AS inicio,
    MAX(event_timestamp) AS fin,
    COUNT(*) AS eventos,
    MIN(IF(event_name = '{pasos[0]}', event_timestamp, NULL)) AS t1,
    MIN(IF(event_name = '{pasos[1]}', event_timestamp, NULL)) AS t2,
    MIN(IF(event_name = '{pasos[2]}', event_timestamp, NULL)) AS t3,
    MIN(IF(event_name = '{pasos[3]}', event_timestamp, NULL)) AS t4,
    COUNTIF(event_name = '{pasos[0]}') AS e1,
    COUNTIF(event_name = '{pasos[1]}') AS e2,
    COUNTIF(event_name = '{pasos[2]}') AS e3,
    COUNTIF(event_name = '{pasos[3]}') AS e4,
    SUM(IF(event_name = '{pasos[3]}', ingresos_usd, NULL)) AS ingresos_usd
  FROM con_sesion
  WHERE clave_sesion IS NOT NULL
  GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
)
SELECT
  m.clave_sesion, m.fecha, m.user_pseudo_id,
  m.dispositivo, m.pais, m.region, m.fuente, m.medio,
  m.inicio, m.fin, m.eventos,
  m.t1, m.t2, m.t3, m.t4,
  m.e1, m.e2, m.e3, m.e4,
  m.ingresos_usd,
  d.dispositivo_sesion,
  d.dispositivos_distintos
FROM marcas m
LEFT JOIN dispositivos d USING (clave_sesion)
ORDER BY m.fecha, m.inicio
"""
    )


def sql_articulos(desde: str, hasta: str) -> str:
    """Líneas de artículo de las compras deduplicadas.

    De cada compra se toman los artículos del **primer** evento de compra de su
    clave, para no repetir líneas cuando la misma transacción llega en varios
    eventos. Conserva artículo, categoría, unidades y el importe de la línea.
    """
    return (
        base_cte(desde, hasta)
        + f"""
, claves AS (
  SELECT
    {CLAVE_COMPRA} AS clave_compra,
    event_timestamp,
    fecha,
    user_pseudo_id,
    session_id,
    dispositivo, pais, fuente, medio,
    items
  FROM base
  WHERE event_name = 'purchase'
)
, compras AS (
  SELECT
    clave_compra,
    ARRAY_AGG(
      STRUCT(fecha, user_pseudo_id, session_id, dispositivo, pais, fuente, medio, items)
      ORDER BY event_timestamp
      LIMIT 1
    )[OFFSET(0)] AS compra
  FROM claves
  WHERE clave_compra IS NOT NULL
  GROUP BY clave_compra
)
SELECT
  c.clave_compra,
  c.compra.fecha AS fecha,
  c.compra.user_pseudo_id AS user_pseudo_id,
  c.compra.session_id AS session_id,
  c.compra.dispositivo AS dispositivo,
  c.compra.pais AS pais,
  c.compra.fuente AS fuente,
  c.compra.medio AS medio,
  COALESCE(NULLIF(TRIM(item.item_name), ''), '{esquema.SIN_INFORMACION}') AS articulo,
  COALESCE(NULLIF(TRIM(item.item_category), ''), '{esquema.SIN_INFORMACION}') AS categoria,
  item.quantity AS unidades,
  item.price_in_usd AS precio_unitario_usd,
  item.item_revenue_in_usd AS ingresos_linea_usd
FROM compras c, UNNEST(c.compra.items) AS item
ORDER BY fecha
"""
    )


def sql_eventos(desde: str, hasta: str, por_dia: int) -> str:
    """Muestra de eventos para la tabla de detalle.

    El conjunto tiene millones de eventos: la tabla guarda **hasta `por_dia`
    eventos por día**, elegidos de forma determinista (hash de usuario y marca de
    tiempo), de modo que cubre todo el periodo y la muestra es reproducible. El
    tablero declara siempre cuántos eventos representa frente al total.

    No se devuelve `user_pseudo_id` ni ningún otro identificador de persona.
    """
    pasos = ", ".join(f"'{paso['clave']}'" for paso in esquema.EMBUDO)
    return (
        base_cte(desde, hasta)
        + f"""
, filtrados AS (
  SELECT
    fecha, event_timestamp, event_name, user_pseudo_id,
    dispositivo, pais, fuente, medio,
    ingresos_usd,
    items
  FROM base
  WHERE event_name IN ({pasos})
)
, numerados AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY fecha
      ORDER BY FARM_FINGERPRINT(CONCAT(IFNULL(user_pseudo_id, ''), '|', CAST(event_timestamp AS STRING)))
    ) AS orden
  FROM filtrados
)
SELECT
  fecha,
  event_timestamp,
  event_name,
  dispositivo,
  pais,
  fuente,
  medio,
  (SELECT item.item_name FROM UNNEST(items) AS item WHERE item.item_name IS NOT NULL LIMIT 1) AS articulo,
  (SELECT item.item_category FROM UNNEST(items) AS item WHERE item.item_category IS NOT NULL LIMIT 1) AS categoria,
  ingresos_usd
FROM numerados
WHERE orden <= {int(por_dia)}
ORDER BY fecha, event_timestamp
"""
    )

