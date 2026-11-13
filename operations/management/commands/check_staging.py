import json
import os
import shutil
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check staging configuration; optionally verify S3, Redis and worker availability."

    def add_arguments(self, parser):
        parser.add_argument("--connections", action="store_true")

    def handle(self, **options):
        problems = []
        if settings.DEBUG:
            problems.append("DJANGO_DEBUG must be false")
        if os.getenv("POSTGRES_PASSWORD") or os.getenv("POSTGRES_PASSWORD_FILE"):
            problems.append("remove database administrator credentials from the application environment; mount its secret only in PostgreSQL")
        if settings.LOCAL_PASSWORD_ONLY:
            problems.append("set LOCAL_PASSWORD_ONLY=false in staging")
        if len(settings.SECRET_KEY) < 50 or "replace-with" in settings.SECRET_KEY:
            problems.append("configure a persistent random secret key of at least 50 characters")
        if not settings.ALLOWED_HOSTS or "*" in settings.ALLOWED_HOSTS:
            problems.append("set explicit allowed hosts")
        if settings.EVIDENCE_STORAGE_BACKEND != "s3" or not settings.EVIDENCE_S3_BUCKET:
            problems.append("configure private versioned S3 evidence storage")
        if settings.EVIDENCE_SCAN_MODE != "external":
            problems.append("configure external malware scanning")
        try:
            command = json.loads(settings.EVIDENCE_SCAN_COMMAND)
            if not isinstance(command, list) or not command or not shutil.which(command[0]):
                problems.append("scanner executable is unavailable")
        except (ValueError, TypeError):
            problems.append("scanner command must be a JSON argument list")
        if settings.AI_REVIEW_PROVIDER == "demo":
            problems.append("demo extraction is not allowed in staging")
        if problems:
            raise CommandError("Staging configuration incomplete: " + "; ".join(problems))
        if options["connections"]:
            from evidence.storage import check_s3
            from pilot.celery import app
            try:
                check_s3()
                with app.connection_for_write() as connection:
                    connection.ensure_connection(max_retries=0)
                if not app.control.inspect(timeout=3).ping():
                    raise ValueError
            except Exception:
                raise CommandError("S3, Redis or worker verification failed. Inspect private configuration and service health.") from None
        self.stdout.write("Staging configuration checks passed." + (" S3, Redis and worker responded." if options["connections"] else " External services have not been verified."))
