from django.db import migrations

VEHICLE_TYPES = [
    ("Moto", "Motorcycle — single passenger, no luggage capacity.", 1),
    ("Sedan", "Standard 4-door sedan.", 4),
    ("SUV", "Larger vehicle for groups or extra luggage.", 6),
    ("Van", "High-capacity vehicle for group rides.", 8),
]


def seed_vehicle_types(apps, schema_editor):
    VehicleType = apps.get_model("vehicles", "VehicleType")
    for name, description, capacity in VEHICLE_TYPES:
        VehicleType.objects.update_or_create(
            name=name,
            defaults={"description": description, "passenger_capacity": capacity, "is_active": True},
        )


def unseed_vehicle_types(apps, schema_editor):
    VehicleType = apps.get_model("vehicles", "VehicleType")
    VehicleType.objects.filter(name__in=[n for n, _, _ in VEHICLE_TYPES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vehicles", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_vehicle_types, unseed_vehicle_types),
    ]
