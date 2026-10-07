from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.drivers.services import apply_as_driver, approve_driver, suspend_driver
from apps.notifications.factory import clear_provider_cache
from apps.notifications.models import Notification, NotificationChannel, NotificationStatus
from apps.notifications.providers.base import NotificationProviderError
from apps.notifications.providers.twilio_provider import TwilioProvider
from apps.notifications.services import NotificationError, deliver_notification, notify
from apps.rides.services import create_ride
from apps.users.models import User
from apps.vehicles.models import VehicleType


def _ride_kwargs(**overrides):
    base = dict(
        vehicle_type=VehicleType.objects.get(name="Sedan"),
        pickup_address="A", pickup_latitude="9.0300", pickup_longitude="38.7400",
        destination_address="B", destination_latitude="9.0100", destination_longitude="38.7600",
    )
    base.update(overrides)
    return base


class ChannelResolutionTests(TestCase):
    def test_guest_only_ever_gets_sms(self):
        ride = create_ride(guest_phone_number="+15551234567", **_ride_kwargs())
        notifications = notify(event_type="RIDE_DRIVER_ASSIGNED", title="t", body="b", guest_phone_number="+15551234567", ride=ride)
        channels = {n.channel for n in notifications}
        self.assertEqual(channels, {NotificationChannel.SMS})

    def test_registered_user_with_all_defaults_gets_in_app_email_sms(self):
        # push is excluded by default since no push_token is registered
        user = User.objects.create(email="chan-user@example.com", keycloak_id="kc-chan-user", phone_number="+15559990000")
        notifications = notify(event_type="RIDE_REQUESTED", title="t", body="b", recipient_user=user)
        channels = {n.channel for n in notifications}
        self.assertEqual(channels, {NotificationChannel.IN_APP, NotificationChannel.EMAIL, NotificationChannel.SMS})

    def test_push_included_once_a_token_is_registered(self):
        user = User.objects.create(email="chan-user2@example.com", keycloak_id="kc-chan-user2", push_token="device-token-123")
        notifications = notify(event_type="RIDE_REQUESTED", title="t", body="b", recipient_user=user)
        channels = {n.channel for n in notifications}
        self.assertIn(NotificationChannel.PUSH, channels)

    def test_disabled_preference_excludes_that_channel(self):
        user = User.objects.create(
            email="chan-user3@example.com", keycloak_id="kc-chan-user3",
            phone_number="+15559990001", notify_sms=False,
        )
        notifications = notify(event_type="RIDE_REQUESTED", title="t", body="b", recipient_user=user)
        channels = {n.channel for n in notifications}
        self.assertNotIn(NotificationChannel.SMS, channels)
        self.assertIn(NotificationChannel.IN_APP, channels)

    def test_requires_exactly_one_of_user_or_guest_phone(self):
        with self.assertRaises(NotificationError):
            notify(event_type="RIDE_REQUESTED", title="t", body="b")

    def test_cannot_give_both_user_and_guest_phone(self):
        user = User.objects.create(email="chan-user4@example.com", keycloak_id="kc-chan-user4")
        with self.assertRaises(NotificationError):
            notify(event_type="RIDE_REQUESTED", title="t", body="b", recipient_user=user, guest_phone_number="+15550000000")


