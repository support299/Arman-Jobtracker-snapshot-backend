from django.core.management.base import BaseCommand

from accounts.models import GHLAuthCredentials
from service_app.service_clone import copy_all_services_between_locations


class Command(BaseCommand):
    help = (
        "Clone all services (structure, no pricing) from one GHL location to another. "
        "Source account is read-only; only new rows are created on the destination."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--from-location",
            type=str,
            required=True,
            help="Source GHL location_id (e.g. TrueShine account)",
        )
        parser.add_argument(
            "--to-location",
            type=str,
            required=True,
            help="Destination GHL location_id",
        )
        parser.add_argument(
            "--no-transaction",
            action="store_true",
            help="Do not wrap the copy in a single database transaction",
        )

    def handle(self, *args, **options):
        src = options["from_location"]
        dst = options["to_location"]
        use_tx = not options["no_transaction"]

        try:
            created = copy_all_services_between_locations(
                src, dst, use_transaction=use_tx
            )
        except GHLAuthCredentials.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(
                    "No GHLAuthCredentials found for one of the location_id values."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {len(created)} service(s) on destination {dst}"
            )
        )
        for s in created:
            self.stdout.write(f"  {s.id}  {s.name}")
