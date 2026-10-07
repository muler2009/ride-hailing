from django.core.management.base import BaseCommand

from apps.dispatch.services import run_dispatch_cycle


class Command(BaseCommand):
    help = (
        "Runs one dispatch cycle: expires overdue offers, dispatches/redispatches "
        "SEARCHING_DRIVER rides, and marks rides that have exhausted their search "
        "window as NO_DRIVER_FOUND. In production this runs on a schedule (e.g. "
        "Celery beat every 5-10 seconds); for now, run it manually or via cron."
    )

    def handle(self, *args, **options):
        results = run_dispatch_cycle()
        self.stdout.write(
            self.style.SUCCESS(
                f"Dispatch cycle complete: {results['expired_offers']} offers expired, "
                f"{results['dispatched']} rides (re)dispatched, "
                f"{results['no_driver_found']} marked NO_DRIVER_FOUND, "
                f"{results['still_searching']} still searching."
            )
        )
