"""
Real HTTP calls to Google's Geocoding and Directions APIs. Requires
GOOGLE_MAPS_API_KEY to be set — see .env.example. Not the default provider
(OSRMNominatimProvider is, since it needs no key), but implemented for
real rather than stubbed, to prove the MapProvider abstraction actually
supports swapping providers rather than just gesturing at the idea.

Same testability note as osrm_nominatim.py: apps/maps/tests.py mocks the
`requests` calls rather than hitting Google's real API (which would also
require a funded API key and this sandbox's network doesn't reach
Google's endpoints anyway).
"""
import requests
from django.conf import settings

from apps.maps.base import Coordinates, GeocodeResult, MapProvider, MapProviderError, RouteResult

REQUEST_TIMEOUT_SECONDS = 10
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"


class GoogleMapsProvider(MapProvider):
    def __init__(self):
        self.api_key = settings.GOOGLE_MAPS_API_KEY
        if not self.api_key:
            raise MapProviderError(
                "GOOGLE_MAPS_API_KEY is not configured — set it in .env to use GoogleMapsProvider."
            )

    def geocode(self, address: str) -> GeocodeResult:
        response = requests.get(
            GEOCODE_URL, params={"address": address, "key": self.api_key}, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code != 200:
            raise MapProviderError(f"Geocoding request failed: {response.status_code} {response.text}")

        data = response.json()
        if data.get("status") != "OK" or not data.get("results"):
            raise MapProviderError(f"No geocoding results for address: {address!r} ({data.get('status')})")

        top = data["results"][0]
        location = top["geometry"]["location"]
        return GeocodeResult(
            formatted_address=top["formatted_address"],
            coordinates=Coordinates(latitude=location["lat"], longitude=location["lng"]),
        )

    def reverse_geocode(self, coordinates: Coordinates) -> str:
        response = requests.get(
            GEOCODE_URL,
            params={"latlng": f"{coordinates.latitude},{coordinates.longitude}", "key": self.api_key},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise MapProviderError(f"Reverse geocoding request failed: {response.status_code} {response.text}")

        data = response.json()
        if data.get("status") != "OK" or not data.get("results"):
            raise MapProviderError(f"No address found for coordinates: {coordinates} ({data.get('status')})")

        return data["results"][0]["formatted_address"]

    def get_route(self, origin: Coordinates, destination: Coordinates) -> RouteResult:
        response = requests.get(
            DIRECTIONS_URL,
            params={
                "origin": f"{origin.latitude},{origin.longitude}",
                "destination": f"{destination.latitude},{destination.longitude}",
                "key": self.api_key,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise MapProviderError(f"Directions request failed: {response.status_code} {response.text}")

        data = response.json()
        if data.get("status") != "OK" or not data.get("routes"):
            raise MapProviderError(f"No route found between {origin} and {destination} ({data.get('status')})")

        leg = data["routes"][0]["legs"][0]
        return RouteResult(
            distance_km=leg["distance"]["value"] / 1000,
            duration_minutes=leg["duration"]["value"] / 60,
            polyline=data["routes"][0].get("overview_polyline", {}).get("points"),
        )
