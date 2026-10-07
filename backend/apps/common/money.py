"""
Shared Decimal-money helpers. Originally written as private details inside
apps/pricing/services.py (Phase 7); extracted here once apps/earnings
(Phase 9) needed the exact same quantization and float-boundary discipline
— duplicating it a second time would have been the wrong call once two
apps needed it identically.
"""
from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0")


def quantize_money(value) -> Decimal:
    """Quantizes to 2 decimal places using standard currency rounding (round-half-up)."""
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def as_decimal(value) -> Decimal:
    """
    Safely coerces a value to Decimal without ever constructing one
    directly from a raw float's binary representation. Going through
    `str()` first avoids exactly the imprecision the architecture doc's
    "never use floating point for money" rule exists to prevent — see
    Phase 7's README notes on Ride.estimated_distance_km for the concrete
    bug this guards against.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
