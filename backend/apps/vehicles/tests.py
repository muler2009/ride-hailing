import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.drivers.services import apply_as_driver
from apps.users.models import User
from apps.vehicles.models import Vehicle, VehicleType
from apps.vehicles.services import register_vehicle, set_active_vehicle


def _fake_file(name="registration.pdf"):
    return SimpleUploadedFile(name, io.BytesIO(b"fake file contents").read(), content_type="application/pdf")


class VehicleTypeSeedTests(APITestCase):
    def test_default_vehicle_types_are_seeded(self):
        names = set(VehicleType.objects.values_list("name", flat=True))
        self.assertEqual(names, {"Moto", "Sedan", "SUV", "Van"})

    def test_vehicle_type_list_endpoint_is_public(self):
        response = self.client.get(reverse("vehicle_type_list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 4)


class VehicleServiceTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create(email="vsvc@example.com", keycloak_id="kc-vsvc")
        self.driver = apply_as_driver(self.user, license_number="D1", license_expiry="2030-01-01")
        self.sedan = VehicleType.objects.get(name="Sedan")
        self.suv = VehicleType.objects.get(name="SUV")

    def test_first_vehicle_is_active_by_default(self):
        vehicle = register_vehicle(
            self.driver,
            vehicle_type=self.sedan,
            make="Toyota",
            model="Corolla",
            year=2022,
            license_plate="ABC-123",
        )
        self.assertTrue(vehicle.is_active)

    def test_second_vehicle_is_inactive_until_activated(self):
        first = register_vehicle(
            self.driver, vehicle_type=self.sedan, make="Toyota", model="Corolla", year=2022, license_plate="ABC-123"
        )
        second = register_vehicle(
            self.driver, vehicle_type=self.suv, make="Honda", model="CR-V", year=2023, license_plate="XYZ-999"
        )

        self.assertTrue(Vehicle.objects.get(pk=first.pk).is_active)
        self.assertFalse(Vehicle.objects.get(pk=second.pk).is_active)

    def test_activating_a_vehicle_deactivates_the_others(self):
        first = register_vehicle(
            self.driver, vehicle_type=self.sedan, make="Toyota", model="Corolla", year=2022, license_plate="ABC-123"
        )
        second = register_vehicle(
            self.driver, vehicle_type=self.suv, make="Honda", model="CR-V", year=2023, license_plate="XYZ-999"
        )

        set_active_vehicle(self.driver, second)

        self.assertFalse(Vehicle.objects.get(pk=first.pk).is_active)
        self.assertTrue(Vehicle.objects.get(pk=second.pk).is_active)

    def test_cannot_activate_another_drivers_vehicle(self):
        other_user = User.objects.create(email="other@example.com", keycloak_id="kc-other")
        other_driver = apply_as_driver(other_user, license_number="D2", license_expiry="2030-01-01")
        other_vehicle = register_vehicle(
            other_driver, vehicle_type=self.sedan, make="Ford", model="Focus", year=2021, license_plate="OTHER-1"
        )

        with self.assertRaises(ValueError):
            set_active_vehicle(self.driver, other_vehicle)


class VehicleApiTests(APITestCase):
    def setUp(self):
        self.driver_user = User.objects.create(email="vapi@example.com", keycloak_id="kc-vapi")
        self.driver = apply_as_driver(self.driver_user, license_number="D1", license_expiry="2030-01-01")
        self.sedan = VehicleType.objects.get(name="Sedan")

        self.non_driver_user = User.objects.create(email="notdriver@example.com", keycloak_id="kc-nd")

        self.client.force_authenticate(user=self.driver_user)

    def test_driver_can_register_a_vehicle(self):
        response = self.client.post(
            reverse("my_vehicles"),
            {
                "vehicle_type_id": str(self.sedan.id),
                "make": "Toyota",
                "model": "Camry",
                "year": 2021,
                "license_plate": "NEW-001",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_active"])

    def test_duplicate_license_plate_is_rejected(self):
        register_vehicle(
            self.driver, vehicle_type=self.sedan, make="Toyota", model="Camry", year=2021, license_plate="DUP-1"
        )
        response = self.client.post(
            reverse("my_vehicles"),
            {
                "vehicle_type_id": str(self.sedan.id),
                "make": "Honda",
                "model": "Civic",
                "year": 2020,
                "license_plate": "DUP-1",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_driver_cannot_register_a_vehicle(self):
        self.client.force_authenticate(user=self.non_driver_user)
        response = self.client.post(
            reverse("my_vehicles"),
            {
                "vehicle_type_id": str(self.sedan.id),
                "make": "Toyota",
                "model": "Camry",
                "year": 2021,
                "license_plate": "NOPE-1",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_activate_endpoint_switches_active_vehicle(self):
        first = register_vehicle(
            self.driver, vehicle_type=self.sedan, make="Toyota", model="Camry", year=2021, license_plate="ACT-1"
        )
        second = register_vehicle(
            self.driver, vehicle_type=self.sedan, make="Honda", model="Civic", year=2020, license_plate="ACT-2"
        )

        response = self.client.post(reverse("vehicle_activate", args=[second.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_active"])
        self.assertFalse(Vehicle.objects.get(pk=first.pk).is_active)

    def test_upload_vehicle_document(self):
        vehicle = register_vehicle(
            self.driver, vehicle_type=self.sedan, make="Toyota", model="Camry", year=2021, license_plate="DOC-1"
        )
        response = self.client.post(
            reverse("vehicle_document_upload", args=[vehicle.id]),
            {"document_type": "REGISTRATION", "file": _fake_file()},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "PENDING_REVIEW")

    def test_cannot_upload_document_for_another_drivers_vehicle(self):
        other_user = User.objects.create(email="otherdriver@example.com", keycloak_id="kc-od")
        other_driver = apply_as_driver(other_user, license_number="D9", license_expiry="2030-01-01")
        other_vehicle = register_vehicle(
            other_driver, vehicle_type=self.sedan, make="Ford", model="Focus", year=2019, license_plate="OTHER-2"
        )

        response = self.client.post(
            reverse("vehicle_document_upload", args=[other_vehicle.id]),
            {"document_type": "REGISTRATION", "file": _fake_file()},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
