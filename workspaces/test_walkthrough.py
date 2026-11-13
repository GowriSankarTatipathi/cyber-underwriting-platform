from io import StringIO
from pathlib import Path
import shutil
import tempfile
from django.conf import settings
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from evidence.models import EvidenceVersion
from evidence.rules import control_summary, current_restore_status
from portfolio.models import Submission
from workspaces.models import Case, User


@override_settings(DEBUG=True, LOCAL_PASSWORD_ONLY=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic")
class WalkthroughTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pilot/data").mkdir(parents=True)
        shutil.copy(settings.BASE_DIR / "pilot/data/pilot.json", self.root / "pilot/data/pilot.json")
        self.override = override_settings(BASE_DIR=self.root, EVIDENCE_LOCAL_ROOT=self.root / ".local/evidence")
        self.override.enable()
        self.addCleanup(self.override.disable)

    def seed(self):
        output = StringIO()
        call_command("prepare_walkthrough_demo", stdout=output)
        return output.getvalue()

    def login(self, role):
        user = User.objects.get(email=f"walkthrough-{role}@example.test")
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return user

    def test_seed_scenarios_retests_and_idempotency_preserve_user_work(self):
        output = self.seed()
        credentials = self.root / ".local/walkthrough-credentials.txt"
        original = credentials.read_text()
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(original.splitlines()[0].split("\t")[1], output)
        self.assertEqual(Case.objects.count(), 3)
        case = Case.objects.get(reference="WALK-01")
        self.assertEqual(current_restore_status(case.requests.get(kind="restore").restore_tests.first()), "Target missed")
        case = Case.objects.get(reference="WALK-02")
        tests = case.requests.get(kind="restore").restore_tests.all()
        retest, original_test = tests
        self.assertEqual(retest.retest_of, original_test)
        self.assertEqual(current_restore_status(retest), "Passed")
        self.assertEqual(control_summary(case.requests.get(title="Access review"))["status"], "Missing")
        self.assertEqual(case.decisions.get().outcome, "conditional")
        self.assertEqual(len(case.decisions.get().conditions), 3)
        self.assertFalse(hasattr(case.capacity_submissions.get(), "release"))
        self.assertTrue(hasattr(Case.objects.get(reference="WALK-03").capacity_submissions.get(), "release"))
        hashes = dict(User.objects.values_list("email", "password"))
        count = EvidenceVersion.objects.count()
        case.internal_notes = "Preserve this user's workshop notes"
        case.save(update_fields=["internal_notes"])
        self.seed()
        case.refresh_from_db()
        self.assertEqual(case.internal_notes, "Preserve this user's workshop notes")
        self.assertEqual(credentials.read_text(), original)
        self.assertEqual(dict(User.objects.values_list("email", "password")), hashes)
        self.assertEqual(EvidenceVersion.objects.count(), count)
        self.assertEqual(Case.objects.count(), 3)

    def test_http_owner_approval_release_response_and_revocation_journey(self):
        self.seed()
        case = Case.objects.get(reference="WALK-02")
        submission = case.capacity_submissions.get()
        self.login("owner")
        response = self.client.get(reverse("capacity_detail", args=[case.pk, submission.pk]))
        self.assertContains(response, "Control results")
        self.assertContains(response, "Access review")
        response = self.client.post(reverse("capacity_consent", args=[case.pk, submission.pk]), {"confirm": "on"})
        self.assertEqual(response.status_code, 302)
        self.login("underwriter")
        self.assertContains(self.client.get(reverse("walkthrough")), "WALK-02")
        response = self.client.post(reverse("capacity_release", args=[case.pk, submission.pk]), {"rationale": "Fictional report reviewed"})
        self.assertEqual(response.status_code, 302)
        self.login("partner")
        self.assertRedirects(self.client.get(reverse("home")), reverse("capacity_inbox"))
        self.assertContains(self.client.get(reverse("capacity_inbox")), "WALK-02")
        self.assertEqual(self.client.get(reverse("case", args=[case.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("capacity_partner", args=[submission.pk]), {"status": "clarification", "message": "Please provide the remaining access review."}).status_code, 302)
        self.assertEqual(submission.responses.count(), 1)
        self.assertEqual(self.client.get(reverse("capacity_export", args=[submission.pk])).status_code, 200)
        self.login("owner")
        self.client.post(reverse("capacity_revoke", args=[case.pk, submission.pk]))
        self.login("partner")
        self.assertEqual(self.client.get(reverse("capacity_export", args=[submission.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("capacity_inbox")), "WALK-02")

    def test_expired_submission_shows_status_and_hides_release_action(self):
        self.seed()
        case = Case.objects.get(reference="WALK-02")
        submission = case.capacity_submissions.get()
        Submission.objects.filter(pk=submission.pk).update(expires_at=timezone.now())
        self.login("underwriter")
        page = self.client.get(reverse("capacity_detail", args=[case.pk, submission.pk]))
        self.assertContains(page, "Expired")
        self.assertNotContains(page, "Review and release")
        self.assertContains(self.client.get(reverse("portfolio", args=[case.pk])), "Expired")

    def test_seed_refuses_production_and_external_storage_before_mutation(self):
        for overrides in ({"DEBUG": False}, {"EVIDENCE_STORAGE_BACKEND": "s3"}, {"EVIDENCE_SCAN_MODE": "external"}):
            with self.subTest(overrides=overrides), override_settings(**overrides), self.assertRaises(CommandError):
                self.seed()
        self.assertFalse(User.objects.exists())
        self.assertFalse(Case.objects.exists())

    def test_technician_navigation_does_not_offer_insurer_tools(self):
        self.seed()
        self.login("technician")
        case = Case.objects.get(reference="WALK-01")
        response = self.client.get(reverse("case", args=[case.pk]))
        self.assertContains(response, "Follow-up work")
        self.assertNotContains(response, 'href="' + reverse("portfolio", args=[case.pk]) + '"')
        self.assertNotContains(response, 'href="' + reverse("pilot_metrics", args=[case.pk]) + '"')
