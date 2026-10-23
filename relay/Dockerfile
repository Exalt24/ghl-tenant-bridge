FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RELAY_DB_PATH=/data/relay.db

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY relay ./relay

# Not root. /data is where a SQLite file lives when the single container deployment is used; Postgres needs no volume.
RUN useradd --create-home --uid 10001 relay && mkdir /data && chown relay /data && chmod 1777 /tmp
USER relay

EXPOSE 8000

# The exec form does not expand ${PORT}, so the command goes through a shell. Railway injects PORT, 8000 is the local default.
# No railway.toml and no Procfile: Railway's config as code is deprecated for new services, so the Dockerfile plus the
# service settings in the README are the whole deploy description.
CMD ["/bin/sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --timeout 30 'relay.app:create_app()'"]
