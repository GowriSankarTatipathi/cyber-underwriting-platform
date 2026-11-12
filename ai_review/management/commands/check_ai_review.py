import shutil
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check local AI/OCR configuration without sending evidence or making a model API request."

    def add_arguments(self, parser):
        parser.add_argument("--queue", action="store_true", help="Check broker connectivity; does not start a worker.")

    def handle(self, **options):
        provider = settings.AI_REVIEW_PROVIDER
        if provider == "demo" and settings.DEBUG:
            self.stdout.write("Provider: local demonstration parser (no live AI or external requests).")
        elif provider == "openai":
            if not settings.AI_ALLOW_EXTERNAL or not settings.OPENAI_API_KEY or not settings.OPENAI_MODEL:
                raise CommandError("Set AI_ALLOW_EXTERNAL, OPENAI_API_KEY, and OPENAI_MODEL for live extraction. Do not put the API key in source control.")
            self.stdout.write("Provider: OpenAI configured. API access/model availability has not been tested.")
        else:
            self.stdout.write("AI processing is disabled. Manual review remains available.")
        if shutil.which(settings.AI_TESSERACT_COMMAND):
            self.stdout.write("OCR: Tesseract executable found.")
        else:
            self.stdout.write("OCR: Tesseract missing. Scanned/image evidence will need manual review.")
        if options["queue"]:
            from pilot.celery import app
            try:
                with app.connection_for_write() as connection:
                    connection.ensure_connection(max_retries=0)
            except Exception:
                raise CommandError("Queue unavailable. Start scripts/dev_queue.py or configure CELERY_BROKER_URL.") from None
            self.stdout.write("Queue: connection succeeded. A Celery worker must also be running.")
