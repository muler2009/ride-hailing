from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.common.health import liveness, readiness

urlpatterns = [
    path("admin/", admin.site.urls),
    # Phase 14 — Kubernetes probes and Prometheus scraping. Deliberately
    # outside the versioned api/v1/ namespace and unauthenticated: these
    # aren't part of the public API surface, and access to them is
    # restricted at the network layer (see
    # infrastructure/kubernetes/base/networkpolicy.yaml) rather than with
    # an API credential, same as any /metrics endpoint.
    path("healthz/", liveness, name="liveness"),
    path("readyz/", readiness, name="readiness"),
    path("", include("django_prometheus.urls")),  # exposes /metrics
    path("api/v1/", include("apps.users.urls")),
    path("api/v1/", include("apps.drivers.urls")),
    path("api/v1/", include("apps.vehicles.urls")),
    path("api/v1/", include("apps.rides.urls")),
    path("api/v1/", include("apps.dispatch.urls")),
    path("api/v1/", include("apps.locations.urls")),
    path("api/v1/", include("apps.maps.urls")),
    path("api/v1/", include("apps.pricing.urls")),
    path("api/v1/", include("apps.payments.urls")),
    path("api/v1/", include("apps.earnings.urls")),
    path("api/v1/", include("apps.notifications.urls")),
    path("api/v1/", include("apps.ratings.urls")),
    path("api/v1/", include("apps.admin_api.urls")),
    path("api/v1/", include("apps.analytics.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

