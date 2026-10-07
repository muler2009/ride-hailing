# Prometheus & Grafana — Phase 14

## What's wired up

- **`/metrics`** on both `api` and `websocket` pods (`django-prometheus`;
  see `backend/config/settings.py`'s Observability section and
  `MIDDLEWARE`) — HTTP request counts/latency by status and view, plus
  database query duration via the instrumented Postgres backend.
- **`prometheus/servicemonitor-{api,websocket}.yaml`** — for a
  Prometheus-Operator-managed Prometheus (e.g. `kube-prometheus-stack`).
  Requires the Operator's CRDs to already exist in the cluster.
- **Plain `prometheus.io/scrape` Pod annotations** on the same two
  Deployments, as a fallback for a bare Prometheus with no Operator.
  Whichever applies depends on how Prometheus itself was installed, not
  on anything in this app — both are shipped so neither install method is
  left with nothing.
- **`prometheus/prometheusrule-alerts.yaml`** — a handful of alerts on
  metrics that are actually exposed today (5xx rate, p95 latency, scrape
  target down, HPA pinned at max, pod restart-looping). Also requires the
  Prometheus Operator.
- **`grafana/dashboard-platform-overview.json`** (+
  `dashboard-configmap.yaml`, which wraps it for kube-prometheus-stack's
  Grafana sidecar to auto-load) — 8 panels: request rate, error rate, api
  latency percentiles, DB query duration, replica-count-vs-HPA-max for
  both scaled tiers, pod restarts, and pods-ready by Deployment.

## Installing (if the cluster doesn't already run kube-prometheus-stack)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace
kubectl apply -f infrastructure/monitoring/prometheus/
kubectl apply -f infrastructure/monitoring/grafana/dashboard-configmap.yaml
```

If a cluster already runs Prometheus without the Operator, skip the
`ServiceMonitor`/`PrometheusRule` files (they're CRDs that install won't
recognize) and rely on the Pod annotations instead; import
`dashboard-platform-overview.json` directly in the Grafana UI
(Dashboards → New → Import) rather than applying the ConfigMap.

## What's *not* wired up — real gaps, not oversights

- **Celery task/queue metrics.** Nothing here exports queue depth, task
  duration, or failure counts for the `worker`/`beat` tier. The standard
  fix is running
  [`celery-exporter`](https://github.com/danihodovic/celery-exporter) as
  its own Deployment against the same Redis broker — not included because
  it's a separate component with its own image/RBAC/manifest to maintain,
  not a config tweak to this app. `worker-scaledobject.example.yaml`
  already depends on knowing queue depth for autoscaling, so this is the
  natural next thing to add alongside it.
- **Business metrics.** Dispatch offer-acceptance rate, no-driver-found
  rate, revenue — all real, already-computed numbers (`apps.admin_api`,
  `apps.analytics`), but as SQL aggregates over Postgres, not as anything
  Prometheus can scrape. Exposing them would mean emitting Prometheus
  gauges from a periodic task (e.g. alongside
  `apps.analytics.tasks.compute_daily_rollup_task`) — deliberately not
  invented here without a concrete alerting need driving *which* numbers
  and *what* thresholds, which isn't this phase's call to make up.
- **Distributed tracing.** Request IDs correlate log lines
  (`apps.common.middleware.RequestIDMiddleware`) but don't propagate as
  trace spans across the api → Celery → provider-webhook chain. OpenTelemetry
  would be the natural addition; out of scope here.
