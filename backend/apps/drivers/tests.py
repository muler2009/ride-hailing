import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.drivers.models import Driver, DriverApprovalStatus
from apps.drivers.services import (
    InvalidDriverTransition,
    apply_as_driver,
    approve_driver,
    reactivate_driver,
    reject_driver,
    suspend_driver,
)
from apps.identity.services import assign_role
from apps.users.models import User


def _fake_file(name="license.pdf"):
    return SimpleUploadedFile(name, io.BytesIO(b"fake file contents").read(), content_type="application/pdf")


class DriverServiceTests(APITestCase):
    """Unit tests for the approval-state transition table, independent of the API layer."""

    def setUp(self):
        self.user = User.objects.create(email="svc-driver@example.com", keycloak_id="kc-svc-1")
        self.driver = apply_as_driver(
            self.user, license_number="D12345", license_expiry="2030-01-01"
        )
        self.admin = User.objects.create(email="svc-admin@example.com", keycloak_id="kc-svc-admin")

    def test_new_application_starts_pending_review(self):
        self.assertEqual(self.driver.approval_status, DriverApprovalStatus.PENDING_REVIEW)

    def test_approve_transitions_to_approved(self):
        driver = approve_driver(self.driver, self.admin)
        self.assertEqual(driver.approval_status, DriverApprovalStatus.APPROVED)
        self.assertEqual(driver.reviewed_by, self.admin)
        self.assertTrue(driver.is_approved)

    def test_reject_transitions_to_rejected_with_reason(self):
        driver = reject_driver(self.driver, self.admin, "Expired license")
        self.assertEqual(driver.approval_status, DriverApprovalStatus.REJECTED)
        self.assertEqual(driver.rejection_reason, "Expired license")

    def test_cannot_approve_an_already_approved_driver(self):
        approve_driver(self.driver, self.admin)
        with self.assertRaises(InvalidDriverTransition):
            approve_driver(self.driver, self.admin)

    def test_cannot_suspend_a_pending_driver(self):
        with self.assertRaises(InvalidDriverTransition):
            suspend_driver(self.driver, self.admin, "not approved yet")

    def test_suspend_then_reactivate(self):
        approve_driver(self.driver, self.admin)
        suspend_driver(self.driver, self.admin, "customer complaints")
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.approval_status, DriverApprovalStatus.SUSPENDED)

        reactivate_driver(self.driver, self.admin)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.approval_status, DriverApprovalStatus.APPROVED)

    def test_resubmitting_after_rejection_returns_to_pending_review(self):
        reject_driver(self.driver, self.admin, "bad photo")
        updated = apply_as_driver(self.user, license_number="D12345-B", license_expiry="2031-01-01")
        self.assertEqual(updated.approval_status, DriverApprovalStatus.PENDING_REVIEW)
        self.assertEqual(updated.license_number, "D12345-B")


class DriverApiTests(APITestCase):
    def setUp(self):
        self.rider = User.objects.create(email="rider@example.com", keycloak_id="kc-rider")
        assign_role(self.rider, "Rider")

        self.driver_user = User.objects.create(email="driver@example.com", keycloak_id="kc-driver")
        assign_role(self.driver_user, "Driver")

        self.ops_admin = User.objects.create(email="ops@example.com", keycloak_id="kc-ops")
        assign_role(self.ops_admin, "Operations Manager")

    def _as(self, user):
        self.client.force_authenticate(user=user)
        return self.client

    def test_driver_can_submit_application(self):
        client = self._as(self.driver_user)
        response = client.post(
            reverse("my_driver_profile"),
            {"license_number": "D999", "license_expiry": "2030-06-01"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["approval_status"], "PENDING_REVIEW")
        self.assertTrue(Driver.objects.filter(user=self.driver_user).exists())

    def test_get_profile_404_when_no_application(self):
        client = self._as(self.driver_user)
        response = client.get(reverse("my_driver_profile"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_driver_can_upload_document(self):
        client = self._as(self.driver_user)
        client.post(
            reverse("my_driver_profile"),
            {"license_number": "D999", "license_expiry": "2030-06-01"},
            format="json",
        )

        response = client.post(
            reverse("driver_document_upload"),
            {"document_type": "DRIVERS_LICENSE", "file": _fake_file()},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "PENDING_REVIEW")

    def test_document_upload_without_application_is_rejected(self):
        client = self._as(self.driver_user)
        response = client.post(
            reverse("driver_document_upload"),
            {"document_type": "DRIVERS_LICENSE", "file": _fake_file()},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rider_cannot_view_admin_driver_queue(self):
        client = self._as(self.rider)
        response = client.get(reverse("admin_driver_list"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_ops_manager_can_approve_a_driver(self):
        driver = apply_as_driver(self.driver_user, license_number="D1", license_expiry="2030-01-01")

        client = self._as(self.ops_admin)
        response = client.post(reverse("admin_driver_approve", args=[driver.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["approval_status"], "APPROVED")

    def test_ops_manager_can_reject_a_driver_with_reason(self):
        driver = apply_as_driver(self.driver_user, license_number="D1", license_expiry="2030-01-01")

        client = self._as(self.ops_admin)
        response = client.post(
            reverse("admin_driver_reject", args=[driver.id]), {"reason": "Invalid license"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["approval_status"], "REJECTED")
        self.assertEqual(response.data["rejection_reason"], "Invalid license")

    def test_invalid_transition_returns_400_not_500(self):
        driver = apply_as_driver(self.driver_user, license_number="D1", license_expiry="2030-01-01")

        client = self._as(self.ops_admin)
        # Suspending a driver still PENDING_REVIEW is not a valid transition.
        response = client.post(
            reverse("admin_driver_suspend", args=[driver.id]), {"reason": "x"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_driver_queue_filters_by_status(self):
        approved_driver = apply_as_driver(self.driver_user, license_number="D1", license_expiry="2030-01-01")
        approve_driver(approved_driver, self.ops_admin)

        other_user = User.objects.create(email="driver2@example.com", keycloak_id="kc-driver2")
        pending_driver = apply_as_driver(other_user, license_number="D2", license_expiry="2030-01-01")

        client = self._as(self.ops_admin)
        response = client.get(reverse("admin_driver_list"), {"status": "pending_review"})

        emails = [d["email"] for d in response.data["results"]]
        self.assertIn(pending_driver.user.email, emails)
        self.assertNotIn(approved_driver.user.email, emails)
