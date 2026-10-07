"""
The abstraction from the architecture doc's Maps & Routing section:

    MapProvider
    ├── GoogleMapsProvider
    ├── MapboxProvider (not implemented — trivial to add following this interface)
    └── OSRMNominatimProvider (OpenStreetMap-based: Nominatim + OSRM, no API key)

No calling code anywhere in the platform should import a specific provider
directly — everything goes through apps/maps/factory.py:get_map_provider(),
so switching providers (or adding Mapbox) never touches apps/rides or any
other consumer.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class MapProviderError(Exception):
    """Raised for any failure talking to a mapping provider — network, auth, or a not-found result."""


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class GeocodeResult:
    formatted_address: str
    coordinates: Coordinates


@dataclass(frozen=True)
class RouteResult:
    distance_km: float
    duration_minutes: float
    polyline: Optional[str] = None  # encoded polyline for map display, when the provider supplies one


class MapProvider(ABC):
    """
    Every provider implements exactly these three operations. Anything
    provider-specific (API keys, request shapes, rate limits) is entirely
    hidden behind this interface.
    """

    @abstractmethod
    def geocode(self, address: str) -> GeocodeResult:
        """Address -> coordinates. Raises MapProviderError if the address can't be resolved."""

    @abstractmethod
    def reverse_geocode(self, coordinates: Coordinates) -> str:
        """Coordinates -> a human-readable address. Raises MapProviderError on failure."""

    @abstractmethod
    def get_route(self, origin: Coordinates, destination: Coordinates) -> RouteResult:
        """Origin/destination -> distance, duration, and (if available) a polyline."""
