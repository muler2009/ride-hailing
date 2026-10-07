"""
Real HTTP calls to two free, public OpenStreetMap-ecosystem services —
no API key required, which makes this a sensible default for local
development and small deployments:

  - Nominatim (https://nominatim.org) for geocoding/reverse geocoding
  - OSRM (http://project-osrm.org) for routing

NOTE ON TESTABILITY: this module's HTTP calls cannot be exercised against
the real services in every environment (e.g. a network-restricted CI
sandbox) — apps/maps/tests.py mocks the `requests` calls and verifies URL
construction, parameter handling, and response parsing against realistic
fixture payloads instead. If you can reach the public internet, the
provider works exactly as written with no changes.

Nominatim's usage policy requires a descriptive User-Agent identifying the
application — see NOMINATIM_USER_AGENT below and set it to something
specific to your deployment before using this against the public instance
in production; heavy production traffic should point NOMINATIM_BASE_URL /
OSRM_BASE_URL at a self-hosted instance instead of the public one.
"""
import requests
from django.conf import settings

from apps.maps.base import Coordinates, GeocodeResult, MapProvider, MapProviderError, RouteResult

REQUEST_TIMEOUT_SECONDS = 10


class OSRMNominatimProvider(MapProvider):
    def __init__(self):
        self.nominatim_base_url = settings.NOMINATIM_BASE_URL.rstrip("/")
        self.osrm_base_url = settings.OSRM_BASE_URL.rstrip("/")
        self.user_agent = settings.NOMINATIM_USER_AGENT

    def geocode(self, address: str) -> GeocodeResult:
        response = requests.get(
            f"{self.nominatim_base_url}/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": self.user_agent},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise MapProviderError(f"Geocoding request failed: {response.status_code} {response.text}")

        results = response.json()
        if not results:
            raise MapProviderError(f"No geocoding results for address: {address!r}")

        top = results[0]
        return GeocodeResult(
            formatted_address=top["display_name"],
            coordinates=Coordinates(latitude=float(top["lat"]), longitude=float(top["lon"])),
        )

    def reverse_geocode(self, coordinates: Coordinates) -> str:
        response = requests.get(
            f"{self.nominatim_base_url}/reverse",
            params={"lat": coordinates.latitude, "lon": coordinates.longitude, "format": "json"},
            headers={"User-Agent": self.user_agent},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise MapProviderError(f"Reverse geocoding request failed: {response.status_code} {response.text}")

        data = response.json()
        if "error" in data or "display_name" not in data:
            raise MapProviderError(f"No address found for coordinates: {coordinates}")

        return data["display_name"]

    def get_route(self, origin: Coordinates, destination: Coordinates) -> RouteResult:
        # OSRM's HTTP API takes coordinates as longitude,latitude (note the order).
        coords_path = f"{origin.longitude},{origin.latitude};{destination.longitude},{destination.latitude}"
        response = requests.get(
            f"{self.osrm_base_url}/route/v1/driving/{coords_path}",
            params={"overview": "full", "geometries": "polyline"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise MapProviderError(f"Routing request failed: {response.status_code} {response.text}")

        data = response.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            raise MapProviderError(f"No route found between {origin} and {destination}: {data.get('code')}")

        route = data["routes"][0]
        return RouteResult(
            distance_km=route["distance"] / 1000,
            duration_minutes=route["duration"] / 60,
            polyline=route.get("geometry"),
        )
