from django.db import migrations

# Illustrative defaults, not real-world pricing — an operator adjusts
# these via PUT /api/v1/pricing-rules/{vehicle_type_id}/ (requires
# `pricing.manage`) once real rates are known.
PRICING_RULES = {
    "Moto": dict(base_fare="1.00", per_km_rate="0.30", per_minute_rate="0.05", per_minute_waiting_rate="0.05", minimum_fare="1.50", cancellation_fee="0.50"),
    "Sedan": dict(base_fare="2.00", per_km_rate="0.50", per_minute_rate="0.10", per_minute_waiting_rate="0.10", minimum_fare="3.00", cancellation_fee="1.00"),
    "SUV": dict(base_fare="3.00", per_km_rate="0.70", per_minute_rate="0.15", per_minute_waiting_rate="0.15", minimum_fare="4.50", cancellation_fee="1.50"),
    "Van": dict(base_fare="4.00", per_km_rate="0.90", per_minute_rate="0.20", per_minute_waiting_rate="0.20", minimum_fare="6.00", cancellation_fee="2.00"),
}


def seed_pricing_rules(apps, schema_editor):
    VehicleType = apps.get_model("vehicles", "VehicleType")
    PricingRule = apps.get_model("pricing", "PricingRule")

    for name, rates in PRICING_RULES.items():
        try:
            vehicle_type = VehicleType.objects.get(name=name)
        except VehicleType.DoesNotExist:
            continue  # vehicles app's own seed migration didn't create this type; skip gracefully
        PricingRule.objects.update_or_create(
            vehicle_type=vehicle_type,
            defaults={**rates, "currency": "USD", "surge_multiplier": "1.00", "tax_rate": "0.0000"},
        )


def unseed_pricing_rules(apps, schema_editor):
    PricingRule = apps.get_model("pricing", "PricingRule")
    PricingRule.objects.filter(vehicle_type__name__in=PRICING_RULES.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("pricing", "0001_initial"),
        ("vehicles", "0002_seed_vehicle_types"),
    ]

    operations = [
        migrations.RunPython(seed_pricing_rules, unseed_pricing_rules),
    ]
