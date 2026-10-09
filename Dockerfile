FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Non-root user; /data is owned by it so a fresh Coolify volume mounted there is writable.
RUN useradd --system --uid 10001 --no-create-home immobot \
    && mkdir -p /data && chown immobot /data
USER immobot

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/healthz', timeout=3)"

# Exactly one worker: only one process may poll Telegram updates.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
