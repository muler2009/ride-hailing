from django.core.management.base import BaseCommand

from apps.vehicles.models import VehicleType

VEHICLE_TYPES = [
    ("Moto", "Motorcycle — single passenger, no luggage capacity.", 1),
    ("Sedan", "Standard 4-door sedan.", 4),
    ("SUV", "Larger vehicle for groups or extra luggage.", 6),
    ("Van", "High-capacity vehicle for group rides.", 8),
]


class Command(BaseCommand):
    help = "Seed default vehicle types."

    def handle(self, *args, **options):
        for name, description, capacity in VEHICLE_TYPES:
            _obj, created = VehicleType.objects.update_or_create(
                name=name,
                defaults={"description": description, "passenger_capacity": capacity, "is_active": True},
            )
            self.stdout.write(f"{'Created' if created else 'Ensured'} vehicle type: {name}")
        self.stdout.write(self.style.SUCCESS("Vehicle type seed complete."))
