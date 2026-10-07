from functools import lru_cache

from django.conf import settings

from apps.maps.base import MapProvider


def _build_osrm_nominatim():
    from apps.maps.providers.osrm_nominatim import OSRMNominatimProvider

    return OSRMNominatimProvider()


def _build_google_maps():
    from apps.maps.providers.google_maps import GoogleMapsProvider

    return GoogleMapsProvider()


_PROVIDER_BUILDERS = {
    "osrm_nominatim": _build_osrm_nominatim,
    "google_maps": _build_google_maps,
    # "mapbox": _build_mapbox,  # add a providers/mapbox.py implementing MapProvider and register it here
}


@lru_cache(maxsize=1)
def get_map_provider() -> MapProvider:
    """
    Returns the configured MapProvider (settings.MAP_PROVIDER, default
    "osrm_nominatim"). This is the ONLY function any other app should call
    to get a provider instance — never import a provider class directly.
    """
    provider_name = settings.MAP_PROVIDER
    builder = _PROVIDER_BUILDERS.get(provider_name)
    if builder is None:
        raise ValueError(
            f"Unknown MAP_PROVIDER '{provider_name}'. Available: {sorted(_PROVIDER_BUILDERS)}"
        )
    return builder()


def clear_provider_cache() -> None:
    """Used by tests when settings.MAP_PROVIDER changes between test runs."""
    get_map_provider.cache_clear()
