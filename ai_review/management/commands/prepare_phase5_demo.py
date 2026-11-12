from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from PIL import Image, ImageDraw, ImageFont


class Command(BaseCommand):
    help = "Prepare fictional scanned restore logs for local OCR demos; no access grants or reviews are created."

    def handle(self, **options):
        if not settings.DEBUG:
            raise CommandError("Demo samples are only available in development.")
        root = settings.BASE_DIR / ".local" / "phase5-samples"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lines = ["FICTIONAL PILOT RESTORE LOG", "Service: Order processing", "Scope: Order processing and required dependencies.",
            "Total recovery time: 7 hours (420 minutes).", "Recovered data age: 35 minutes.", "Functional checks: passed; created and retrieved a test order."]
        image = Image.new("RGB", (1800, 650), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=30)
        for index, line in enumerate(lines):
            draw.text((60, 45 + index * 85), line, fill="black", font=font)
        for filename, format in (("restore-seven-hours.png", "PNG"), ("restore-seven-hours-scanned.pdf", "PDF")):
            target = root / filename
            if not target.exists():
                image.save(target, format=format)
                target.chmod(0o600)
        target = root / "conflicting-restore.txt"
        if not target.exists():
            target.write_text("FICTIONAL CONFLICTING OBSERVATIONS\nService: Order processing\nTotal recovery time: 7 hours (420 minutes).\nTotal recovery time: 3 hours (180 minutes).\nScope: The log does not identify whether these are separate tests.\n")
            target.chmod(0o600)
        self.stdout.write("Fictional text, PNG, and scanned PDF samples are ready in .local/phase5-samples/. Existing files were preserved.")
