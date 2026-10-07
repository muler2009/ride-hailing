"""
Django settings for the Ride-Hailing Platform.

Environment-driven so the same codebase runs against SQLite for local dev
and against PostgreSQL in staging/production, per the architecture doc
(PostgreSQL is the system of record; nothing here should assume SQLite).

Authentication is delegated to Keycloak (OIDC) — see
apps/identity/keycloak_client.py and apps/identity/authentication.py.
Authorization (RBAC) remains entirely local, per apps/identity/services.py.

Phase 14 (Production Deployment & Kubernetes) added the sections below
marked "Phase 14": structured logging, Prometheus metrics, static-file
serving via WhiteNoise, a Redis-backed cache option, and the
SECURE_*/proxy settings needed to run correctly behind a TLS-terminating
Ingress. Nothing above those sections changed in shape — Phase 14 is
additive hardening on an already-complete application, not a rewrite.
"""

import sys
from decimal import Decimal
from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config(
    "SECRET_KEY",
    default="dev-only-insecure-key-do-not-use-in-production-U2U0QXXcK-dhHsy5vtQDDzDNcmrPkh",
)
DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())

INSTALLED_APPS = [
    "daphne",  # first, so it takes over `runserver` for ASGI/WebSocket support
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "corsheaders",
    "channels",
    "django_prometheus",  # Phase 14 — /metrics; see MIDDLEWARE for the required before/after pair
    # platform apps (mirrors the bounded contexts in the architecture doc)
    "apps.common",
    "apps.identity",
    "apps.users",
    "apps.drivers",
    "apps.vehicles",
    "apps.rides",
    "apps.dispatch",
    "apps.locations",
    "apps.maps",
    "apps.pricing",
    "apps.payments",
    "apps.earnings",
    "apps.notifications",
    "apps.ratings",
    "apps.admin_api",
    "apps.analytics",
]

