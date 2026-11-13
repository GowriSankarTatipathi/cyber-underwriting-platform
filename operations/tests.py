from datetime import timedelta
from pathlib import Path
import tempfile
from unittest.mock import patch
from cryptography.fernet import Fernet
from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from ai_review.processing import run_job
from ai_review.services import queue_review
from ai_review.test_extraction import LOG
from evidence import tests as evidence_tests
from underwriting.services import respond, save_finding
from workspaces.models import Case
from .models import ExtractionEvaluation, FollowupEvaluation, ReviewSession
from .recovery import CHUNK, RecoveryError, decrypt, encrypt
from .services import evaluate_extraction, evaluate_followup, metrics, timer


@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic", AI_REVIEW_PROVIDER="demo")
class PilotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        evidence_tests.EvidenceTests.setUpTestData.__func__(cls)

    setUp = evidence_tests.EvidenceTests.setUp
    upload = evidence_tests.EvidenceTests.upload
    login = evidence_tests.EvidenceTests.login

    def result(self):
        job, _ = queue_review(self.underwriter, self.upload(body=LOG))
        run_job(job.pk)
        return job.result

    def finding(self):
        return save_finding(self.underwriter, self.case, {"request": self.restore_request, "title": "Retest evidence", "kind": "evidence", "description": "Provide supporting evidence", "priority": "high", "priority_reason": "Missing verification",
            "owner": self.tech, "due_date": timezone.localdate() + timedelta(days=7), "retest_of": None})

    def test_review_timer_excludes_open_sessions_and_prevents_double_count(self):
        session = timer(self.underwriter, self.case, "start")
        self.assertEqual(metrics(self.underwriter, self.case)["review_minutes"], 0)
        with self.assertRaises(ValidationError):
            timer(self.underwriter, self.case, "start")
        ReviewSession.objects.filter(pk=session.pk).update(started_at=timezone.now() - timedelta(minutes=10))
        timer(self.underwriter, self.case, "stop", "Reviewed restore log")
        self.assertAlmostEqual(metrics(self.underwriter, self.case)["review_minutes"], 10, delta=0.1)

    def test_independent_quality_metrics_have_denominators_and_supersede_assessments(self):
        result = self.result()
        total = len(result.facts)
        evaluate_extraction(self.underwriter, result, total + 1, total - 1, total - 2, "Gold labels checked independently")
        result_metrics = metrics(self.underwriter, self.case)
        self.assertEqual(result_metrics["fact_precision"], round((total - 1) / total, 4))
        self.assertEqual(result_metrics["fact_recall"], round((total - 1) / (total + 1), 4))
        evaluate_extraction(self.underwriter, result, total, total, total, "Reconciled gold labels")
        self.assertEqual(metrics(self.underwriter, self.case)["evaluated_extractions"], 1)
        self.assertEqual(ExtractionEvaluation.objects.count(), 2)
        with self.assertRaises(ValidationError):
            evaluate_extraction(self.underwriter, result, total, total + 1, total, "Invalid")

    def test_invalid_citations_stay_in_quality_denominator(self):
        from ai_review.validation import validate_extraction
        facts, _ = validate_extraction({"facts": [{"field": "restore_duration_minutes", "value": "420", "citations": [{"passage_id": "invented", "quote": "invented"}]}], "warnings": []}, [])
        self.assertEqual(facts[0]["submitted_citation_count"], 1)
        self.assertEqual(facts[0]["citations"], [])

    def test_first_response_assessment_is_fixed_to_first_response_and_never_closes_finding(self):
        finding = self.finding()
        first = respond(self.tech, finding, "First attempt", [])
        respond(self.tech, finding, "Second attempt", [])
        evaluation = evaluate_followup(self.underwriter, finding, False, "The first response omitted scope")
        self.assertEqual(evaluation.first_response, first)
        self.assertEqual(metrics(self.underwriter, self.case)["first_response_success_rate"], 0)
        finding.refresh_from_db()
        self.assertEqual(finding.state, "submitted")

    def test_empty_metrics_are_null_not_perfect_scores(self):
        result = metrics(self.underwriter, self.case)
        for field in ("fact_precision", "fact_recall", "citation_accuracy", "first_response_success_rate"):
            self.assertIsNone(result[field])

    def test_role_revocation_and_http_pages(self):
        result = self.result()
        finding = self.finding()
        respond(self.tech, finding, "Response", [])
        self.login(self.underwriter)
        for name, args in [("pilot_metrics", [self.case.pk]), ("pilot_metrics_export", [self.case.pk]),
            ("pilot_evaluate", [self.case.pk, "extraction", result.pk]), ("pilot_evaluate", [self.case.pk, "followup", finding.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        for user in (self.tech, self.owner, self.outsider):
            with self.assertRaises(Http404):
                metrics(user, self.case)
        Case.objects.filter(pk=self.case.pk).update(consent_revoked_at=timezone.now())
        self.assertEqual(self.client.get(reverse("pilot_metrics_export", args=[self.case.pk])).status_code, 404)


class RecoveryCryptoTests(SimpleTestCase):
    def test_large_bundle_roundtrip_and_tamper_wrong_key_truncation_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source, encrypted = folder / "source", folder / "bundle"
            data = b"private evidence\n" * (CHUNK // 8)
            source.write_bytes(data)
            key = Fernet(Fernet.generate_key())
            encrypt(source, encrypted, key)
            self.assertNotIn(b"private evidence", encrypted.read_bytes())
            restored = folder / "restored"
            decrypt(encrypted, restored, key)
            self.assertEqual(restored.read_bytes(), data)
            with self.assertRaises(RecoveryError):
                decrypt(encrypted, folder / "wrong-key", Fernet(Fernet.generate_key()))
            original = encrypted.read_bytes()
            for index, damaged in enumerate((original[:-1], original[:100] + bytes([original[100] ^ 1]) + original[101:])):
                changed = folder / f"damaged-{index}"
                changed.write_bytes(damaged)
                with self.assertRaises(RecoveryError):
                    decrypt(changed, folder / f"output-{index}", key)

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "source", Path(folder) / "target"
            source.write_bytes(b"source")
            target.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                encrypt(source, target, Fernet(Fernet.generate_key()))
            self.assertEqual(target.read_bytes(), b"keep")
