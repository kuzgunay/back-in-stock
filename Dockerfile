# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app

RUN groupadd --system app \
 && useradd --system --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# The SQLite database lives on a volume so it survives container restarts
# and re-creation. The directory is owned by the non-root user, and Docker
# copies that ownership into a fresh named or anonymous volume.
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

# One worker on purpose: the delivery worker and reconciler run inside the
# process, and SQLite wants a single writer (see README).
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1 --no-access-log --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 10"]
