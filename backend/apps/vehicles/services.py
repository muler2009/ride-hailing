from django.db import transaction

from apps.vehicles.models import Vehicle, VehicleDocument


@transaction.atomic
def register_vehicle(driver, *, vehicle_type, make, model, year, license_plate, color="") -> Vehicle:
    """
    Registers a new vehicle for a driver. The first vehicle a driver
    registers becomes their active one automatically; subsequent vehicles
    are registered inactive until explicitly activated via
    `set_active_vehicle`.
    """
    is_first_vehicle = not Vehicle.objects.filter(driver=driver).exists()
    vehicle = Vehicle.objects.create(
        driver=driver,
        vehicle_type=vehicle_type,
        make=make,
        model=model,
        year=year,
        color=color,
        license_plate=license_plate,
        is_active=is_first_vehicle,
    )
    return vehicle


@transaction.atomic
def set_active_vehicle(driver, vehicle: Vehicle) -> Vehicle:
    """
    Activates one vehicle for a driver, deactivating all others. This is the
    only path that may change `Vehicle.is_active` — it's a multi-row
    invariant ("exactly one active vehicle per driver") that a simple field
    default can't enforce on its own.
    """
    if vehicle.driver_id != driver.id:
        raise ValueError("Vehicle does not belong to this driver.")

    Vehicle.objects.filter(driver=driver).exclude(pk=vehicle.pk).update(is_active=False)
    vehicle.is_active = True
    vehicle.save(update_fields=["is_active"])
    return vehicle


def submit_vehicle_document(vehicle: Vehicle, *, document_type: str, file, expires_at=None) -> VehicleDocument:
    document, _created = VehicleDocument.objects.update_or_create(
        vehicle=vehicle,
        document_type=document_type,
        defaults={
            "file": file,
            "expires_at": expires_at,
            "status": "PENDING_REVIEW",
            "reviewed_by": None,
            "reviewed_at": None,
            "rejection_reason": "",
        },
    )
    return document
