# syntax=docker/dockerfile:1
#
# Backend image. Deliberately plain: this service is FastAPI + pydantic
# plus two optional drivers, so a multi-stage build with a compiler
# toolchain would add complexity for nothing — psycopg[binary] ships
# wheels, and nothing here needs to be compiled.
FROM python:3.12-slim

# Never write .pyc into the layer, never buffer stdout (so logs actually
# appear in `docker logs` in real time rather than in bursts on exit).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first, so a source-only change doesn't re-run pip install.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as a non-root user. The SQLite fallback writes into /data, so that
# directory has to be owned by the runtime user — otherwise a deployment
# without DATABASE_URL crashes on first write instead of degrading to the
# local backend as designed.
RUN useradd --create-home --uid 10001 autonomize \
    && mkdir -p /data \
    && chown -R autonomize:autonomize /app /data
USER autonomize

ENV AUTONOMIZE_DB_PATH=/data/autonomize.db \
    PORT=8787

EXPOSE 8787

# /api/health issues a real query, so this reports unhealthy when the
# database is unreachable rather than merely when the process has died.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os,urllib.request,sys; \
port=os.environ.get('PORT','8787'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=4).status==200 else 1)"

# Shell form so $PORT expands — most PaaS platforms (Render, Railway, Fly,
# Cloud Run) assign the port at runtime rather than letting you pick it.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
