# Infrastructure — Phase 14 (Production Deployment & Kubernetes)

Everything needed to run the Django project built in `backend/` as a real,
horizontally-scaled, zero-downtime-deployable production service —
distinct from `backend/`, which is the application itself and knows
nothing about how it's deployed (per `apps/common/health.py`'s liveness/
readiness split and `config/settings.py`'s env-driven config, the
application is deployment-target-agnostic by construction; this directory
is what actually targets it at Kubernetes).

- **[`docker/`](docker/README.md)** — the image, and a local full-stack
  `docker-compose.yml`.
- **[`kubernetes/`](kubernetes/README.md)** — Kustomize base + dev/staging/
  production overlays: Deployments, autoscaling, PodDisruptionBudgets, the
  migrate Job, Ingress, NetworkPolicies. Read this one first — it also
  explains, end to end, how the "zero dropped in-flight rides" rolling-
  deploy requirement is actually satisfied.
- **[`monitoring/`](monitoring/README.md)** — Prometheus scrape config +
  alerts, and a Grafana dashboard, both as code.
- **[`.github/workflows/ci-cd.yml`](../.github/workflows/ci-cd.yml)** +
  **[`.github/actions/deploy/`](../.github/actions/deploy/action.yml)** —
  test → build → push → deploy-staging → deploy-production, the last two
  sharing one composite action so they can't silently drift in *how*
  they deploy, only in *which* overlay/secrets/URL they target.

## The one-sentence version of every major decision here

- **One Docker image, four Kubernetes workloads** (`api` via gunicorn,
  `websocket` via Daphne, `worker` and `beat` via Celery) — one build/
  scan/promote pipeline instead of four to keep in sync.
- **Kustomize, not Helm** — see `kubernetes/README.md`.
- **Managed Postgres/Redis in staging/production, self-hosted only in the
  throwaway `dev` overlay** — see `kubernetes/README.md`.
- **Autoscaling policy is genuinely different per tier**, not the same
  HPA three times with the name changed — `api` scales on CPU (a
  reasonable proxy for a stateless request-response tier), `websocket`
  scales on CPU too but far more conservatively on the way down (removing
  a websocket pod drops its live connections, which removing an api pod
  doesn't), and `worker` ships a CPU-based HPA by default but documents —
  and provides — a KEDA queue-depth `ScaledObject` as the better fit,
  since a task worker's load has no real relationship to its CPU use.
  `beat` is never autoscaled at all: it's a singleton by design.
- **Migrations run as their own Job, once per deploy, never on container
  startup** — several pods starting at once must never race to `ALTER`
  the same tables.
- **CI always deploys staging before production**, sharing one deploy
  action between them; configure `production` as a GitHub Environment
  with required reviewers to turn that into a real manual approval gate.

## Honest limitations

Documented in place, not hidden: `kubernetes/README.md` on what could and
couldn't be mechanically verified without a live cluster in this sandbox
and on the one real gap in the zero-downtime story (an already-open
WebSocket connection can't survive its pod terminating, however gracefully);
`monitoring/README.md` on Celery/business metrics not being exported yet;
`beat-deployment.yaml` on why a singleton scheduler has no
PodDisruptionBudget.
