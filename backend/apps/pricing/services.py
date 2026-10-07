"""
The pricing engine. calculate_fare() is a pure function — Decimal in,
Decimal out, no database writes — deliberately separated from
create_fare_record()'s persistence so the arithmetic itself can be tested
without touching the DB, and so the exact same calculation logic backs
both a pre-ride ESTIMATE and a post-ride FINAL fare.

Every money value is Decimal from end to end. Each line item is quantized
to the currency's minor unit (2 decimal places here) at the moment it's
computed, and the total is the sum of those already-quantized line items
— never a separately-rounded number that could drift by a cent from what
the line items themselves add up to.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction

from apps.common.money import ZERO, as_decimal, quantize_money
from apps.pricing.models import Fare, FareLineItem, FareLineItemType, FareType, PricingRule


class PricingError(Exception):
    pass


# Local aliases retained so the rest of this file (and its docstrings/
# comments referencing them) didn't need touching when these moved to
# apps/common/money.py for reuse by apps/earnings (Phase 9).
_money = quantize_money
_as_decimal = as_decimal


@dataclass(frozen=True)
class FareLineItemData:
    item_type: str
    label: str
    amount: Decimal  # already quantized; negative for DISCOUNT


@dataclass(frozen=True)
class FareBreakdown:
    line_items: list = field(default_factory=list)
    total_amount: Decimal = ZERO
    currency: str = "USD"


def get_pricing_rule(vehicle_type) -> PricingRule:
    try:
        return vehicle_type.pricing_rule
    except PricingRule.DoesNotExist:
        raise PricingError(f"No pricing rule configured for vehicle type '{vehicle_type.name}'.")


def calculate_fare(
    *,
    pricing_rule: PricingRule,
    distance_km: Decimal,
    duration_minutes: Decimal,
    waiting_minutes: Decimal = ZERO,
    tolls: Decimal = ZERO,
    discount_amount: Decimal = ZERO,
) -> FareBreakdown:
    """
    Computes a full fare breakdown from a pricing rule and trip metrics.
    All numeric arguments must already be Decimal — this function never
    constructs a Decimal from a float, and callers (views/serializers)
    are responsible for that boundary using DRF's DecimalField, never
    FloatField, for anything money-related.

    Order of operations:
      1. base + distance + duration = the "metered" subtotal
      2. surge multiplies ONLY the metered subtotal (not waiting time or
         tolls — surge reflects trip-demand pricing on the ride itself)
      3. waiting time and tolls are added on top, unsurged
      4. if the running subtotal is below the rule's minimum fare, a
         visible MINIMUM_FARE_ADJUSTMENT line item makes up the
         difference — never a silent override
      5. tax is computed on that adjusted subtotal
      6. discount is subtracted last, capped so the total can't go negative
    """
    line_items: list[FareLineItemData] = []

    base_fare = _money(pricing_rule.base_fare)
    distance_fare = _money(pricing_rule.per_km_rate * distance_km)
    duration_fare = _money(pricing_rule.per_minute_rate * duration_minutes)
    line_items.append(FareLineItemData(FareLineItemType.BASE_FARE, "Base fare", base_fare))
    line_items.append(FareLineItemData(FareLineItemType.DISTANCE_FARE, "Distance", distance_fare))
    line_items.append(FareLineItemData(FareLineItemType.DURATION_FARE, "Duration", duration_fare))

    metered_subtotal = base_fare + distance_fare + duration_fare

    surge_multiplier = pricing_rule.surge_multiplier
    if surge_multiplier != 1:
        surge_amount = _money(metered_subtotal * (surge_multiplier - 1))
        if surge_amount != 0:
            line_items.append(
                FareLineItemData(
                    FareLineItemType.SURGE_ADJUSTMENT, f"Surge ({surge_multiplier}x)", surge_amount
                )
            )
    else:
        surge_amount = ZERO

    waiting_fare = _money(pricing_rule.per_minute_waiting_rate * waiting_minutes)
    if waiting_fare != 0:
        line_items.append(FareLineItemData(FareLineItemType.WAITING_FARE, "Waiting time", waiting_fare))

    tolls_amount = _money(tolls)
    if tolls_amount != 0:
        line_items.append(FareLineItemData(FareLineItemType.TOLLS, "Tolls", tolls_amount))

    running_subtotal = metered_subtotal + surge_amount + waiting_fare + tolls_amount

    minimum_fare = _money(pricing_rule.minimum_fare)
    minimum_fare_adjustment = _money(max(ZERO, minimum_fare - running_subtotal))
    if minimum_fare_adjustment != 0:
        line_items.append(
            FareLineItemData(
                FareLineItemType.MINIMUM_FARE_ADJUSTMENT, "Minimum fare adjustment", minimum_fare_adjustment
            )
        )

    adjusted_subtotal = running_subtotal + minimum_fare_adjustment

    tax_amount = _money(adjusted_subtotal * pricing_rule.tax_rate)
    if tax_amount != 0:
        line_items.append(FareLineItemData(FareLineItemType.TAX, "Tax", tax_amount))

    pre_discount_total = adjusted_subtotal + tax_amount

    discount = _money(discount_amount)
    discount_applied = min(discount, pre_discount_total)
    if discount_applied != 0:
        line_items.append(FareLineItemData(FareLineItemType.DISCOUNT, "Discount", -discount_applied))

    total_amount = pre_discount_total - discount_applied

    return FareBreakdown(line_items=line_items, total_amount=total_amount, currency=pricing_rule.currency)


@transaction.atomic
def create_fare_record(ride, fare_type: str, breakdown: FareBreakdown, *, distance_km: Decimal, duration_minutes: Decimal) -> Fare:
    """
    Persists a FareBreakdown. FINAL and CANCELLATION fares are immutable —
    calling this again for a ride that already has one raises rather than
    silently overwriting a record that may already be reflected in a
    payment or a receipt. ESTIMATE fares may be recalculated freely.
    """
    existing = Fare.objects.filter(ride=ride, fare_type=fare_type).first()
    if existing is not None:
        if fare_type != FareType.ESTIMATE:
            raise PricingError(f"A {fare_type} fare already exists for this ride and cannot be recalculated.")
        existing.delete()

    fare = Fare.objects.create(
        ride=ride,
        fare_type=fare_type,
        currency=breakdown.currency,
        distance_km=distance_km,
        duration_minutes=duration_minutes,
        total_amount=breakdown.total_amount,
    )
    FareLineItem.objects.bulk_create(
        [
            FareLineItem(
                fare=fare, item_type=item.item_type, label=item.label, amount=item.amount, sequence=index
            )
            for index, item in enumerate(breakdown.line_items)
        ]
    )
    return fare


def estimate_fare_for_ride(ride) -> Fare:
    """Called best-effort from RideCreateView once a route estimate exists (Phase 6)."""
    if ride.estimated_distance_km is None or ride.estimated_duration_minutes is None:
        raise PricingError("Ride has no route estimate to price against yet.")

    distance_km = _as_decimal(ride.estimated_distance_km)
    duration_minutes = _as_decimal(ride.estimated_duration_minutes)

    pricing_rule = get_pricing_rule(ride.vehicle_type)
    breakdown = calculate_fare(pricing_rule=pricing_rule, distance_km=distance_km, duration_minutes=duration_minutes)
    return create_fare_record(ride, FareType.ESTIMATE, breakdown, distance_km=distance_km, duration_minutes=duration_minutes)


def finalize_fare_for_ride(ride, *, distance_km=None, duration_minutes=None, waiting_minutes=ZERO, tolls=ZERO) -> Fare:
    """
    Called at trip completion. Without real trip telemetry (a future
    enhancement building on Phase 5's DriverLocation history — actual
    distance/duration traveled, not just requested), this falls back to
    the ride's original route estimate from Phase 6. A caller with real
    figures (e.g. computed from the location trail) can pass them in
    directly to override that fallback.
    """

    def _resolve(explicit, fallback):
        value = explicit if explicit is not None else fallback
        return _as_decimal(value) if value is not None else None

    distance_km = _resolve(distance_km, ride.estimated_distance_km)
    duration_minutes = _resolve(duration_minutes, ride.estimated_duration_minutes)
    if distance_km is None or duration_minutes is None:
        raise PricingError("No distance/duration available to calculate the final fare.")

    pricing_rule = get_pricing_rule(ride.vehicle_type)
    breakdown = calculate_fare(
        pricing_rule=pricing_rule,
        distance_km=distance_km,
        duration_minutes=duration_minutes,
        waiting_minutes=_as_decimal(waiting_minutes),
        tolls=_as_decimal(tolls),
    )
    return create_fare_record(ride, FareType.FINAL, breakdown, distance_km=distance_km, duration_minutes=duration_minutes)


def apply_cancellation_fee(ride) -> Fare:
    """Called when a rider cancels late enough (see apps/rides/services.py) to owe a flat fee."""
    pricing_rule = get_pricing_rule(ride.vehicle_type)
    fee = _money(pricing_rule.cancellation_fee)
    breakdown = FareBreakdown(
        line_items=[FareLineItemData(FareLineItemType.CANCELLATION_FEE, "Cancellation fee", fee)],
        total_amount=fee,
        currency=pricing_rule.currency,
    )
    return create_fare_record(ride, FareType.CANCELLATION, breakdown, distance_km=ZERO, duration_minutes=ZERO)
