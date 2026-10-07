"""
SMS and Push follow the same MapProvider/PaymentProvider pattern from
Phases 6 and 8: one interface, real implementations behind it, never
imported directly by calling code — only through apps/notifications/factory.py.

Email has no equivalent abstraction here — Django's own EMAIL_BACKEND
setting already solves "swap the email transport" (console for dev, SMTP
for production), so apps/notifications/services.py calls
django.core.mail.send_mail directly rather than reinventing that.
"""
from abc import ABC, abstractmethod
from typing import Optional


class NotificationProviderError(Exception):
    """Raised for any failure sending an SMS or push notification — network, auth, or a rejected message."""


class SMSProvider(ABC):
    @abstractmethod
    def send_sms(self, *, to: str, message: str) -> str:
        """Sends an SMS and returns the provider's message reference. Raises NotificationProviderError on failure."""


class PushProvider(ABC):
    @abstractmethod
    def send_push(self, *, device_token: str, title: str, body: str, data: Optional[dict] = None) -> str:
        """Sends a push notification and returns the provider's message reference."""
