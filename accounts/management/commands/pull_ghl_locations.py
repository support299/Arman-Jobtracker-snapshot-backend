"""
Pull GHL location details into accounts.Location.

Uses LocationServices.pull_locations and GHLAuthCredentials per location.

Usage:
    python manage.py pull_ghl_locations
    python manage.py pull_ghl_locations --location-id <ghl_location_id>
    python manage.py pull_ghl_locations --location-id a --location-id b
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Fetch locations from GoHighLevel and save to accounts.Location. "
        "Without --location-id, syncs every distinct location_id on "
        "GHLAuthCredentials."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--location-id",
            dest="location_ids",
            action="append",
            default=[],
            metavar="ID",
            help=(
                "GHL location_id (repeat for multiple). "
                "Omit to sync all credential locations."
            ),
        )

    def handle(self, *args, **options):
        from accounts.utils import LocationServices

        raw = options["location_ids"]
        ids = [x.strip() for x in raw if x and str(x).strip()]
        loc_arg = ids if ids else None

        if ids:
            self.stdout.write("Pulling locations for: " + ", ".join(ids))
        else:
            self.stdout.write(
                "Pulling for all GHLAuthCredentials.location_id values"
            )

        summary = LocationServices.pull_locations(loc=loc_arg)

        for line in summary:
            if "Failed" in line:
                self.stdout.write(self.style.ERROR(line))
            else:
                self.stdout.write(self.style.SUCCESS(line))

        n = len(summary)
        self.stdout.write(self.style.NOTICE(f"Done ({n} location(s) processed)."))
