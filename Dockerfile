FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FACTORY_FEE_RUNTIME_DIR=/app/runtime \
    FACTORY_FEE_CONFIG=/app/runtime/config/config.production.yaml \
    FACTORY_FEE_DATABASE_PATH=/app/runtime/data/factory_fee.db \
    FACTORY_FEE_OUTPUT_DIR=/app/runtime/data/output \
    PORT=5050 \
    GUNICORN_WORKERS=2 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=120

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY config ./config
COPY deploy/docker-entrypoint.sh /usr/local/bin/factory-fee-entrypoint
COPY factory_fee ./factory_fee
COPY scripts ./scripts
COPY README.md README.md

RUN chmod +x /usr/local/bin/factory-fee-entrypoint \
    && mkdir -p /app/runtime/data/input /app/runtime/data/output /app/runtime/data/config_tables /app/runtime/data/periods

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5050/healthz || exit 1

ENTRYPOINT ["factory-fee-entrypoint"]
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers ${GUNICORN_WORKERS} --threads ${GUNICORN_THREADS} --timeout ${GUNICORN_TIMEOUT} factory_fee.wsgi:app"]
