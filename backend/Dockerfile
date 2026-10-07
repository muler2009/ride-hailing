# syntax=docker/dockerfile:1
#
# One image, four processes (Phase 14). Every dependency here (psycopg
# [binary], cryptography, PyJWT, gunicorn, daphne, ...) ships a pre-built
# manylinux wheel for this platform, so no C compiler is needed at all —
# the build/runtime split below exists purely to keep pip's cache and the
# venv's build-time footprint out of the final image, not to separate
# "compile" from "run".
#
# The same image is used, with a different command, for all four
# Kubernetes workloads (see infrastructure/kubernetes/base/*.yaml):
#   api        -> gunicorn (this file's default CMD)          — WSGI/HTTP
#   websocket  -> daphne                                        — ASGI/WS
#   worker     -> celery -A config worker
#   beat       -> celery -A config beat
# One image means one build/scan/promote pipeline for all four instead of
# four near-identical Dockerfiles to keep in sync.

# ---- Builder ----------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- Runtime ------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings

# A dedicated non-root user for every process this image runs. Belt and
# braces with the Kubernetes manifests' own `runAsNonRoot: true` /
# `runAsUser: 1000` (a compromised dependency shouldn't get to rely on the
# Dockerfile alone to keep it out of root).
RUN groupadd --gid 1000 django \
    && useradd --uid 1000 --gid django --create-home --shell /usr/sbin/nologin django

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=django:django . .

# Built with placeholder settings so collectstatic (which loads the full
# Django settings module) needs no real secret, database, or Redis
# connection just to hash and copy static assets at build time. The real
# SECRET_KEY/DATABASE_*/REDIS_URL are supplied at *container start*, from
# the Kubernetes Secret/ConfigMap (see infrastructure/kubernetes/base/) —
# never baked into the image.
RUN SECRET_KEY=build-time-placeholder-unused-at-runtime \
    DATABASE_ENGINE=sqlite \
    DEBUG=False \
    python manage.py collectstatic --noinput

USER django

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", \
     "--bind=0.0.0.0:8000", \
     "--worker-class=gthread", \
     "--workers=3", \
     "--threads=4", \
     "--timeout=30", \
     "--graceful-timeout=30", \
     "--forwarded-allow-ips=*", \
     "--access-logfile=-", \
     "--error-logfile=-"]
