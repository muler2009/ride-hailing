# Docker — Phase 14

## The image

`backend/Dockerfile` — one multi-stage image, shared by all four
Kubernetes workloads (api/websocket/worker/beat; see
`infrastructure/kubernetes/base/*-deployment.yaml`, each overriding only
the container `command`). Build it from the `backend/` directory (it's
the build context — `COPY . .` expects to find `manage.py` at the root of
what's copied):

```bash
docker build -t ride-hailing-backend:local backend/
```

No build args or `--build-arg` secrets are needed — `collectstatic` runs
at build time against placeholder settings (`Dockerfile`'s comment
explains why that's safe), and every *real* secret is supplied at
container start, from the environment, never baked into a layer.

## Local full stack

`infrastructure/docker/docker-compose.yml` runs the same four processes
plus Postgres and Redis — the real production topology, locally:

```bash
cp backend/.env.example backend/.env   # then fill in real values for anything you need working
docker compose -f infrastructure/docker/docker-compose.yml up --build
```

This is **not** what the automated test suite runs against (that's
SQLite — see `backend/README.md` and `.github/workflows/ci-cd.yml`) and
**not** a production stand-in (see
`infrastructure/kubernetes/README.md`'s note on managed vs. self-hosted
Postgres/Redis) — it exists specifically to exercise the api/websocket/
worker/beat split together, which `manage.py runserver` alone can't.

Keycloak is intentionally not part of this compose file, for the same
reason `backend/README.md` gives for local `runserver` use: it's only
needed for actual login, not for anything this compose file is for.

## What's deliberately not here

A `docker-compose.override.yml` for hot-reloading source changes into the
containers, a `Makefile` wrapping these commands, and a local Keycloak +
seed-realm setup are all reasonable additions a team would likely want —
none are included here to keep this phase's actual deliverable (the
production deployment path) from sprawling into general local-DX tooling,
which is a separate, genuinely optional decision for whoever adopts this.
