from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.maps.base import Coordinates, MapProviderError
from apps.maps.factory import clear_provider_cache, get_map_provider
from apps.maps.providers.google_maps import GoogleMapsProvider
from apps.maps.providers.osrm_nominatim import OSRMNominatimProvider
from apps.vehicles.models import VehicleType


def _mock_response(status_code=200, json_data=None, text=""):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.text = text
    return response


class OSRMNominatimProviderTests(TestCase):
    """
    Exercises URL construction and response parsing against realistic
    fixture payloads shaped exactly like Nominatim/OSRM's real APIs — the
    `requests` calls themselves are mocked since this sandbox's network
    can't reach the public internet. If you can, OSRMNominatimProvider
    works against the real services with zero code changes.
    """

    def setUp(self):
        self.provider = OSRMNominatimProvider()

    @mock.patch("apps.maps.providers.osrm_nominatim.requests.get")
    def test_geocode_parses_nominatim_response(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data=[
                {
                    "display_name": "Addis Ababa, Ethiopia",
                    "lat": "9.0300",
                    "lon": "38.7400",
                }
            ]
        )

        result = self.provider.geocode("Addis Ababa")

        self.assertEqual(result.formatted_address, "Addis Ababa, Ethiopia")
        self.assertAlmostEqual(result.coordinates.latitude, 9.03)
        self.assertAlmostEqual(result.coordinates.longitude, 38.74)

        call_args = mock_get.call_args
        self.assertIn("/search", call_args.args[0])
        self.assertEqual(call_args.kwargs["params"]["q"], "Addis Ababa")
        self.assertIn("User-Agent", call_args.kwargs["headers"])

    @mock.patch("apps.maps.providers.osrm_nominatim.requests.get")
    def test_geocode_with_no_results_raises(self, mock_get):
        mock_get.return_value = _mock_response(json_data=[])

        with self.assertRaises(MapProviderError):
            self.provider.geocode("a place that does not exist anywhere")

    @mock.patch("apps.maps.providers.osrm_nominatim.requests.get")
    def test_geocode_http_error_raises(self, mock_get):
        mock_get.return_value = _mock_response(status_code=503, text="Service Unavailable")

        with self.assertRaises(MapProviderError):
            self.provider.geocode("Addis Ababa")

    @mock.patch("apps.maps.providers.osrm_nominatim.requests.get")
    def test_reverse_geocode_parses_response(self, mock_get):
        mock_get.return_value = _mock_response(json_data={"display_name": "Bole Road, Addis Ababa, Ethiopia"})

        address = self.provider.reverse_geocode(Coordinates(latitude=9.03, longitude=38.74))

        self.assertEqual(address, "Bole Road, Addis Ababa, Ethiopia")
        call_args = mock_get.call_args
        self.assertEqual(call_args.kwargs["params"]["lat"], 9.03)
        self.assertEqual(call_args.kwargs["params"]["lon"], 38.74)

    @mock.patch("apps.maps.providers.osrm_nominatim.requests.get")
    def test_reverse_geocode_with_error_field_raises(self, mock_get):
        mock_get.return_value = _mock_response(json_data={"error": "Unable to geocode"})

        with self.assertRaises(MapProviderError):
            self.provider.reverse_geocode(Coordinates(latitude=0, longitude=0))

    @mock.patch("apps.maps.providers.osrm_nominatim.requests.get")
    def test_get_route_parses_osrm_response(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data={
                "code": "Ok",
                "routes": [{"distance": 5200.0, "duration": 720.0, "geometry": "abc123polyline"}],
            }
        )

        route = self.provider.get_route(
            origin=Coordinates(latitude=9.03, longitude=38.74),
            destination=Coordinates(latitude=9.01, longitude=38.76),
        )

        self.assertAlmostEqual(route.distance_km, 5.2)
        self.assertAlmostEqual(route.duration_minutes, 12.0)
        self.assertEqual(route.polyline, "abc123polyline")

        # OSRM expects longitude,latitude order in the URL path.
        call_args = mock_get.call_args
        self.assertIn("38.74,9.03;38.76,9.01", call_args.args[0])

    @mock.patch("apps.maps.providers.osrm_nominatim.requests.get")
    def test_get_route_with_no_route_found_raises(self, mock_get):
        mock_get.return_value = _mock_response(json_data={"code": "NoRoute", "routes": []})

        with self.assertRaises(MapProviderError):
            self.provider.get_route(
                origin=Coordinates(latitude=9.03, longitude=38.74),
                destination=Coordinates(latitude=9.01, longitude=38.76),
            )


class GoogleMapsProviderTests(TestCase):
    def setUp(self):
        with override_settings(GOOGLE_MAPS_API_KEY="test-key"):
            self.provider = GoogleMapsProvider()

    def test_missing_api_key_raises_on_construction(self):
        with override_settings(GOOGLE_MAPS_API_KEY=""):
            with self.assertRaises(MapProviderError):
                GoogleMapsProvider()

    @mock.patch("apps.maps.providers.google_maps.requests.get")
    def test_geocode_parses_google_response(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data={
                "status": "OK",
                "results": [
                    {
                        "formatted_address": "Addis Ababa, Ethiopia",
                        "geometry": {"location": {"lat": 9.03, "lng": 38.74}},
                    }
                ],
            }
        )

        result = self.provider.geocode("Addis Ababa")

        self.assertEqual(result.formatted_address, "Addis Ababa, Ethiopia")
        self.assertEqual(result.coordinates.latitude, 9.03)
        self.assertEqual(result.coordinates.longitude, 38.74)

    @mock.patch("apps.maps.providers.google_maps.requests.get")
    def test_geocode_zero_results_raises(self, mock_get):
        mock_get.return_value = _mock_response(json_data={"status": "ZERO_RESULTS", "results": []})

        with self.assertRaises(MapProviderError):
            self.provider.geocode("nowhere")

    @mock.patch("apps.maps.providers.google_maps.requests.get")
    def test_get_route_parses_directions_response(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data={
                "status": "OK",
                "routes": [
                    {
                        "legs": [{"distance": {"value": 5200}, "duration": {"value": 720}}],
                        "overview_polyline": {"points": "xyz789"},
                    }
                ],
            }
        )

        route = self.provider.get_route(
            origin=Coordinates(latitude=9.03, longitude=38.74),
            destination=Coordinates(latitude=9.01, longitude=38.76),
        )

        self.assertAlmostEqual(route.distance_km, 5.2)
        self.assertAlmostEqual(route.duration_minutes, 12.0)
        self.assertEqual(route.polyline, "xyz789")


