from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.drivers.services import apply_as_driver, approve_driver
from apps.identity.services import assign_role
from apps.users.models import User
from apps.vehicles.models import VehicleType


def _ride_payload(**overrides):
    sedan = VehicleType.objects.get(name="Sedan")
    base = {
        "vehicle_type_id": str(sedan.id),
        "pickup_address": "123 Main St",
        "pickup_latitude": "9.0300",
        "pickup_longitude": "38.7400",
        "destination_address": "456 Side St",
        "destination_latitude": "9.0100",
        "destination_longitude": "38.7600",
    }
    base.update(overrides)
    return base


class GuestRideApiTests(APITestCase):
    """The core of this feature: no account required to request a ride."""

    def test_guest_can_create_a_ride_with_just_a_phone_number(self):
        response = self.client.post(
            reverse("ride_create"),
            _ride_payload(guest_phone_number="+15551234567", guest_name="Sam"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "SEARCHING_DRIVER")
        self.assertIsNone(response.data["rider_email"])
        self.assertIsNotNone(response.data["tracking_token"])

    def test_guest_creation_without_a_phone_number_is_rejected(self):
        response = self.client.post(reverse("ride_create"), _ride_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_guest_creation_with_invalid_phone_format_is_rejected(self):
        response = self.client.post(
            reverse("ride_create"), _ride_payload(guest_phone_number="not-a-phone"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_guest_can_track_their_ride_by_token(self):
        create_response = self.client.post(
            reverse("ride_create"), _ride_payload(guest_phone_number="+15551234567"), format="json"
        )
        token = create_response.data["tracking_token"]

        track_response = self.client.get(reverse("guest_ride_track", args=[token]))
        self.assertEqual(track_response.status_code, status.HTTP_200_OK)
        self.assertEqual(track_response.data["status"], "SEARCHING_DRIVER")

    def test_tracking_with_a_wrong_token_returns_404(self):
        import uuid

        response = self.client.get(reverse("guest_ride_track", args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_guest_can_cancel_their_own_ride_by_token(self):
        create_response = self.client.post(
            reverse("ride_create"), _ride_payload(guest_phone_number="+15551234567"), format="json"
        )
        token = create_response.data["tracking_token"]

        cancel_response = self.client.post(
            reverse("guest_ride_cancel", args=[token]), {"reason": "no longer needed"}, format="json"
        )
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        self.assertEqual(cancel_response.data["status"], "CANCELLED_BY_RIDER")

    def test_tracking_token_is_not_leaked_to_non_owners(self):
        """
        The registered-rider detail view and admin/driver views must never
        surface tracking_token for a ride they don't own — it's a guest's
        bearer credential, not general ride metadata.
        """
        create_response = self.client.post(
            reverse("ride_create"), _ride_payload(guest_phone_number="+15551234567"), format="json"
        )
        ride_id = create_response.data  # guest response includes it, that's fine (they own it)
        self.assertIsNotNone(ride_id["tracking_token"])

        admin = User.objects.create(email="admin-view@example.com", keycloak_id="kc-admin-view")
        assign_role(admin, "Operations Manager")
        self.client.force_authenticate(user=admin)

        list_response = self.client.get(reverse("admin_ride_list"))
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        for ride in list_response.data["results"]:
            self.assertIsNone(ride["tracking_token"])


class RegisteredRiderRideApiTests(APITestCase):
    def setUp(self):
        self.rider = User.objects.create(email="rider@example.com", keycloak_id="kc-rider-api")
        assign_role(self.rider, "Rider")
        self.client.force_authenticate(user=self.rider)

    def test_registered_rider_can_create_a_ride_without_a_phone_number(self):
        response = self.client.post(reverse("ride_create"), _ride_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["rider_email"], "rider@example.com")

    def test_guest_fields_are_ignored_when_authenticated(self):
        response = self.client.post(
            reverse("ride_create"),
            _ride_payload(guest_phone_number="+19998887777"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["rider_email"], "rider@example.com")

    def test_rider_sees_ride_in_their_own_list(self):
        self.client.post(reverse("ride_create"), _ride_payload(), format="json")
        response = self.client.get(reverse("my_rides"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_authenticated_user_without_ride_request_permission_is_denied(self):
        driver_user = User.objects.create(email="notrider@example.com", keycloak_id="kc-notrider")
        assign_role(driver_user, "Driver")
        self.client.force_authenticate(user=driver_user)

        response = self.client.post(reverse("ride_create"), _ride_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_rider_can_cancel_their_own_ride(self):
        create_response = self.client.post(reverse("ride_create"), _ride_payload(), format="json")
        ride_id = create_response.data["id"]

        response = self.client.post(reverse("ride_cancel", args=[ride_id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "CANCELLED_BY_RIDER")


class DriverRideActionApiTests(APITestCase):
    def setUp(self):
        self.rider = User.objects.create(email="driveraction-rider@example.com", keycloak_id="kc-da-rider")
        assign_role(self.rider, "Rider")

        self.admin = User.objects.create(email="driveraction-admin@example.com", keycloak_id="kc-da-admin")
        assign_role(self.admin, "Operations Manager")

        driver_user = User.objects.create(email="driveraction-driver@example.com", keycloak_id="kc-da-driver")
        assign_role(driver_user, "Driver")
        self.driver = apply_as_driver(driver_user, license_number="D1", license_expiry="2030-01-01")
        approve_driver(self.driver, self.admin)
        self.driver_user = driver_user

        self.client.force_authenticate(user=self.rider)
        create_response = self.client.post(reverse("ride_create"), _ride_payload(), format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)

        from apps.rides.models import Ride

        self.ride = Ride.objects.get(rider=self.rider)

    def test_ops_manager_can_manually_assign_a_driver(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            reverse("admin_ride_assign_driver", args=[self.ride.id]),
            {"driver_id": str(self.driver.id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "DRIVER_ASSIGNED")

    def test_rider_cannot_assign_a_driver(self):
        self.client.force_authenticate(user=self.rider)
        response = self.client.post(
            reverse("admin_ride_assign_driver", args=[self.ride.id]),
            {"driver_id": str(self.driver.id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_driver_can_accept_after_being_assigned(self):
        self.client.force_authenticate(user=self.admin)
        self.client.post(
            reverse("admin_ride_assign_driver", args=[self.ride.id]),
            {"driver_id": str(self.driver.id)},
            format="json",
        )

        self.client.force_authenticate(user=self.driver_user)
        response = self.client.post(reverse("ride_accept", args=[self.ride.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "DRIVER_ACCEPTED")

    def test_unassigned_driver_cannot_accept(self):
        self.client.force_authenticate(user=self.driver_user)
        response = self.client.post(reverse("ride_accept", args=[self.ride.id]))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_full_driver_lifecycle_over_the_api(self):
        self.client.force_authenticate(user=self.admin)
        self.client.post(
            reverse("admin_ride_assign_driver", args=[self.ride.id]),
            {"driver_id": str(self.driver.id)},
            format="json",
        )

        self.client.force_authenticate(user=self.driver_user)
        self.assertEqual(self.client.post(reverse("ride_accept", args=[self.ride.id])).status_code, 200)
        self.assertEqual(self.client.post(reverse("ride_arriving", args=[self.ride.id])).status_code, 200)
        self.assertEqual(self.client.post(reverse("ride_arrived", args=[self.ride.id])).status_code, 200)
        self.assertEqual(self.client.post(reverse("ride_start", args=[self.ride.id])).status_code, 200)
        complete_response = self.client.post(reverse("ride_complete", args=[self.ride.id]))
        self.assertEqual(complete_response.status_code, 200)
        self.assertEqual(complete_response.data["status"], "PAYMENT_PENDING")

    def test_non_driver_cannot_call_driver_action_endpoints(self):
        self.client.force_authenticate(user=self.rider)
        response = self.client.post(reverse("ride_accept", args=[self.ride.id]))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_support_agent_can_view_but_not_assign(self):
        support = User.objects.create(email="support@example.com", keycloak_id="kc-support")
        assign_role(support, "Support Agent")
        self.client.force_authenticate(user=support)

        detail_response = self.client.get(reverse("ride_detail", args=[self.ride.id]))
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)

        assign_response = self.client.post(
            reverse("admin_ride_assign_driver", args=[self.ride.id]),
            {"driver_id": str(self.driver.id)},
            format="json",
        )
        self.assertEqual(assign_response.status_code, status.HTTP_403_FORBIDDEN)
