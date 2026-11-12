import json
import os
import secrets
from datetime import timedelta
from pathlib import Path
from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from workspaces.models import Case, CaseFile, ChecklistVersion, EvidenceRequest, Membership, Organization, User
from workspaces.services import audit


class Command(BaseCommand):
    help = "Create fictional Phase 2 accounts and a case. Never resets existing passwords."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data can only be created with DJANGO_DEBUG=true.")
        local = settings.BASE_DIR / ".local"
        local.mkdir(mode=0o700, exist_ok=True)
        credentials = []
        users = {}
        for label, name in [("underwriter", "Alex Morgan"), ("reviewer", "Jordan Lee"), ("owner", "Casey Taylor"), ("technician", "Sam Rivera")]:
            email = f"{label}@example.test"
            user = User.objects.filter(email=email).first()
            if user is None:
                password = secrets.token_urlsafe(18)
                first, last = name.split(" ", 1)
                user = User.objects.create_user(email, password, first_name=first, last_name=last)
                credentials.append(f"{email}\t{password}")
            EmailAddress.objects.get_or_create(user=user, email=email, defaults={"verified": True, "primary": True})
            users[label] = user
        insurer, _ = Organization.objects.get_or_create(name="Harborline Specialty (fictional)", kind="insurer")
        for role in ("underwriter", "reviewer"):
            Membership.objects.get_or_create(user=users[role], organization=insurer, defaults={"role": role})
        sample = json.loads((settings.BASE_DIR / "pilot/data/pilot.json").read_text())
        checklist = ChecklistVersion.objects.filter(insurer=insurer, version="pilot-1").first()
        if checklist is None:
            requirements = [{"name": item["name"], "evidence": item["evidence"], "freshness_days": int(item["freshness"].split()[0])} for item in sample["checklist"]]
            checklist = ChecklistVersion.objects.create(insurer=insurer, version="pilot-1", requirements=requirements, recovery_targets=sample["targets"])
        case = Case.objects.filter(reference="DEMO-001").first()
        if case is None:
            business = Organization.objects.create(name=sample["applicant"], kind="business")
            case = Case.objects.create(reference="DEMO-001", insurer=insurer, applicant=business, checklist=checklist, service=sample["service"], created_by=users["underwriter"], assignee=users["underwriter"], due_date=timezone.localdate() + timedelta(days=14))
            for item in checklist.requirements:
                EvidenceRequest.objects.create(case=case, kind={"Supervised restore": "restore", "Backup coverage": "backup"}.get(item["name"], "control"), title=item["name"], description=item["evidence"], freshness_days=item["freshness_days"], due_date=case.due_date)
            audit(case, users["underwriter"], "Demo case created", "Business authorization pending")
        from evidence.services import ensure_case_service
        ensure_case_service(case)
        item = case.requests.filter(title="Supervised restore").first()
        if item:
            key = f"{case.pk}/restore-brief.txt"
            destination = Path(settings.PRIVATE_FILE_ROOT) / key
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_text("FICTIONAL DEVELOPMENT SAMPLE\nOrder processing restore: 420 minutes.\nRecovery target: 240 minutes.\nRecovered data age: 35 minutes.\nFunctional checks: passed.\nThis brief is a Phase 2 access-control fixture, not reviewed evidence.\n")
            CaseFile.objects.get_or_create(request=item, storage_key=key, defaults={"name": "Fictional restore brief.txt"})
        if credentials:
            path = local / "demo-credentials.txt"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write("\n".join(credentials) + "\n")
            self.stdout.write("New credentials saved privately to .local/demo-credentials.txt (ignored by Git).")
        self.stdout.write(self.style.SUCCESS("Phase 2 demo ready. Sign in as underwriter@example.test; MFA enrollment is required. Invite owner@example.test from DEMO-001 to begin."))
