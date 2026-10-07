from functools import lru_cache

from apps.payments.providers.base import PaymentProvider, PaymentProviderError


def _build_stripe():
    from apps.payments.providers.stripe_provider import StripeProvider

    return StripeProvider()


def _build_chapa():
    from apps.payments.providers.chapa_provider import ChapaProvider

    return ChapaProvider()


_PROVIDER_BUILDERS = {
    "stripe": _build_stripe,
    "chapa": _build_chapa,
    # "telebirr": _build_telebirr,  # add providers/telebirr.py implementing PaymentProvider and register it here
}


@lru_cache(maxsize=None)
def get_payment_provider(provider_name: str) -> PaymentProvider:
    """
    Unlike apps.maps's single globally-configured provider, payment
    provider selection is per-transaction (a rider picks "pay by card" or
    a region defaults to a particular gateway) — so this takes an explicit
    name rather than reading one setting.
    """
    builder = _PROVIDER_BUILDERS.get(provider_name)
    if builder is None:
        raise PaymentProviderError(
            f"Unknown payment provider '{provider_name}'. Available: {sorted(_PROVIDER_BUILDERS)}"
        )
    return builder()


def clear_provider_cache() -> None:
    """Used by tests when provider settings change between test runs."""
    get_payment_provider.cache_clear()
