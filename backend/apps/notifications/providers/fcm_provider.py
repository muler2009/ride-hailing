"""
Real HTTP calls to Firebase Cloud Messaging's HTTP v1 API
(https://fcm.googleapis.com/v1/projects/{project}/messages:send).

Scoping note, matching this codebase's precedent for Telebirr (Phase 8):
FCM v1 requires a Bearer OAuth2 access token obtained by signing a JWT
with a Google service account's private key and exchanging it at Google's
token endpoint — a genuinely separate concern from *sending* a push once
you have a valid token. This provider does the latter (a real, correct
HTTP v1 call) and takes a pre-obtained access token from settings rather
than implementing service-account JWT signing itself. In production,
something else (a scheduled refresh job, or a library like `google-auth`)
is responsible for keeping FCM_ACCESS_TOKEN current; that's an
operational concern, not a code path this module owns.
"""
import requests
from django.conf import settings

from apps.notifications.providers.base import NotificationProviderError, PushProvider

REQUEST_TIMEOUT_SECONDS = 10


class FCMProvider(PushProvider):
    def __init__(self):
        self.project_id = settings.FCM_PROJECT_ID
        self.access_token = settings.FCM_ACCESS_TOKEN
        if not (self.project_id and self.access_token):
            raise NotificationProviderError("FCM is not fully configured (project id and access token required).")

    def send_push(self, *, device_token: str, title: str, body: str, data: dict = None) -> str:
        payload = {
            "message": {
                "token": device_token,
                "notification": {"title": title, "body": body},
            }
        }
        if data:
            payload["message"]["data"] = {k: str(v) for k, v in data.items()}

        response = requests.post(
            f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
            headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response_data = response.json()
        if response.status_code != 200:
            raise NotificationProviderError(f"FCM push failed: {response_data.get('error', response.text)}")

        return response_data.get("name", "")