class IdempotencyTests(TestCase):
    def test_calling_notify_twice_for_the_same_event_does_not_duplicate(self):
        ride = create_ride(guest_phone_number="+15551112222", **_ride_kwargs())
        first = notify(event_type="RIDE_DRIVER_ASSIGNED", title="t", body="b", guest_phone_number="+15551112222", ride=ride)
        second = notify(event_type="RIDE_DRIVER_ASSIGNED", title="t", body="b", guest_phone_number="+15551112222", ride=ride)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)  # already exists — no-op
        self.assertEqual(
            Notification.objects.filter(ride=ride, event_type="RIDE_DRIVER_ASSIGNED").count(), 1
        )

    def test_different_event_types_are_not_deduplicated_against_each_other(self):
        ride = create_ride(guest_phone_number="+15551112223", **_ride_kwargs())
        notify(event_type="RIDE_REQUESTED", title="t", body="b", guest_phone_number="+15551112223", ride=ride)
        notify(event_type="RIDE_DRIVER_ASSIGNED", title="t2", body="b2", guest_phone_number="+15551112223", ride=ride)

        self.assertEqual(Notification.objects.filter(ride=ride).count(), 2)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailDeliveryTests(TestCase):
    def test_email_is_actually_sent_via_locmem_backend(self):
        user = User.objects.create(email="deliver-email@example.com", keycloak_id="kc-deliver-email")
        notify(event_type="RIDE_REQUESTED", title="Hello", body="World", recipient_user=user)
        email_notification = Notification.objects.get(recipient_user=user, channel=NotificationChannel.EMAIL)

        deliver_notification(email_notification.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["deliver-email@example.com"])
        self.assertEqual(mail.outbox[0].subject, "Hello")

        email_notification.refresh_from_db()
        self.assertEqual(email_notification.status, NotificationStatus.SENT)
        self.assertIsNotNone(email_notification.sent_at)

    def test_in_app_notification_is_sent_with_no_external_call(self):
        user = User.objects.create(email="deliver-inapp@example.com", keycloak_id="kc-deliver-inapp")
        notify(event_type="RIDE_REQUESTED", title="Hi", body="There", recipient_user=user)
        in_app = Notification.objects.get(recipient_user=user, channel=NotificationChannel.IN_APP)

        deliver_notification(in_app.id)
        in_app.refresh_from_db()
        self.assertEqual(in_app.status, NotificationStatus.SENT)


class SMSDeliveryTests(TestCase):
    def setUp(self):
        clear_provider_cache()
        self.addCleanup(clear_provider_cache)

    @override_settings(TWILIO_ACCOUNT_SID="AC_test", TWILIO_AUTH_TOKEN="test_token", TWILIO_FROM_NUMBER="+15550001111")
    @mock.patch("apps.notifications.providers.twilio_provider.requests.post")
    def test_sms_delivery_marks_sent_on_success(self, mock_post):
        mock_post.return_value = mock.Mock(status_code=201, json=lambda: {"sid": "SM123"})

        ride = create_ride(guest_phone_number="+15559998888", **_ride_kwargs())
        [notification] = notify(event_type="RIDE_DRIVER_ASSIGNED", title="t", body="b", guest_phone_number="+15559998888", ride=ride)

        deliver_notification(notification.id)

        notification.refresh_from_db()
        self.assertEqual(notification.status, NotificationStatus.SENT)
        self.assertEqual(mock_post.call_args.kwargs["data"]["To"], "+15559998888")

    @override_settings(TWILIO_ACCOUNT_SID="AC_test", TWILIO_AUTH_TOKEN="test_token", TWILIO_FROM_NUMBER="+15550001111")
    @mock.patch("apps.notifications.providers.twilio_provider.requests.post")
    def test_sms_delivery_marks_failed_on_provider_error(self, mock_post):
        mock_post.return_value = mock.Mock(status_code=400, json=lambda: {"message": "Invalid number"})

        ride = create_ride(guest_phone_number="+15559998889", **_ride_kwargs())
        [notification] = notify(event_type="RIDE_DRIVER_ASSIGNED", title="t", body="b", guest_phone_number="+15559998889", ride=ride)

        deliver_notification(notification.id)

        notification.refresh_from_db()
        self.assertEqual(notification.status, NotificationStatus.FAILED)
        self.assertIn("Invalid number", notification.failure_reason)

    def test_unconfigured_twilio_raises_on_construction(self):
        with override_settings(TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="", TWILIO_FROM_NUMBER=""):
            with self.assertRaises(NotificationProviderError):
                TwilioProvider()

    def test_delivering_an_already_sent_notification_is_a_no_op(self):
        ride = create_ride(guest_phone_number="+15559998890", **_ride_kwargs())
        [notification] = notify(event_type="RIDE_DRIVER_ASSIGNED", title="t", body="b", guest_phone_number="+15559998890", ride=ride)
        notification.status = NotificationStatus.SENT
        notification.save()

        result = deliver_notification(notification.id)
        self.assertEqual(result.status, NotificationStatus.SENT)


class PushDeliveryTests(TestCase):
    def setUp(self):
        clear_provider_cache()
        self.addCleanup(clear_provider_cache)

    @override_settings(FCM_PROJECT_ID="test-project", FCM_ACCESS_TOKEN="test-access-token")
    @mock.patch("apps.notifications.providers.fcm_provider.requests.post")
    def test_push_delivery_marks_sent_on_success(self, mock_post):
        mock_post.return_value = mock.Mock(status_code=200, json=lambda: {"name": "projects/test/messages/1"})

        user = User.objects.create(email="push-user@example.com", keycloak_id="kc-push-user", push_token="device-abc")
        notify(event_type="RIDE_REQUESTED", title="t", body="b", recipient_user=user)
        push_notification = Notification.objects.get(recipient_user=user, channel=NotificationChannel.PUSH)

        deliver_notification(push_notification.id)

        push_notification.refresh_from_db()
        self.assertEqual(push_notification.status, NotificationStatus.SENT)
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["message"]["token"], "device-abc")


