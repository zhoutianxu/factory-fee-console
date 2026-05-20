FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FACTORY_FEE_CONFIG=/app/config/config.production.yaml \
    FACTORY_FEE_DATABASE_PATH=/app/data/factory_fee.db \
    FACTORY_FEE_OUTPUT_DIR=/app/data/output

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY config ./config
COPY factory_fee ./factory_fee
COPY scripts ./scripts
COPY README.md README.md

RUN mkdir -p /app/data/input /app/data/output /app/data/config_tables /app/data/periods

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5050/healthz || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--threads", "4", "--timeout", "120", "factory_fee.wsgi:app"]