MIDDLEWARE = [
    # Phase 14: must be first — django-prometheus times the request from
    # as early as possible, and its matching "after" middleware (below)
    # must be last for the same reason, in reverse.
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Phase 14: WhiteNoise serves collected static files (admin CSS/JS, DRF's
    # browsable-API assets) directly from the app process — no separate
    # nginx/CDN needed for a modest admin surface. Must sit directly after
    # SecurityMiddleware, per WhiteNoise's own documented requirement.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Phase 14: tags this request with an ID (generating one, or reusing an
    # inbound X-Request-ID from the Ingress) before anything downstream logs.
    "apps.common.middleware.RequestIDMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

AUTH_USER_MODEL = "users.User"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
if config("DATABASE_ENGINE", default="sqlite") == "postgres":
    DATABASES = {
        "default": {
            # django-prometheus's instrumented backend wraps the real
            # postgresql backend to export django_db_query_duration_seconds
            # etc. (Phase 14) — a drop-in ENGINE swap, not a different driver.
            "ENGINE": "django_prometheus.db.backends.postgresql",
            "NAME": config("DATABASE_NAME", default="ride_hailing"),
            "USER": config("DATABASE_USER", default="postgres"),
            "PASSWORD": config("DATABASE_PASSWORD", default=""),
            "HOST": config("DATABASE_HOST", default="localhost"),
            "PORT": config("DATABASE_PORT", default="5432"),
            # Phase 14: persistent connections, so a pod under steady load
            # isn't paying a fresh TCP+TLS+auth handshake on every request —
            # meaningful at Kubernetes scale in a way it isn't for a single
            # local dev process. CONN_HEALTH_CHECKS validates a pooled
            # connection is still alive before reusing it, rather than
            # handing a view a connection Postgres already dropped.
            "CONN_MAX_AGE": config("DATABASE_CONN_MAX_AGE", default=60, cast=int),
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Phase 14: collectstatic's target directory inside the container image
# (see infrastructure/docker/Dockerfile) — WhiteNoise serves straight from
# here, so this only matters in a real deployment; local `runserver` never
# reads it (Django's staticfiles finder serves app static/ dirs directly).
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # CompressedManifestStaticFilesStorage needs `collectstatic` to have
        # already run (it hashes filenames and writes a manifest) — true in
        # the Docker image, not true for a bare local checkout, hence the
        # DEBUG-gated fallback to Django's plain storage for `runserver`.
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}

# ---------------------------------------------------------------------------
# CORS — mobile/web clients call the API from different origins.
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS", default="http://localhost:3000,http://localhost:5173", cast=Csv()
)

# ---------------------------------------------------------------------------
# Logging (Phase 14) — plain stdout, always. Kubernetes captures a
# container's stdout/stderr and ships it to whatever the cluster runs for
# log aggregation (Loki, CloudWatch, Stackdriver, ...); a Django
# FileHandler and log rotation would just be writing to a container
# filesystem nothing ever reads. LOG_FORMAT picks the *shape* of that
# stdout line: human-readable text for local dev, one JSON object per line
# in staging/production so the aggregator can index level/logger/
# request_id as real fields instead of grepping free text. Every line
# carries the current request's ID (apps.common.middleware.RequestIDMiddleware)
# so a single request's log lines — including ones several function calls
# deep, with no request object threaded through — can be pulled together
# after the fact.
# ---------------------------------------------------------------------------
LOG_LEVEL = config("LOG_LEVEL", default="INFO")
LOG_FORMAT = config("LOG_FORMAT", default="console" if DEBUG else "json")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "apps.common.logging_filters.RequestIDLogFilter"},
    },
    "formatters": {
        "console": {
            "format": "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s",
        },
        "json": {"()": "config.logging_formatters.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["request_id"],
            "formatter": LOG_FORMAT,
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        # Both explicitly re-declared (rather than left to inherit "root")
        # so LOG_LEVEL controls them independently of any other library
        # that might otherwise talk over Django/Celery at the root logger.
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "celery": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

# ---------------------------------------------------------------------------
# Observability (Phase 14) — Prometheus metrics via django-prometheus (see
# MIDDLEWARE and the instrumented Postgres ENGINE above). Exposed at
# /metrics by config/urls.py. Not authenticated — like every /metrics
# endpoint, it's expected to be reachable only from inside the cluster
# (the Prometheus scraper), which infrastructure/kubernetes enforces with a
# NetworkPolicy rather than an API credential.
# ---------------------------------------------------------------------------
# Skips the query django-prometheus would otherwise run against
# django_migrations on every single scrape just to export one gauge —
# migration state is checked once at deploy time (the migrate Job), not on
# a 15-second Prometheus interval.
PROMETHEUS_EXPORT_MIGRATIONS = False

# ---------------------------------------------------------------------------
# Security / reverse proxy (Phase 14) — the app is only ever reached
# through a TLS-terminating Ingress in staging/production (see
# infrastructure/kubernetes/base/ingress.yaml), so Django itself never
# speaks TLS; it has to trust the Ingress's X-Forwarded-Proto instead of
# its own request scheme to know whether the original request was https.
# ---------------------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

if not DEBUG:
    SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)
    # kubelet's liveness/readiness probes hit the pod directly over plain
    # HTTP on the container port — they never go through the TLS-terminating
    # Ingress, so there is no X-Forwarded-Proto to make them look "secure".
    # Without this, SECURE_SSL_REDIRECT would 301 every probe request,
    # which is at best wasted round-trips and at worst a prober that treats
    # a redirect as failure — for two endpoints that carry no sensitive
    # data and exist specifically to be hit unencrypted, in-cluster.
    SECURE_REDIRECT_EXEMPT = [r"^healthz/$", r"^readyz/$"]
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    # Conservative default (1 day). Ramp this up over successive real
    # rollouts (a week, then a month, then a year) rather than starting at
    # a long value — an HSTS max-age is a promise browsers hold you to even
    # if a later deploy needs to serve plain HTTP again.
    SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=86400, cast=int)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = config("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False, cast=bool)
    # Both default False for the reason above — a preload-listed domain is
    # effectively permanent (removal takes months and a browser release
    # cycle), so this is a deliberate later opt-in once HSTS itself has
    # been running safely for a while, not a launch-day default.
    SECURE_HSTS_PRELOAD = config("SECURE_HSTS_PRELOAD", default=False, cast=bool)
else:
    SECURE_SSL_REDIRECT = False

# ---------------------------------------------------------------------------
# Django REST Framework — platform-wide API conventions
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.identity.authentication.KeycloakAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.common.pagination.StandardResultsPagination",
    "PAGE_SIZE": 20,
    "EXCEPTION_HANDLER": "apps.common.exceptions.api_exception_handler",
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
}

# ---------------------------------------------------------------------------
# Keycloak — the platform's identity provider (OIDC). RBAC/authorization
# stays local; see apps/identity/services.py.
# ---------------------------------------------------------------------------
KEYCLOAK_SERVER_URL = config("KEYCLOAK_SERVER_URL", default="http://localhost:8080")
KEYCLOAK_REALM = config("KEYCLOAK_REALM", default="ride-hailing")
KEYCLOAK_CLIENT_ID = config("KEYCLOAK_CLIENT_ID", default="ride-hailing-backend")
KEYCLOAK_CLIENT_SECRET = config("KEYCLOAK_CLIENT_SECRET", default="")

# Used only for the Admin API calls made at registration time (creating a
# Keycloak user, assigning a realm role) — see apps/identity/keycloak_client.py.
KEYCLOAK_ADMIN_CLIENT_ID = config("KEYCLOAK_ADMIN_CLIENT_ID", default="admin-cli")
KEYCLOAK_ADMIN_USERNAME = config("KEYCLOAK_ADMIN_USERNAME", default="admin")
KEYCLOAK_ADMIN_PASSWORD = config("KEYCLOAK_ADMIN_PASSWORD", default="admin")

