from datetime import timedelta
from decimal import Decimal
import uuid
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from evidence import tests as evidence_tests
from evidence.models import ControlAssessment, EvidenceReview, FileScan, RestoreTest
from evidence.rules import control_summary
from evidence.services import publish_policy, review_restore
from underwriting.models import Finding
from underwriting.snapshots import case_snapshot, checksum
from workspaces.models import Case, Membership
from workspaces.tests import make_user
from .errors import ProcessingError
from .models import ConsentRevocation, DraftDisposition, ExtractionResult, FactReview, ProcessingAttempt, ReviewJob
from .permissions import require_result_access
from .processing import run_job
from .providers import demo_extract
from .rules import discrepancies, effective_facts, rule_preview
from .services import authorize_external, cancel_job, prepare_followup, publish_followup, queue_review, reject_followup, retry_job, review_fact, revoke_external
from .tasks import dispatch_job
from .test_extraction import LOG


@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic",
    AI_REVIEW_PROVIDER="demo", AI_ALLOW_EXTERNAL=False, OPENAI_API_KEY="", OPENAI_MODEL="")
class AIWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        evidence_tests.EvidenceTests.setUpTestData.__func__(cls)

    setUp = evidence_tests.EvidenceTests.setUp
    login = evidence_tests.EvidenceTests.login
    upload = evidence_tests.EvidenceTests.upload
    record = evidence_tests.EvidenceTests.record

    def process(self, version=None, body=LOG):
        version = version or self.upload(body=body, accept=False)
        job, _ = queue_review(self.underwriter, version)
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "completed", job.error_code)
        return job.result

    def accept(self, result, field=None):
        reviews = []
        for fact in result.facts:
            if field is None or field == fact["field"]:
                reviews.append(review_fact(self.underwriter, result, fact["key"], "accept", "Checked against original file.", confirm=True))
        return reviews

    def draft_data(self, draft):
        data = dict(draft.content)
        data.update(request=self.restore_request, owner=self.tech, due_date=timezone.localdate() + timedelta(days=7),
            retest_of=RestoreTest.objects.get(pk=data["retest_of"]) if data["retest_of"] else None)
        return data

    def test_cited_seven_hours_misses_four_hour_target_without_accepting_evidence(self):
        result = self.process()
        self.assertEqual(rule_preview(result, False)["status"], "Target missed")
        self.assertEqual(rule_preview(result)["status"], "Inconclusive")
        self.assertEqual(EvidenceReview.objects.count(), 0)
        self.assertEqual(RestoreTest.objects.count(), 0)
        self.assertEqual(ControlAssessment.objects.count(), 0)
        self.accept(result)
        self.assertEqual(rule_preview(result)["status"], "Target missed")
        self.assertEqual(control_summary(self.access_request)["status"], "Missing")
        self.assertEqual(result.source_sha256, result.job.version.sha256)

    def test_complete_retest_draft_to_assigned_workspace_with_frozen_provenance(self):
        version = self.upload(body=LOG)
        baseline = self.record(version)
        review_restore(self.underwriter, baseline, True, "Seven hours exceeds four-hour target.")
        result = self.process(version)
        self.accept(result)
        draft = prepare_followup(self.underwriter, result)
        self.assertEqual(draft.content["kind"], "retest")
        self.assertEqual(draft.content["retest_of"], str(baseline.pk))
        self.assertIn("420", draft.content["description"])
        self.assertFalse(Finding.objects.exists())
        self.login(self.underwriter)
        data = self.draft_data(draft)
        data.update(request=str(data["request"].pk), owner=data["owner"].pk, retest_of=str(baseline.pk),
            rationale="Send a supervised retest request.", confirm="on")
        response = self.client.post(reverse("ai_draft", args=[self.case.pk, draft.pk]), data)
        self.assertEqual(response.status_code, 302, response.content[:1000])
        disposition = DraftDisposition.objects.get(draft=draft)
        self.assertEqual(disposition.finding.retest_of, baseline)
        snapshot = case_snapshot(self.case)
        self.assertEqual(snapshot["ai_followups"][0]["review_snapshot"]["source_sha256"], version.sha256)
        self.login(self.tech)
        self.assertContains(self.client.get(reverse("finding_detail", args=[self.case.pk, disposition.finding_id])), "7 hours")
        self.assertEqual(self.client.get(reverse("ai_draft", args=[self.case.pk, draft.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("ai_job", args=[self.case.pk, result.job_id])).status_code, 404)

    def test_http_queue_fact_review_and_all_pages_render(self):
        version = self.upload(body=LOG, accept=False)
        self.login(self.underwriter)
        url = reverse("ai_queue", args=[self.case.pk, version.pk])
        self.assertContains(self.client.get(url), "Local demo parser")
        self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 302)
        job = ReviewJob.objects.get()
        run_job(job.pk)
        result = ExtractionResult.objects.get()
        for fact in result.facts:
            url = reverse("ai_fact_review", args=[self.case.pk, job.pk, fact["key"]])
            self.assertEqual(self.client.get(url).status_code, 200)
            response = self.client.post(url, {"action": "accept", "rationale": "Checked original", "previous_review_id": 0, "confirm": "on"})
            self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse("ai_draft_prepare", args=[self.case.pk, job.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response.url), "Underwriter review & assignment")
        draft = result.drafts.get()
        for name, args in [("ai_overview", [self.case.pk]), ("ai_job", [self.case.pk, job.pk]),
            ("ai_draft_reject", [self.case.pk, draft.pk]), ("ai_job_status", [self.case.pk, job.pk])]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        self.login(self.owner)
        self.assertEqual(self.client.get(reverse("ai_consent", args=[self.case.pk])).status_code, 200)

    def test_pending_ai_review_does_not_invalidate_phase4_decision_material(self):
        version = self.upload(body=LOG)
        before = checksum(case_snapshot(self.case))
        result = self.process(version)
        self.accept(result)
        prepare_followup(self.underwriter, result)
        self.assertEqual(before, checksum(case_snapshot(self.case)))
        self.assertNotIn("ai_followups", case_snapshot(self.case))

    def test_deduplicated_requests_and_duplicate_worker_delivery(self):
        version = self.upload(body=LOG)
        with self.captureOnCommitCallbacks(execute=True) as callbacks, patch("ai_review.services.dispatch_job") as dispatch:
            first, created = queue_review(self.underwriter, version)
            second, duplicate_created = queue_review(self.underwriter, version)
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(len(callbacks), 1)
        dispatch.assert_called_once_with(first.pk)
        run_job(first.pk)
        with patch("ai_review.processing.extract") as provider:
            run_job(first.pk)
            provider.assert_not_called()
        self.assertEqual(ExtractionResult.objects.count(), 1)
        self.assertEqual(ProcessingAttempt.objects.count(), 1)

    def test_corrections_rejections_and_stale_tabs_are_audited(self):
        result = self.process()
        fact = next(row for row in result.facts if row["field"] == "restore_duration_minutes")
        citation = fact["citations"][0]
        with self.assertRaises(ValidationError):
            review_fact(self.underwriter, result, fact["key"], "correct", "Invalid correction", value="180", passage_id=citation["passage_id"], quote=citation["quote"], confirm=True)
        review = review_fact(self.underwriter, result, fact["key"], "correct", "Confirmed minutes", value="420.0", passage_id=citation["passage_id"], quote=citation["quote"], confirm=True)
        with self.assertRaises(ValidationError):
            review_fact(self.underwriter, result, fact["key"], "reject", "Stale tab", confirm=True)
        review_fact(self.underwriter, result, fact["key"], "reject", "Source needs clarification", previous_review_id=review.pk, confirm=True)
        self.assertNotIn("restore_duration_minutes", effective_facts(result)[0])
        self.assertEqual(result.reviews.count(), 2)
        result.refresh_from_db()
        self.assertEqual(next(row for row in result.facts if row["key"] == fact["key"])["normalized_value"], "420")
        with self.assertRaises(ValidationError):
            review.rationale = "Rewrite history"
            review.save()

    def test_conflicting_values_require_resolution_before_followup(self):
        result = self.process(body=LOG + b"Total recovery time: 3 hours (180 minutes).\n")
        self.assertTrue(discrepancies(result))
        self.accept(result)
        with self.assertRaises(ValidationError):
            prepare_followup(self.underwriter, result)
        fact = result.facts[-1]
        previous = result.reviews.get(fact_key=fact["key"])
        review_fact(self.underwriter, result, fact["key"], "reject", "Separate retest; source needs separation", previous_review_id=previous.pk, confirm=True)
        self.assertEqual(rule_preview(result)["status"], "Target missed")
        self.assertIsNotNone(prepare_followup(self.underwriter, result))

    def test_inventory_manual_record_and_same_observation_discrepancies(self):
        first = self.process()
        self.accept(first)
        other = self.upload(body=LOG.replace(b"Ordering", b"Another service").replace(b"7 hours (420 minutes)", b"3 hours (180 minutes)"), accept=False)
        self.record(other, duration_minutes=Decimal("420"))
        result = self.process(other)
        issues = " ".join(discrepancies(result))
        self.assertIn("service name differs", issues)
        self.assertIn("restore record", issues)
        self.assertIn("reviewed document", issues)
        later = self.upload(body=LOG.replace(b"7 hours (420 minutes)", b"3 hours (180 minutes)"), observed=self.observed + timedelta(hours=1), accept=False)
        self.assertFalse(discrepancies(self.process(later)))

    def test_new_version_blocks_mutations_but_retains_historical_read(self):
        result = self.process()
        self.accept(result)
        draft = prepare_followup(self.underwriter, result)
        second = self.upload(document=result.job.version.document, body=LOG + b"Updated log", accept=False)
        with self.assertRaises(ProcessingError):
            prepare_followup(self.underwriter, result)
        with self.assertRaises(ProcessingError):
            publish_followup(self.underwriter, draft, self.draft_data(draft), "Outdated", True)
        with self.assertRaises(ValidationError):
            queue_review(self.underwriter, result.job.version)
        self.login(self.underwriter)
        self.assertContains(self.client.get(reverse("ai_job", args=[self.case.pk, result.job_id])), "newer evidence version")
        new_job, created = queue_review(self.underwriter, second)
        self.assertTrue(created)
        self.assertNotEqual(new_job.pk, result.job_id)

    def test_target_revision_requires_new_job_and_pins_original_target(self):
        result = self.process()
        publish_policy(self.underwriter, self.service, {"duration_minutes": 480, "data_age_minutes": 60, "required_scope": "Ordering and dependencies"})
        with self.assertRaises(ProcessingError):
            self.accept(result)
        newer, created = queue_review(self.underwriter, result.job.version)
        self.assertTrue(created)
        self.assertNotEqual(newer.policy_id, result.job.policy_id)
        self.assertEqual(result.job.policy.duration_minutes, 240)

    def test_changed_fact_review_invalidates_draft_and_only_decider_can_publish(self):
        result = self.process()
        self.accept(result)
        draft = prepare_followup(self.underwriter, result)
        reviewer = make_user("ai-assigned-reviewer")
        Membership.objects.create(user=reviewer, organization=self.insurer, role="reviewer")
        Case.objects.filter(pk=self.case.pk).update(assignee=reviewer)
        with self.assertRaises(Http404):
            publish_followup(reviewer, draft, self.draft_data(draft), "Not a decider", True)
        first = result.reviews.first()
        review_fact(self.underwriter, result, first.fact_key, "reject", "Reconsidered", previous_review_id=first.pk, confirm=True)
        with self.assertRaises(ValidationError):
            publish_followup(self.underwriter, draft, self.draft_data(draft), "Stale", True)

    def test_publish_validates_owner_confirmation_and_one_disposition(self):
        result = self.process()
        self.accept(result)
        draft = prepare_followup(self.underwriter, result)
        with self.assertRaises(ValidationError):
            publish_followup(self.underwriter, draft, self.draft_data(draft), "Missing confirmation")
        with self.assertRaises(ValidationError):
            publish_followup(self.underwriter, draft, {**self.draft_data(draft), "owner": self.outsider}, "Wrong recipient", True)
        reject_followup(self.underwriter, draft, "Manual clarification preferred")
        with self.assertRaises(ValidationError):
            publish_followup(self.underwriter, draft, self.draft_data(draft), "Already rejected", True)
        self.assertFalse(Finding.objects.exists())

    def test_owner_tech_outsider_and_wrong_case_cannot_read_jobs(self):
        result = self.process()
        for user in (self.owner, self.tech, self.outsider):
            self.login(user)
            for name in ("ai_job", "ai_job_status"):
                self.assertEqual(self.client.get(reverse(name, args=[self.case.pk, result.job_id])).status_code, 404)
            with self.assertRaises(Http404):
                queue_review(user, result.job.version)
        self.login(self.underwriter)
        self.assertEqual(self.client.get(reverse("ai_job", args=[uuid.uuid4(), result.job_id])).status_code, 404)

    def test_csrf_and_mfa_are_required_for_queue_and_review(self):
        result = self.process()
        self.login(self.underwriter)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.cookies = self.client.cookies
        self.assertEqual(csrf_client.post(reverse("ai_queue", args=[self.case.pk, result.job.version_id]), {"confirm": "on"}).status_code, 403)
        session = self.client.session
        session["account_authentication_methods"] = [{"method": "password", "at": 0}]
        session.save()
        response = self.client.get(reverse("ai_job", args=[self.case.pk, result.job_id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/", response.url)

    def test_quarantined_result_cannot_be_read_or_used(self):
        result = self.process()
        FileScan.objects.create(version=result.job.version, status="quarantined", engine="test", detail="New scan", actor=self.underwriter)
        with self.assertRaises(Http404):
            require_result_access(self.underwriter, result.job)
        with self.assertRaises(Http404):
            self.accept(result)
        self.login(self.underwriter)
        self.assertEqual(self.client.get(reverse("ai_job", args=[self.case.pk, result.job_id])).status_code, 404)

    def test_revoked_case_access_before_worker_blocks_storage_and_provider(self):
        job, _ = queue_review(self.underwriter, self.upload(body=LOG))
        Case.objects.filter(pk=self.case.pk).update(consent_revoked_at=timezone.now())
        with patch("ai_review.processing.read_private") as storage, patch("ai_review.processing.extract") as provider:
            run_job(job.pk)
            storage.assert_not_called()
            provider.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "blocked")
        self.assertFalse(ExtractionResult.objects.exists())

    def test_disabled_requester_blocks_queued_processing(self):
        job, _ = queue_review(self.underwriter, self.upload(body=LOG))
        type(self.underwriter).objects.filter(pk=self.underwriter.pk).update(is_active=False)
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "blocked")

    def test_revocation_during_provider_call_discards_result(self):
        job, _ = queue_review(self.underwriter, self.upload(body=LOG))
        def revoked(job, passages):
            Case.objects.filter(pk=self.case.pk).update(consent_revoked_at=timezone.now())
            return demo_extract(passages)
        with patch("ai_review.processing.extract", side_effect=revoked):
            run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "blocked")
        self.assertFalse(ExtractionResult.objects.exists())

    def test_cancelled_running_job_discards_provider_result(self):
        job, _ = queue_review(self.underwriter, self.upload(body=LOG))
        def cancelled(job, passages):
            cancel_job(self.underwriter, job)
            return demo_extract(passages)
        with patch("ai_review.processing.extract", side_effect=cancelled):
            run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.assertFalse(ExtractionResult.objects.exists())
        self.assertEqual(job.runs.get().status, "cancelled")

    def test_transient_failures_have_bounded_retry_and_manual_retry_keeps_history(self):
        job, _ = queue_review(self.underwriter, self.upload(body=LOG))
        with patch("ai_review.processing.extract", side_effect=ProcessingError("provider_unavailable", "Try again", True)):
            self.assertTrue(run_job(job.pk))
            self.assertTrue(run_job(job.pk))
            self.assertFalse(run_job(job.pk))
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), ("failed", 3))
        retry_job(self.underwriter, job)
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts, job.retry_base), ("completed", 4, 3))
        self.assertEqual(list(job.runs.order_by("number").values_list("number", flat=True)), [1, 2, 3, 4])

    def test_expired_lease_can_be_recovered_but_live_worker_cannot_be_retried(self):
        job, _ = queue_review(self.underwriter, self.upload(body=LOG))
        token = uuid.uuid4()
        ReviewJob.objects.filter(pk=job.pk).update(status="running", attempts=1, execution_token=token, lease_expires_at=timezone.now() + timedelta(seconds=60))
        ProcessingAttempt.objects.create(job=job, number=1, execution_token=token)
        with self.assertRaises(ValidationError):
            retry_job(self.underwriter, job)
        ReviewJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        retry_job(self.underwriter, job)
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.runs.get(number=1).status, "interrupted")

    def test_queue_failure_keeps_manual_restore_workflow_available(self):
        version = self.upload(body=LOG)
        job, _ = queue_review(self.underwriter, version)
        with patch("ai_review.tasks.process_evidence.apply_async", side_effect=RuntimeError("SECRET CONNECTION")):
            dispatch_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.error_code, "queue_unavailable")
        self.assertNotIn("SECRET", job.error_message)
        review = review_restore(self.underwriter, self.record(version), True, "Manually reviewed log")
        self.assertEqual(review.result, "Target missed")

    def test_external_processing_requires_configuration_and_business_authorization(self):
        version = self.upload(body=LOG)
        with override_settings(AI_REVIEW_PROVIDER="openai", AI_ALLOW_EXTERNAL=True, OPENAI_API_KEY="test", OPENAI_MODEL="configured-model"):
            with self.assertRaises(ValidationError):
                queue_review(self.underwriter, version)
            with self.assertRaises(Http404):
                authorize_external(self.underwriter, self.case, timezone.now() + timedelta(days=1), True)
            with self.assertRaises(ValidationError):
                authorize_external(self.owner, self.case, timezone.now() + timedelta(days=31), True)
            consent = authorize_external(self.owner, self.case, timezone.now() + timedelta(days=1), True)
            job, _ = queue_review(self.underwriter, version)
            self.assertEqual(job.consent_id, consent.pk)
            revoke_external(self.owner, consent)
            with patch("ai_review.processing.extract") as provider:
                run_job(job.pk)
                provider.assert_not_called()
            job.refresh_from_db()
            self.assertEqual(job.status, "blocked")

    @override_settings(AI_REVIEW_PROVIDER="openai", AI_ALLOW_EXTERNAL=True, OPENAI_API_KEY="test", OPENAI_MODEL="configured-model")
    def test_external_authorization_revoked_during_preparation_prevents_transmission(self):
        from .preparation import prepare_document
        consent = authorize_external(self.owner, self.case, timezone.now() + timedelta(days=1), True)
        job, _ = queue_review(self.underwriter, self.upload(body=LOG))
        def prepare_then_revoke(*args):
            prepared = prepare_document(*args)
            revoke_external(self.owner, consent)
            return prepared
        with patch("ai_review.processing.prepare_document", side_effect=prepare_then_revoke), patch("ai_review.processing.extract") as provider:
            run_job(job.pk)
            provider.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "blocked")

    @override_settings(AI_REVIEW_PROVIDER="openai", AI_ALLOW_EXTERNAL=True, OPENAI_API_KEY="test", OPENAI_MODEL="configured-model")
    def test_completed_result_retains_case_access_after_external_consent_revocation(self):
        consent = authorize_external(self.owner, self.case, timezone.now() + timedelta(days=1), True)
        job, _ = queue_review(self.underwriter, self.upload(body=LOG))
        with patch("ai_review.processing.extract", side_effect=lambda job, passages: demo_extract(passages)):
            run_job(job.pk)
        result = ExtractionResult.objects.get(job=job)
        revoke_external(self.owner, consent)
        self.accept(result)
        Case.objects.filter(pk=self.case.pk).update(consent_revoked_at=timezone.now())
        with self.assertRaises(Http404):
            require_result_access(self.underwriter, result.job)
