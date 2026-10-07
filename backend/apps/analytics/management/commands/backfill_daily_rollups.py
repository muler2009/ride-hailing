from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.analytics.services import compute_daily_rollups_for_range


class Command(BaseCommand):
    """
    Backfills DailyPlatformRollup/DailyVehicleTypeRollup rows for a range
    of past days — for standing the table up the first time (rolling up
    every day since launch), or for correcting a range after fixing a
    bug in a source app's data.

    Usage:
        manage.py backfill_daily_rollups --days 30
        manage.py backfill_daily_rollups --start 2026-06-01 --end 2026-06-30
    """

    help = "Compute (or recompute) daily analytics rollups for a range of closed days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Backfill the last N fully-closed days, ending yesterday.")
        parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD), inclusive.")
        parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD), inclusive. Defaults to yesterday.")

    def handle(self, *args, **options):
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)

        if options["days"] is not None:
            if options["start"] or options["end"]:
                raise CommandError("Pass either --days, or --start/--end, not both.")
            if options["days"] < 1:
                raise CommandError("--days must be at least 1.")
            end_date = yesterday
            start_date = end_date - timedelta(days=options["days"] - 1)
        else:
            if not options["start"]:
                raise CommandError("Pass --days, or at least --start (YYYY-MM-DD).")
            start_date = date.fromisoformat(options["start"])
            end_date = date.fromisoformat(options["end"]) if options["end"] else yesterday

        if end_date >= today:
            raise CommandError(f"--end ({end_date}) must be before today ({today}); only closed days can be rolled up.")
        if start_date > end_date:
            raise CommandError(f"--start ({start_date}) is after --end ({end_date}).")

        self.stdout.write(f"Backfilling rollups for {start_date} through {end_date}...")
        results = compute_daily_rollups_for_range(start_date, end_date)
        for rollup in results:
            self.stdout.write(
                f"  {rollup.date}: {rollup.rides_requested} requested, "
                f"{rollup.rides_completed} completed, gross {rollup.gross_revenue}"
            )
        self.stdout.write(self.style.SUCCESS(f"Backfill complete — {len(results)} day(s) computed."))
