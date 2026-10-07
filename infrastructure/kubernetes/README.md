# Kubernetes manifests — Phase 14

Kustomize (`base/` + `overlays/{dev,staging,production}/`), not Helm — a
single app with four workload types and three environments doesn't need a
templating language on top of YAML; Kustomize's patch-over-base model
stays closer to plain manifests, which makes every environment's actual
diff from base explicit and readable in one file (each overlay's
`kustomization.yaml`) instead of hidden inside `{{ if }}` conditionals.

## Layout

```
base/                          # environment-agnostic — every real resource lives here
  namespace.yaml, serviceaccount.yaml
  configmap.yaml                # non-secret config
  secret.example.yaml           # documents required Secret keys — NOT applied (see its own header)
  api-*.yaml                    # Deployment, Service, HPA, PodDisruptionBudget
  websocket-*.yaml              # same shape, different tuning — see each file's comments for why
  worker-*.yaml                 # Deployment, HPA (CPU fallback), PDB
  worker-scaledobject.example.yaml  # KEDA alternative to worker-hpa.yaml — not applied by default
  beat-deployment.yaml           # singleton — no HPA, no PDB, by design (see the file)
  migrate-job.yaml
  ingress.yaml
  networkpolicy-*.yaml
  kustomization.yaml
overlays/
  dev/          # throwaway/local cluster; brings its own Postgres+Redis
  staging/      # real cluster, managed Postgres/Redis, lower capacity
  production/   # real cluster, managed Postgres/Redis, higher capacity
```

## Assumptions worth knowing before adapting this elsewhere

- **One cluster per environment** (or at least, this app owns the whole
  `ride-hailing` namespace in whatever cluster it's in). All three
  overlays keep `namespace: ride-hailing` unchanged from base. Running
  multiple environments in one shared cluster needs a per-overlay
  namespace override, which interacts with how Kustomize handles a
  `Namespace` resource inside `resources:` — not taken on here to keep
  the overlays simple and predictable.
- **Managed Postgres and Redis in staging/production** (RDS/Cloud SQL,
  ElastiCache/Memorystore, or equivalent) — deliberately not run as
  StatefulSets in-cluster. A database is the one thing in this whole
  system that isn't stateless-and-disposable the way every Deployment
  here is; self-hosting it inside the same cluster as the app tier trades
  a managed service's backups/PITR/failover for "one less bill," which is
  the wrong trade for a system of record. `overlays/dev/` breaks this
  rule on purpose, clearly marked, purely so a laptop cluster needs
  nothing external at all.
- **An Ingress controller and cert-manager exist in staging/production**
  clusters already. `ingress.yaml`'s annotations assume nginx-ingress +
  cert-manager specifically; swap them for the target cluster's actual
  controller (the path-based routing underneath — `/ws` to `websocket`,
  everything else to `api` — is portable).
- **A `kube-prometheus-stack`-shaped monitoring namespace** — see
  `../monitoring/README.md`.

## Applying manifests: what I could and couldn't verify here

Every file under `base/` was validated against the real upstream
Kubernetes OpenAPI schema
(`yannh/kubernetes-json-schema`) — not just YAML-syntax-checked, but
checked against the actual field names/types/required-fields the
Kubernetes API server itself enforces. The overlays' `kustomization.yaml`
patches were checked for valid YAML and for each patch targeting the
right resource by kind/name, but **I could not run the actual
`kustomize build` / `kubectl apply --dry-run` in the sandbox this was
built in** (no outbound access to fetch the `kustomize` binary at the
time). Before applying for real:

```bash
kubectl kustomize infrastructure/kubernetes/overlays/staging | kubectl apply --dry-run=client -f -
kubectl kustomize infrastructure/kubernetes/overlays/staging | kubectl apply --dry-run=server -f -
```

The `--dry-run=server` form is the stronger check — it validates against
the real live API server (CRDs, admission webhooks, and all) without
actually creating anything.

## First deploy to a new cluster

```bash
# 1. Secrets first — see secret.example.yaml's header for real options
#    (Sealed Secrets / External Secrets Operator / a CI-driven
#    kubectl create secret). None of these are scripted here on purpose:
#    which one applies depends entirely on what the target cluster
#    already has, and templating one in would just be a false sense of
#    completeness for the other two.
kubectl apply -f base/namespace.yaml
# ... create the api-secrets Secret by whichever method fits ...

# 2. Migrations before any app pod exists
kubectl apply -k overlays/production   # creates the ConfigMap, then...
kubectl wait --for=condition=complete --timeout=180s job/migrate -n ride-hailing

# 3. Confirm the rollout actually completed, not just "was accepted"
kubectl rollout status deployment/api -n ride-hailing
kubectl rollout status deployment/websocket -n ride-hailing
kubectl rollout status deployment/worker -n ride-hailing
kubectl rollout status deployment/beat -n ride-hailing
```

Every subsequent deploy is what `.github/workflows/ci-cd.yml` +
`.github/actions/deploy/action.yml` automate: point the overlay at a new
image tag (`kustomize edit set image`), re-run the migrate Job, `apply`,
wait for rollout, smoke-test `/readyz/`.

## How "zero dropped in-flight rides" during a rolling deploy actually works

This is the roadmap's own testable criterion for this phase, and it's
worth walking through end to end rather than taking on faith, because
it's assembled from several small pieces across several files:

1. **`rollingUpdate.maxUnavailable: 0`** (api/websocket/worker Deployments)
   — no existing pod is removed until replacement capacity is confirmed,
   full stop.
2. **`readinessProbe: GET /readyz/`** (`apps/common/health.py`, backend) —
   "confirmed" means this pod has actually proven Postgres and Redis are
   reachable from it, not just that its process started.
3. **`preStop: sleep N`** — when a pod *does* start terminating, this
   runs *before* SIGTERM reaches the app, giving the Service/Ingress
   endpoint-removal a head start so no *new* request is ever routed to a
   pod that's already shutting down.
4. **Graceful SIGTERM handling** — gunicorn's `--graceful-timeout=30`
   (api), Daphne's own graceful shutdown (websocket), Celery's warm
   shutdown (worker) all let whatever's *already in flight* finish rather
   than cutting it off, within a `terminationGracePeriodSeconds` sized to
   comfortably exceed step 3's sleep plus this timeout.
5. **The migrate Job runs to completion before any of the above even
   starts** — so an in-flight request during the rollout is never running
   against a schema the new code doesn't expect.

The one honest gap: an already-**open WebSocket connection** on a
`websocket` pod that's being terminated gets cut regardless of how
generous steps 3–4 are — there's no way to hand off an established TCP
connection to a different pod. `websocket-deployment.yaml`'s own comment
explains why this can't be fully solved at the infrastructure level and
why it doesn't actually violate the stated criterion: a *ride* is a row
in Postgres, untouched by any of this; a *socket* is a connection the
client is expected to reconnect, and does so seeing correct state because
that state was never in the socket to begin with.
