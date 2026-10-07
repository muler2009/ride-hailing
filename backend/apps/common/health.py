"""
Liveness/readiness endpoints for the Kubernetes probes (Phase 14) — see
infrastructure/kubernetes/base/api-deployment.yaml and
websocket-deployment.yaml for how they're wired in.

Deliberately two separate views with two different failure semantics,
matching Kubernetes' own liveness/readiness distinction:

- A liveness failure gets the pod killed and restarted. That's only the
  right response if restarting actually fixes something (a wedged
  process, a deadlock) — so liveness here checks nothing but "can this
  process return an HTTP response at all."
- A readiness failure just pulls the pod out of Service rotation until it
  passes again; the pod keeps running. That's the right response to "the
  database is briefly unreachable" — restarting this pod wouldn't bring
  Postgres back, and killing every pod at once the moment a shared
  dependency has a blip would turn a transient outage into a self-inflicted
  total one.

This second distinction is also most of the mechanism behind this phase's
"zero dropped in-flight rides during a rolling deploy" requirement: the
readinessProbe is what a rolling update polls before it's willing to
route traffic to a new pod, and what keeps a pod that's shutting down out
of rotation for the last few seconds of its life (see the preStop hook in
the Deployment manifests).
"""
from functools import lru_cache

import redis
from django.conf import settings
from django.db import connections
from django.db.utils import Error as DjangoDatabaseError
from django.http import JsonResponse


@lru_cache(maxsize=1)
def _readiness_redis_client() -> "redis.Redis":
    # A small, independent client rather than reusing
    # apps.locations.geo.get_redis_client(): apps.common is a base module
    # every other app depends on, so it must not import from a domain app
    # like apps.locations — that would invert the dependency direction.
    return redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)


def liveness(request):
    """GET /healthz/ — the process is up and can handle a request. See module docstring for why this checks nothing else."""
    return JsonResponse({"status": "ok"})


def readiness(request):
    """GET /readyz/ — can this pod actually serve traffic right now? Checked against every dependency a request needs."""
    checks = {}
    healthy = True

    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["database"] = "ok"
    except DjangoDatabaseError as exc:
        checks["database"] = f"error: {exc.__class__.__name__}"
        healthy = False

    try:
        _readiness_redis_client().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc.__class__.__name__}"
        healthy = False

    return JsonResponse(
        {"status": "ok" if healthy else "unavailable", "checks": checks},
        status=200 if healthy else 503,
    )
