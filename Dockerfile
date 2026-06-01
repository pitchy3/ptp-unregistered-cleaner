FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PTP_CONFIG_PATH=/config/config.yaml

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /config /data \
    && chown -R appuser:appuser /app /config /data

USER appuser
VOLUME ["/config", "/data"]

CMD ["ptp-unregistered-cleaner", "daemon"]
