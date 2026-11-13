"""Isolated, repeatable fictional cases; never changes the user's existing pilot."""
import json
import os
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from evidence.services import assess_control, ensure_case_service, record_restore, review_evidence, review_restore, upload_evidence
from portfolio import services as portfolio
from underwriting.services import approve_report, draft_report, prepare_decision, record_decision, save_finding
from workspaces.models import Case, ChecklistVersion, EvidenceRequest, Membership, Organization, SharingGrant, User
from workspaces.services import audit
from workspaces.demo_data import DEMO_INSURER, SCENARIOS


class Command(BaseCommand):
    help = "Prepare three isolated fictional walkthrough cases and a partner inbox. Existing cases, passwords and approvals are preserved."

    @transaction.atomic
    def handle(self, **options):
        if not settings.DEBUG or settings.EVIDENCE_STORAGE_BACKEND != "local" or settings.EVIDENCE_SCAN_MODE != "basic":
            raise CommandError("Walkthrough seeding requires debug mode, local evidence storage and basic development scanning. It must not run in staging.")
        now = timezone.now()
        credentials, users = [], {}
        insurer, _ = Organization.objects.get_or_create(name=DEMO_INSURER, kind="insurer")
        # A stable insurer lock also serializes two accidental concurrent seed runs.
        Organization.objects.select_for_update().get(pk=insurer.pk)
        for role in ("underwriter", "owner", "technician", "partner"):
            email = f"walkthrough-{role}@example.test"
            user = User.objects.filter(email__iexact=email).first()
            if not user:
                password = secrets.token_urlsafe(24)
                user = User.objects.create_user(email, password, first_name="Fictional", last_name=role.title())
                EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
                credentials.append(f"{email}\t{password}")
            if not user.is_active:
                raise CommandError(f"The existing fictional {role} account is inactive. No account was reset.")
            users[role] = user
        underwriter, owner, tech, partner = (users[k] for k in ("underwriter", "owner", "technician", "partner"))
        Membership.objects.get_or_create(user=underwriter, organization=insurer, defaults={"role": "underwriter"})
        sample = json.loads((settings.BASE_DIR / "pilot/data/pilot.json").read_text())
        requirements = [{"name": row["name"], "evidence": row["evidence"], "freshness_days": int(row["freshness"].split()[0])} for row in sample["checklist"]]
        checklist, _ = ChecklistVersion.objects.get_or_create(insurer=insurer, version="walkthrough-v1", defaults={"product": "Commercial cyber", "requirements": requirements, "recovery_targets": sample["targets"]})
        created = 0
        for reference, business_name, description in SCENARIOS:
            existing = Case.objects.filter(reference=reference).first()
            if existing:
                if existing.insurer_id != insurer.pk or existing.created_by_id != underwriter.pk:
                    raise CommandError(f"{reference} is already used by another case. Nothing was changed.")
                continue
            business = Organization.objects.create(name=business_name, kind="business")
            case = Case.objects.create(reference=reference, insurer=insurer, applicant=business, checklist=checklist,
                kind="renewal", service="Order processing", assignee=underwriter, created_by=underwriter,
                due_date=(now + timedelta(days=14)).date(), consent_granted_by=owner, consent_expires_at=now + timedelta(days=90),
                internal_notes="FICTIONAL WALKTHROUGH ONLY. " + description)
            for user, role in ((owner, "owner"), (tech, "technician")):
                SharingGrant.objects.create(case=case, user=user, role=role, granted_by=underwriter if role == "owner" else owner,
                    expires_at=now + timedelta(days=90), organization_name="Fictional walkthrough organization")
            items = {}
            for row in requirements:
                name = row["name"]
                items[name] = EvidenceRequest.objects.create(case=case, kind={"Supervised restore": "restore", "Backup coverage": "backup"}.get(name, "control"),
                    title=name, description=row["evidence"], freshness_days=row["freshness_days"], due_date=case.due_date, assigned_to=tech)
            service = ensure_case_service(case)
            restore = items["Supervised restore"]
            first = self.restore(case, service, restore, underwriter, tech, now, minutes=420)
            if reference != "WALK-01":
                retest = self.restore(case, service, restore, underwriter, tech, now, minutes=180, parent=first)
                assess_control(underwriter, restore, "verified", "FICTIONAL: verified three-hour retest with passing functional checks.", list(retest.evidence_versions.all()))
            findings = []
            for item in items.values():
                if item.kind == "restore" and reference != "WALK-01":
                    continue
                findings.append(save_finding(underwriter, case, {"request": item, "title": "Provide " + item.title.lower(),
                    "description": "FICTIONAL: provide dated, scope-matched evidence for " + item.title.lower() + ".",
                    "kind": "retest" if item.kind == "restore" else "evidence", "priority": "high", "priority_reason": "Evidence is missing or the recovery target was missed.",
                    "owner": tech, "due_date": case.due_date, "retest_of": first if item.kind == "restore" else None}))
            portfolio.authorize(owner, case, now + timedelta(days=30), True)
            portfolio.record_dependency(underwriter, case, service, "Fictional Shared Cloud", "fictional-shared-cloud.example", True,
                "FICTIONAL service inventory", now - timedelta(days=1))
            if reference != "WALK-01":
                snapshot = prepare_decision(underwriter, case)
                decision = record_decision(underwriter, snapshot, "conditional", "FICTIONAL: retest verified; outstanding evidence is covered by explicit conditions.", findings, True)
                report = draft_report(underwriter, decision, "Fictional capacity review", "The three-hour retest meets the four-hour target. Access review, MFA and backup coverage still require evidence.")
                approve_report(underwriter, report, "FICTIONAL: approved the exact summary for the partner walkthrough.")
                relationship = portfolio.Relationship.objects.filter(insurer=insurer, contract_reference="FICTIONAL-WALKTHROUGH-ONLY").first()
                if not relationship:
                    relationship = portfolio.create_relationship(underwriter, case, "Fictional Capacity Partners", partner, "FICTIONAL-WALKTHROUGH-ONLY", now + timedelta(days=30), True)
                submission = portfolio.prepare_submission(underwriter, case, relationship, report, now + timedelta(days=7))
                if reference == "WALK-03":
                    portfolio.consent_submission(owner, submission, True)
                    portfolio.release_submission(underwriter, submission, "FICTIONAL: approved for the demo partner inbox only.")
            audit(case, underwriter, "Fictional walkthrough prepared", "Pre-authorized fictional accounts only; no external processing or messages.")
            created += 1
        if credentials:
            directory = Path(settings.BASE_DIR) / ".local"
            directory.mkdir(exist_ok=True, parents=True, mode=0o700)
            path = directory / "walkthrough-credentials.txt"
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write("\n".join(credentials) + "\n")
        self.stdout.write(f"Created {created} fictional walkthrough cases. Existing cases and passwords were preserved.")
        self.stdout.write("New passwords, if any, are in .local/walkthrough-credentials.txt. Use walkthrough-underwriter@example.test or walkthrough-partner@example.test. No email was sent.")

    def restore(self, case, service, item, underwriter, tech, now, minutes, parent=None):
        body = f"FICTIONAL WALKTHROUGH\nCase: {case.reference}\nService: Order processing\nRestore: {minutes} minutes\nData age: 25 minutes\nFunctional checks: passed; created and retrieved an order.\n".encode()
        original = parent.evidence_versions.first().document if parent else None
        version, _ = upload_evidence(tech, item, SimpleUploadedFile("fictional-restore.txt", body), service,
            "Fictional restore log", "Fictional supervised test", "Order processing and required dependencies", now - timedelta(hours=3 if parent else 24), uuid.uuid4(), original)
        review_evidence(underwriter, version, "accepted", True, "FICTIONAL: source and scope reviewed.")
        test = record_restore(tech, item, {"service": service, "retest_of": parent, "performed_at": now - timedelta(hours=3 if parent else 24),
            "scope": "Order processing and required dependencies", "supervised_by": "Fictional technician", "duration_minutes": Decimal(minutes),
            "data_age_minutes": Decimal(25), "functional_checks_passed": True, "functional_checks": "Created and retrieved a fictional test order.", "evidence_versions": [version]})
        review_restore(underwriter, test, True, "FICTIONAL: verified the elapsed time and functional checks.")
        return test