# ---------------------------------------------------------------------------
# Redis — the driver-location store dispatch queries (apps/dispatch/geo.py).
# Distinct from CACHES below (that's the RBAC permission-lookup cache);
# this connection is used directly for GEO commands, which Django's cache
# abstraction doesn't expose.
# ---------------------------------------------------------------------------
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379/0")

# ---------------------------------------------------------------------------
# Cache — used by the RBAC permission lookup (apps/identity/services.py).
# LocMemCache is process-local, which is silently wrong the moment there's
# more than one process: a role change made via one Kubernetes pod would
# leave every *other* pod's permission cache stale with no way to know it
# needs invalidating. Phase 14 makes that finally configurable — set
# CACHE_BACKEND=redis (the production/staging default in
# infrastructure/kubernetes) to share one cache across every pod, using
# Django's built-in Redis backend (no extra dependency). Local dev keeps
# LocMemCache so `manage.py runserver` needs nothing else running.
# ---------------------------------------------------------------------------
CACHE_BACKEND = config("CACHE_BACKEND", default="locmem")
if CACHE_BACKEND == "redis":
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": config("CACHE_URL", default=REDIS_URL),
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# ---------------------------------------------------------------------------
# Channels — WebSocket support for real-time ride tracking (apps/locations).
# Backed by the same Redis instance; a distinct logical layer from the
# driver-location GEO store and the RBAC cache above.
# ---------------------------------------------------------------------------
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    }
}

# ---------------------------------------------------------------------------
# Maps & Routing (apps/maps) — provider abstraction. "osrm_nominatim" needs
# no API key and is the default; "google_maps" needs GOOGLE_MAPS_API_KEY.
# ---------------------------------------------------------------------------
MAP_PROVIDER = config("MAP_PROVIDER", default="osrm_nominatim")

NOMINATIM_BASE_URL = config("NOMINATIM_BASE_URL", default="https://nominatim.openstreetmap.org")
NOMINATIM_USER_AGENT = config(
    "NOMINATIM_USER_AGENT", default="ride-hailing-platform-dev (set a real contact in production)"
)
OSRM_BASE_URL = config("OSRM_BASE_URL", default="https://router.project-osrm.org")

GOOGLE_MAPS_API_KEY = config("GOOGLE_MAPS_API_KEY", default="")

# ---------------------------------------------------------------------------
# Payments (apps/payments) — provider abstraction. Neither provider is
# configured by default since both need real merchant credentials;
# payments via CASH work with no configuration at all.
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY = config("STRIPE_SECRET_KEY", default="")
STRIPE_WEBHOOK_SECRET = config("STRIPE_WEBHOOK_SECRET", default="")

CHAPA_SECRET_KEY = config("CHAPA_SECRET_KEY", default="")
CHAPA_WEBHOOK_SECRET = config("CHAPA_WEBHOOK_SECRET", default="")
CHAPA_CALLBACK_URL = config("CHAPA_CALLBACK_URL", default="http://localhost:8000/api/v1/payments/webhooks/chapa/")

# ---------------------------------------------------------------------------
# Driver Earnings & Wallet (apps/earnings)
# ---------------------------------------------------------------------------
PLATFORM_COMMISSION_RATE = config("PLATFORM_COMMISSION_RATE", default="0.20")
MINIMUM_PAYOUT_AMOUNT = config("MINIMUM_PAYOUT_AMOUNT", default="10.00", cast=Decimal)

# ---------------------------------------------------------------------------
# Celery — async task processing (apps/notifications, Phase 10). Broker is
# the same Redis instance used elsewhere (see config/celery.py for why).
# CELERY_TASK_ALWAYS_EAGER lets tests run tasks synchronously in-process
# without a running worker — standard Celery testing practice.
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=REDIS_URL)
_RUNNING_TESTS = "test" in sys.argv
CELERY_TASK_ALWAYS_EAGER = config("CELERY_TASK_ALWAYS_EAGER", default=_RUNNING_TESTS, cast=bool)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
# Phase 14: let Django's LOGGING config (above) own the root logger instead
# of Celery installing its own handler/formatter — otherwise worker and
# beat processes log in a different shape than the api/websocket processes,
# defeating the point of structured JSON logs in production.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

# ---------------------------------------------------------------------------
# Notifications (apps/notifications)
# ---------------------------------------------------------------------------
EMAIL_BACKEND = config("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="no-reply@example.com")

SMS_PROVIDER = config("SMS_PROVIDER", default="twilio")
TWILIO_ACCOUNT_SID = config("TWILIO_ACCOUNT_SID", default="")
TWILIO_AUTH_TOKEN = config("TWILIO_AUTH_TOKEN", default="")
TWILIO_FROM_NUMBER = config("TWILIO_FROM_NUMBER", default="")

PUSH_PROVIDER = config("PUSH_PROVIDER", default="fcm")
FCM_PROJECT_ID = config("FCM_PROJECT_ID", default="")
FCM_ACCESS_TOKEN = config("FCM_ACCESS_TOKEN", default="")