class RideEventNotificationIntegrationTests(TestCase):
    """Confirms the actual hooks wired into apps/rides, apps/drivers, apps/payments fire correctly."""

    def test_creating_a_ride_fires_ride_requested(self):
        ride = create_ride(guest_phone_number="+15557778888", **_ride_kwargs())
        self.assertTrue(
            Notification.objects.filter(ride=ride, event_type="RIDE_REQUESTED", channel=NotificationChannel.SMS).exists()
        )

    def test_approving_a_driver_fires_driver_approved(self):
        user = User.objects.create(email="notify-driver@example.com", keycloak_id="kc-notify-driver")
        admin = User.objects.create(email="notify-admin@example.com", keycloak_id="kc-notify-admin")
        driver = apply_as_driver(user, license_number="D1", license_expiry="2030-01-01")

        approve_driver(driver, admin)

        self.assertTrue(
            Notification.objects.filter(recipient_user=user, event_type="DRIVER_APPROVED", channel=NotificationChannel.IN_APP).exists()
        )

    def test_suspending_a_driver_fires_driver_suspended(self):
        user = User.objects.create(email="notify-driver2@example.com", keycloak_id="kc-notify-driver2")
        admin = User.objects.create(email="notify-admin2@example.com", keycloak_id="kc-notify-admin2")
        driver = apply_as_driver(user, license_number="D2", license_expiry="2030-01-01")
        approve_driver(driver, admin)

        suspend_driver(driver, admin, "policy violation")

        self.assertTrue(
            Notification.objects.filter(recipient_user=user, event_type="DRIVER_SUSPENDED").exists()
        )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class CeleryEagerIntegrationTests(TestCase):
    """
    With CELERY_TASK_ALWAYS_EAGER, .delay() runs the task synchronously
    in-process — no worker needed — proving notify() -> the Celery task ->
    deliver_notification() actually connects end to end, not just that
    each piece works in isolation.
    """

    def test_notify_actually_results_in_a_sent_in_app_notification(self):
        user = User.objects.create(email="eager-user@example.com", keycloak_id="kc-eager-user")
        notify(event_type="RIDE_REQUESTED", title="t", body="b", recipient_user=user)

        notification = Notification.objects.get(recipient_user=user, channel=NotificationChannel.IN_APP)
        self.assertEqual(notification.status, NotificationStatus.SENT)


class NotificationApiTests(APITestCase):
    def test_user_sees_own_in_app_notifications(self):
        user = User.objects.create(email="api-notify@example.com", keycloak_id="kc-api-notify")
        notify(event_type="RIDE_REQUESTED", title="Hello", body="World", recipient_user=user)

        self.client.force_authenticate(user=user)
        response = self.client.get(reverse("my_notifications"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["title"], "Hello")

    def test_user_only_sees_their_own_notifications(self):
        user = User.objects.create(email="api-notify2@example.com", keycloak_id="kc-api-notify2")
        other = User.objects.create(email="api-notify3@example.com", keycloak_id="kc-api-notify3")
        notify(event_type="RIDE_REQUESTED", title="Hello", body="World", recipient_user=other)

        self.client.force_authenticate(user=user)
        response = self.client.get(reverse("my_notifications"))
        self.assertEqual(len(response.data), 0)

    def test_can_register_push_token(self):
        user = User.objects.create(email="api-push@example.com", keycloak_id="kc-api-push")
        self.client.force_authenticate(user=user)

        response = self.client.post(reverse("push_token"), {"push_token": "new-device-token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        user.refresh_from_db()
        self.assertEqual(user.push_token, "new-device-token")

    def test_can_update_notification_preferences(self):
        user = User.objects.create(email="api-prefs@example.com", keycloak_id="kc-api-prefs")
        self.client.force_authenticate(user=user)

        response = self.client.patch(reverse("notification_preferences"), {"notify_sms": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["notify_sms"])
        self.assertTrue(response.data["notify_email"])  # unchanged

        user.refresh_from_db()
        self.assertFalse(user.notify_sms)
