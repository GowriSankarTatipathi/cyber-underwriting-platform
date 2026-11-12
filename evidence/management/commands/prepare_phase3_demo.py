from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Prepare fictional upload documents without granting access or creating reviews."

    def handle(self, **options):
        if not settings.DEBUG:
            raise CommandError("Fictional demo samples are only available in development.")
        root = settings.BASE_DIR / ".local" / "phase3-samples"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        date = timezone.now().strftime("%Y-%m-%d %H:%M UTC")
        samples = {
            "restore-initial.txt": f"FICTIONAL PILOT EVIDENCE\nService: Order processing\nObserved: {date}\nSupervisor: Sample IT technician\nScope: Order processing and required dependencies; create and retrieve a test order.\nTotal recovery time: 7 hours (420 minutes).\nRecovered data age: 35 minutes.\nFunctional checks: passed; create and retrieve a test order.\n",
            "restore-retest.txt": f"FICTIONAL PILOT EVIDENCE\nService: Order processing\nObserved: {date}\nSupervisor: Sample IT technician\nRetest after improving recovery procedure.\nScope: Order processing and required dependencies; create and retrieve a test order.\nTotal recovery time: 3 hours (180 minutes).\nRecovered data age: 25 minutes.\nFunctional checks: passed; create and retrieve a test order.\n",
            "backup-job.txt": f"FICTIONAL PILOT EVIDENCE\nService: Order processing\nObserved: {date}\nBackup job: successful.\nCoverage: Order database and application configuration.\nThis records a completed backup job; it is not a restore test.\n",
            "mfa-coverage.txt": f"FICTIONAL PILOT EVIDENCE\nObserved: {date}\nSource: Sample identity export.\nScope: Privileged accounts for Order processing.\nAll 8 sampled privileged accounts have MFA enabled.\n",
        }
        for name, content in samples.items():
            path = root / name
            if not path.exists():
                path.write_text(content)
                path.chmod(0o600)
        self.stdout.write("Fictional samples are ready in .local/phase3-samples/. Existing files were preserved. The access-review document is intentionally missing.")
