from functools import lru_cache

from apps.notifications.providers.base import NotificationProviderError, PushProvider, SMSProvider


def _build_twilio():
    from apps.notifications.providers.twilio_provider import TwilioProvider

    return TwilioProvider()


def _build_fcm():
    from apps.notifications.providers.fcm_provider import FCMProvider

    return FCMProvider()


_SMS_PROVIDER_BUILDERS = {"twilio": _build_twilio}
_PUSH_PROVIDER_BUILDERS = {"fcm": _build_fcm}


@lru_cache(maxsize=1)
def get_sms_provider() -> SMSProvider:
    from django.conf import settings

    builder = _SMS_PROVIDER_BUILDERS.get(settings.SMS_PROVIDER)
    if builder is None:
        raise NotificationProviderError(f"Unknown SMS provider '{settings.SMS_PROVIDER}'.")
    return builder()


@lru_cache(maxsize=1)
def get_push_provider() -> PushProvider:
    from django.conf import settings

    builder = _PUSH_PROVIDER_BUILDERS.get(settings.PUSH_PROVIDER)
    if builder is None:
        raise NotificationProviderError(f"Unknown push provider '{settings.PUSH_PROVIDER}'.")
    return builder()


def clear_provider_cache() -> None:
    """Used by tests when provider settings change between test runs."""
    get_sms_provider.cache_clear()
    get_push_provider.cache_clear()
