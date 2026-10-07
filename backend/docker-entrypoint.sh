#!/usr/bin/env bash
# Deliberately minimal. This one image is shared by four very different
# processes — gunicorn (api), daphne (websocket), `celery worker`, and
# `celery beat` (see infrastructure/kubernetes/base/*-deployment.yaml for
# which command each one runs) — and the one thing none of them should do
# is run database migrations on startup: a rolling deploy starts several
# pods at once, and they'd all race to ALTER the same tables concurrently.
# Migrations run exactly once per deploy, as their own Kubernetes Job
# (infrastructure/kubernetes/base/migrate-job.yaml), before the rollout of
# any of these Deployments begins.
set -euo pipefail
exec "$@"
