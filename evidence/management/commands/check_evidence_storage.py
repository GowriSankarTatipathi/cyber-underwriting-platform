from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from evidence.storage import StorageUnavailable, check_s3


class Command(BaseCommand):
    help = "Read-only check of S3 versioning, public access blocking, and encryption."

    def handle(self, **options):
        if settings.EVIDENCE_STORAGE_BACKEND != "s3":
            self.stdout.write("Private local development storage selected. Live S3 has not been verified.")
            return
        try:
            check_s3()
        except StorageUnavailable as error:
            raise CommandError(str(error))
        self.stdout.write(self.style.SUCCESS("S3 bucket configuration verified: versioning, public access blocking, and encryption."))
