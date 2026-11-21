# Imagen de la API del portafolio (solo lectura sobre MongoDB).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Certificados raíz: el importador descarga los boletines de la CNBV por HTTPS
# (la imagen -slim no los trae y urllib falla con CERTIFICATE_VERIFY_FAILED).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dependencias primero: aprovecha la caché de capas.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Snapshots de runtime. Los tres servicios que los leen los abren en su
# constructor, así que sin estos archivos la API no arranca:
#   data/snapshots/banxico/corpus.json              → briefing BANXICO (RAG léxico)
#   data/snapshots/copiloto/data.json               → Copiloto NL → SQL
#   data/snapshots/incident-commander/data.json     → Data Incident Commander
# El resto de `data/` queda fuera del contexto (ver .dockerignore): son entradas
# de importación que no se versionan.
COPY data/snapshots ./data/snapshots

# Carpeta escribible para el índice SQLite que el Copiloto genera al arrancar
# (`data/copiloto_nl_sql/wdi.sqlite3`). Se crea antes del chown para que el
# usuario sin privilegios pueda escribirla.
RUN mkdir -p /app/data/copiloto_nl_sql

# Usuario sin privilegios.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3).read()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