class ProviderFactoryTests(TestCase):
    def tearDown(self):
        clear_provider_cache()

    def test_default_provider_is_osrm_nominatim(self):
        clear_provider_cache()
        with override_settings(MAP_PROVIDER="osrm_nominatim"):
            provider = get_map_provider()
        self.assertIsInstance(provider, OSRMNominatimProvider)

    def test_can_select_google_maps_provider(self):
        clear_provider_cache()
        with override_settings(MAP_PROVIDER="google_maps", GOOGLE_MAPS_API_KEY="test-key"):
            provider = get_map_provider()
        self.assertIsInstance(provider, GoogleMapsProvider)

    def test_unknown_provider_name_raises(self):
        clear_provider_cache()
        with override_settings(MAP_PROVIDER="not_a_real_provider"):
            with self.assertRaises(ValueError):
                get_map_provider()


class MapsApiTests(APITestCase):
    """
    These endpoints are AllowAny, matching the "no account required"
    philosophy already established for ride creation — a guest planning a
    ride needs to search addresses before they can submit one.
    """

    def tearDown(self):
        clear_provider_cache()

    @mock.patch("apps.maps.views.get_map_provider")
    def test_geocode_endpoint_requires_no_auth(self, mock_get_provider):
        from apps.maps.base import Coordinates as C
        from apps.maps.base import GeocodeResult

        mock_provider = mock.Mock()
        mock_provider.geocode.return_value = GeocodeResult(
            formatted_address="Addis Ababa, Ethiopia", coordinates=C(latitude=9.03, longitude=38.74)
        )
        mock_get_provider.return_value = mock_provider

        response = self.client.post(reverse("maps_geocode"), {"address": "Addis Ababa"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["formatted_address"], "Addis Ababa, Ethiopia")

    @mock.patch("apps.maps.views.get_map_provider")
    def test_geocode_endpoint_surfaces_provider_error_as_422(self, mock_get_provider):
        mock_provider = mock.Mock()
        mock_provider.geocode.side_effect = MapProviderError("No results")
        mock_get_provider.return_value = mock_provider

        response = self.client.post(reverse("maps_geocode"), {"address": "nowhere"}, format="json")

        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.data)

    @mock.patch("apps.maps.views.get_map_provider")
    def test_route_endpoint_requires_no_auth(self, mock_get_provider):
        from apps.maps.base import RouteResult

        mock_provider = mock.Mock()
        mock_provider.get_route.return_value = RouteResult(distance_km=5.2, duration_minutes=12.0, polyline="abc")
        mock_get_provider.return_value = mock_provider

        response = self.client.post(
            reverse("maps_route"),
            {
                "origin_latitude": "9.03", "origin_longitude": "38.74",
                "destination_latitude": "9.01", "destination_longitude": "38.76",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertAlmostEqual(response.data["distance_km"], 5.2)
        self.assertAlmostEqual(response.data["duration_minutes"], 12.0)

    def test_invalid_coordinates_are_rejected(self):
        response = self.client.post(
            reverse("maps_reverse_geocode"), {"latitude": "999", "longitude": "38.74"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RideCreationRouteIntegrationTests(APITestCase):
    """Confirms create_ride's best-effort route estimation and its failure tolerance."""

    def tearDown(self):
        clear_provider_cache()

    def _ride_payload(self):
        sedan = VehicleType.objects.get(name="Sedan")
        return {
            "vehicle_type_id": str(sedan.id),
            "pickup_address": "A", "pickup_latitude": "9.0300", "pickup_longitude": "38.7400",
            "destination_address": "B", "destination_latitude": "9.0100", "destination_longitude": "38.7600",
            "guest_phone_number": "+15551234567",
        }

    @mock.patch("apps.rides.views.get_map_provider")
    def test_successful_route_lookup_populates_ride_estimate(self, mock_get_provider):
        from apps.maps.base import RouteResult

        mock_provider = mock.Mock()
        mock_provider.get_route.return_value = RouteResult(distance_km=3.4, duration_minutes=9.0)
        mock_get_provider.return_value = mock_provider

        response = self.client.post(reverse("ride_create"), self._ride_payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertAlmostEqual(float(response.data["estimated_distance_km"]), 3.4)
        self.assertAlmostEqual(float(response.data["estimated_duration_minutes"]), 9.0)

    @mock.patch("apps.rides.views.get_map_provider")
    def test_route_lookup_failure_does_not_break_ride_creation(self, mock_get_provider):
        mock_provider = mock.Mock()
        mock_provider.get_route.side_effect = MapProviderError("provider unreachable")
        mock_get_provider.return_value = mock_provider

        response = self.client.post(reverse("ride_create"), self._ride_payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data["estimated_distance_km"])
        self.assertIsNone(response.data["estimated_duration_minutes"])
