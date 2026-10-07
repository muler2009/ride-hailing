from django.core.management.base import BaseCommand

from apps.pricing.models import PricingRule
from apps.vehicles.models import VehicleType

PRICING_RULES = {
    "Moto": dict(base_fare="1.00", per_km_rate="0.30", per_minute_rate="0.05", per_minute_waiting_rate="0.05", minimum_fare="1.50", cancellation_fee="0.50"),
    "Sedan": dict(base_fare="2.00", per_km_rate="0.50", per_minute_rate="0.10", per_minute_waiting_rate="0.10", minimum_fare="3.00", cancellation_fee="1.00"),
    "SUV": dict(base_fare="3.00", per_km_rate="0.70", per_minute_rate="0.15", per_minute_waiting_rate="0.15", minimum_fare="4.50", cancellation_fee="1.50"),
    "Van": dict(base_fare="4.00", per_km_rate="0.90", per_minute_rate="0.20", per_minute_waiting_rate="0.20", minimum_fare="6.00", cancellation_fee="2.00"),
}


class Command(BaseCommand):
    help = "Seed default pricing rules for each vehicle type. Idempotent — safe to re-run."

    def handle(self, *args, **options):
        for name, rates in PRICING_RULES.items():
            try:
                vehicle_type = VehicleType.objects.get(name=name)
            except VehicleType.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"Skipping '{name}' — no such vehicle type."))
                continue
            _rule, created = PricingRule.objects.update_or_create(
                vehicle_type=vehicle_type,
                defaults={**rates, "currency": "USD", "surge_multiplier": "1.00", "tax_rate": "0.0000"},
            )
            self.stdout.write(f"{'Created' if created else 'Ensured'} pricing rule: {name}")
        self.stdout.write(self.style.SUCCESS("Pricing rule seed complete."))
