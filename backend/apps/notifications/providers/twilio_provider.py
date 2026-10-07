"""
Real HTTP calls to Twilio's REST API. Same testability note as every
other provider in this codebase: this sandbox can't reach Twilio's API,
so apps/notifications/tests.py mocks the `requests` call and verifies
request construction and response parsing instead.
"""
import requests
from django.conf import settings

from apps.notifications.providers.base import NotificationProviderError, SMSProvider

REQUEST_TIMEOUT_SECONDS = 10


class TwilioProvider(SMSProvider):
    def __init__(self):
        self.account_sid = settings.TWILIO_ACCOUNT_SID
        self.auth_token = settings.TWILIO_AUTH_TOKEN
        self.from_number = settings.TWILIO_FROM_NUMBER
        if not (self.account_sid and self.auth_token and self.from_number):
            raise NotificationProviderError("Twilio is not fully configured (SID, auth token, and from-number required).")

    def send_sms(self, *, to: str, message: str) -> str:
        response = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json",
            auth=(self.account_sid, self.auth_token),
            data={"From": self.from_number, "To": to, "Body": message},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = response.json()
        if response.status_code not in (200, 201):
            raise NotificationProviderError(f"Twilio SMS failed: {data.get('message', response.text)}")

        return data["sid"]
