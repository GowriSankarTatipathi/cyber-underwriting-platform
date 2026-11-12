import hashlib
import io
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid
from datetime import timedelta
from decimal import Decimal

import boto3
from moto import mock_aws
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from workspaces.models import Case, ChecklistVersion, EvidenceRequest, Membership, Organization, SharingGrant
from workspaces.tests import make_user
from .models import ControlAssessment, Document, EvidenceReview, EvidenceVersion, FileScan, RecoveryPolicy, RestoreReview, RestoreTest, Service
from .rules import control_summary, current_restore_status, evaluate_restore, freshness
from .services import assess_control, ensure_case_service, publish_policy, record_backup, record_restore, rescan_version, review_evidence, review_restore, upload_evidence
from .storage import StorageUnavailable, check_s3, put_private, read_private


@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic")
class EvidenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.underwriter = make_user("evidence-reviewer")
        cls.owner = make_user("evidence-owner")
        cls.tech = make_user("evidence-tech")
        cls.outsider = make_user("evidence-outsider")
        cls.insurer = Organization.objects.create(name="Evidence Insurer", kind="insurer")
        cls.business = Organization.objects.create(name="Evidence Business", kind="business")
        Membership.objects.create(user=cls.underwriter, organization=cls.insurer, role="underwriter")
        cls.checklist = ChecklistVersion.objects.create(insurer=cls.insurer, version="evidence-1", requirements=[], recovery_targets={"duration_minutes": 240, "data_age_minutes": 60})
        cls.case = Case.objects.create(reference="EVIDENCE-001", insurer=cls.insurer, applicant=cls.business, checklist=cls.checklist, service="Ordering", created_by=cls.underwriter, assignee=cls.underwriter, consent_granted_by=cls.owner, consent_expires_at=timezone.now() + timedelta(days=30))
        cls.owner_grant = SharingGrant.objects.create(case=cls.case, user=cls.owner, role="owner", granted_by=cls.underwriter, expires_at=timezone.now() + timedelta(days=30))
        cls.tech_grant = SharingGrant.objects.create(case=cls.case, user=cls.tech, role="technician", granted_by=cls.owner, expires_at=timezone.now() + timedelta(days=30))
        cls.restore_request = EvidenceRequest.objects.create(case=cls.case, kind="restore", title="Supervised restore", description="Restore evidence", freshness_days=90, assigned_to=cls.tech)
        cls.backup_request = EvidenceRequest.objects.create(case=cls.case, kind="backup", title="Backup coverage", description="Backup evidence", freshness_days=30, assigned_to=cls.tech)
        cls.control_request = EvidenceRequest.objects.create(case=cls.case, kind="control", title="MFA", description="MFA evidence", freshness_days=90, assigned_to=cls.tech)
        cls.access_request = EvidenceRequest.objects.create(case=cls.case, kind="control", title="Access review", description="Missing access review", freshness_days=180)
        cls.service = ensure_case_service(cls.case)
        for user in (cls.underwriter, cls.owner, cls.tech, cls.outsider):
            TOTP.activate(user, generate_totp_secret())

    def setUp(self):
        cache.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = override_settings(EVIDENCE_LOCAL_ROOT=self.directory.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.observed = timezone.now() - timedelta(days=2)

    def login(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        session = self.client.session
        session["account_authentication_methods"] = [{"method": "password", "at": time.time()}, {"method": "mfa", "at": time.time()}]
        session["account_authentication_methods_user"] = str(user.pk)
        session.save()

    def upload(self, item=None, body=b"Fictional restore evidence", document=None, accept=True, observed=None, key=None, name="evidence.txt"):
        item = item or self.restore_request
        version, _ = upload_evidence(self.tech, item, SimpleUploadedFile(name, body), self.service,
            "Restore log", "Test log", "Ordering and dependencies", observed or self.observed,
            key or uuid.uuid4(), document)
        if accept:
            review_evidence(self.underwriter, version, "accepted", True, "Source and service scope checked.")
        return version

    def record(self, version, **extra):
        data = dict(service=self.service, retest_of=None, performed_at=timezone.now() - timedelta(days=1),
            scope="Ordering and dependencies", supervised_by="Fictional technician", duration_minutes=Decimal("420"),
            data_age_minutes=Decimal("35"), functional_checks_passed=True, functional_checks="Created and retrieved a test order.", evidence_versions=[version])
        data.update(extra)
        return record_restore(self.tech, self.restore_request, data)

    def test_initial_and_linked_retest_keep_distinct_results_and_access_gap(self):
        initial_version = self.upload()
        initial = self.record(initial_version)
        first_review = review_restore(self.underwriter, initial, True, "Seven hours exceeds the target.")
        self.assertEqual(first_review.result, "Target missed")
        second_version = self.upload(body=b"Retest completed in three hours", document=initial_version.document)
        retest = self.record(second_version, retest_of=initial, duration_minutes=Decimal("180"), data_age_minutes=Decimal("25"), performed_at=timezone.now() - timedelta(hours=1))
        second_review = review_restore(self.underwriter, retest, True, "Three-hour retest verified.")
        self.assertEqual(second_review.result, "Passed")
        self.assertEqual(retest.retest_of, initial)
        initial.refresh_from_db()
        self.assertEqual(initial.duration_minutes, Decimal("420"))
        self.assertEqual(initial.latest_review.result, "Target missed")
        self.assertEqual(control_summary(self.access_request)["status"], "Missing")
        self.assertNotEqual(initial_version.storage_key, second_version.storage_key)
        self.assertEqual(read_private(initial_version), b"Fictional restore evidence")

    def test_missing_measurements_stay_inconclusive(self):
        version = self.upload()
        for field in ("duration_minutes", "data_age_minutes", "functional_checks_passed"):
            with self.subTest(field=field):
                test = self.record(version, **{field: None})
                self.assertEqual(evaluate_restore(test, True)[0], "Inconclusive")

    def test_functional_failure_and_partial_scope(self):
        version = self.upload()
        test = self.record(version, duration_minutes=Decimal("180"), functional_checks_passed=False)
        self.assertEqual(evaluate_restore(test, True)[0], "Failed")
        test = self.record(version, duration_minutes=Decimal("180"))
        self.assertEqual(evaluate_restore(test, False)[0], "Partial")
        self.assertEqual(evaluate_restore(test, None)[0], "Inconclusive")

    def test_data_age_target_and_exact_boundaries(self):
        version = self.upload()
        test = self.record(version, duration_minutes=Decimal("240"), data_age_minutes=Decimal("60"))
        self.assertEqual(evaluate_restore(test, True)[0], "Passed")
        test = self.record(version, duration_minutes=Decimal("240"), data_age_minutes=Decimal("60.01"))
        self.assertEqual(evaluate_restore(test, True)[0], "Target missed")
        test = self.record(version, duration_minutes=Decimal("0"), data_age_minutes=Decimal("0"))
        self.assertEqual(evaluate_restore(test, True)[0], "Passed")

    def test_empty_functional_description_is_inconclusive(self):
        test = self.record(self.upload(), duration_minutes=Decimal("180"), functional_checks="")
        self.assertEqual(evaluate_restore(test, True)[0], "Inconclusive")

    def test_negative_measurements_rejected(self):
        version = self.upload()
        with self.assertRaises(ValidationError):
            self.record(version, duration_minutes=Decimal("-1"))

    def test_future_upload_and_future_test_rejected(self):
        with self.assertRaises(ValidationError):
            self.upload(observed=timezone.now() + timedelta(days=1))
        version = self.upload()
        with self.assertRaises(ValidationError):
            self.record(version, performed_at=timezone.now() + timedelta(days=1))

    def test_stale_evidence_and_stale_tests_do_not_pass(self):
        version = self.upload(observed=timezone.now() - timedelta(days=91))
        test = self.record(version, duration_minutes=Decimal("180"))
        self.assertEqual(evaluate_restore(test, True)[0], "Inconclusive")
        version = self.upload()
        test = self.record(version, duration_minutes=Decimal("180"), performed_at=timezone.now() - timedelta(days=91))
        self.assertEqual(evaluate_restore(test, True)[0], "Inconclusive")

    def test_exact_freshness_boundary_and_unknown_future(self):
        at = timezone.now()
        self.assertEqual(freshness(at - timedelta(days=90), 90, at), "Fresh")
        self.assertEqual(freshness(at - timedelta(days=90, seconds=1), 90, at), "Stale")
        self.assertEqual(freshness(at + timedelta(seconds=1), 90, at), "Unknown")

    def test_unreviewed_evidence_does_not_pass_restore(self):
        version = self.upload(accept=False)
        test = self.record(version, duration_minutes=Decimal("180"))
        self.assertEqual(evaluate_restore(test, True)[0], "Inconclusive")

    def test_control_traces_exact_evidence_and_invalidates_on_new_version(self):
        first = self.upload(item=self.control_request)
        assessment = assess_control(self.underwriter, self.control_request, "verified", "MFA scope confirmed.", [first])
        self.assertEqual(control_summary(self.control_request)["status"], "Verified")
        self.assertEqual(assessment.review_snapshot[str(first.pk)]["sha256"], first.sha256)
        self.upload(item=self.control_request, body=b"Changed MFA scope", document=first.document, accept=False)
        self.assertEqual(control_summary(self.control_request)["status"], "Needs review")
        self.assertEqual(list(assessment.evidence_versions.all()), [first])

    def test_control_cannot_verify_stale_or_unaccepted_versions(self):
        version = self.upload(item=self.control_request, accept=False)
        with self.assertRaises(ValidationError):
            assess_control(self.underwriter, self.control_request, "verified", "Unreviewed", [version])
        stale = self.upload(item=self.control_request, body=b"Old report", observed=timezone.now() - timedelta(days=91))
        with self.assertRaises(ValidationError):
            assess_control(self.underwriter, self.control_request, "verified", "Stale", [stale])

    def test_restore_control_requires_latest_passing_test(self):
        version = self.upload()
        with self.assertRaises(ValidationError):
            assess_control(self.underwriter, self.restore_request, "verified", "No test", [version])
        test = self.record(version, duration_minutes=Decimal("180"))
        review_restore(self.underwriter, test, True, "Verified")
        assess_control(self.underwriter, self.restore_request, "verified", "Meets recovery requirements", [version])
        self.assertEqual(control_summary(self.restore_request)["status"], "Verified")
        self.record(version, duration_minutes=Decimal("420"), performed_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(control_summary(self.restore_request)["status"], "Needs review")

    def test_later_rejection_invalidates_review_without_deleting_history(self):
        version = self.upload()
        test = self.record(version, duration_minutes=Decimal("180"))
        reviewed = review_restore(self.underwriter, test, True, "Initial review")
        review_evidence(self.underwriter, version, "rejected", False, "Found a scope discrepancy.")
        self.assertEqual(current_restore_status(test), "Needs review")
        self.assertEqual(reviewed.result, "Passed")
        self.assertEqual(version.reviews.count(), 2)

    def test_new_target_version_preserves_prior_result(self):
        version = self.upload()
        test = self.record(version, duration_minutes=Decimal("180"))
        review_restore(self.underwriter, test, True, "Verified")
        publish_policy(self.underwriter, self.service, {"duration_minutes": Decimal("120"), "data_age_minutes": Decimal("30"), "required_scope": "Ordering plus all dependencies"})
        test.refresh_from_db()
        self.assertEqual(test.policy.duration_minutes, Decimal("240"))
        self.assertEqual(current_restore_status(test), "Prior target version")
        new = self.record(version, duration_minutes=Decimal("180"), data_age_minutes=Decimal("25"))
        self.assertEqual(evaluate_restore(new, True)[0], "Target missed")

    def test_versions_reviews_tests_are_immutable(self):
        version = self.upload()
        test = self.record(version)
        review = review_restore(self.underwriter, test, True, "Verified measurements")
        for obj in (version, test, review, version.latest_review, test.policy):
            with self.subTest(model=type(obj).__name__):
                with self.assertRaises(ValidationError):
                    obj.save()
                with self.assertRaises(ValidationError):
                    obj.delete()

    def test_duplicate_retry_and_duplicate_version_do_not_create_extra_records(self):
        key = uuid.uuid4()
        first = self.upload(key=key, accept=False)
        duplicate = self.upload(key=key, accept=False)
        repeated = self.upload(document=first.document, accept=False)
        self.assertEqual(first.pk, duplicate.pk)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(EvidenceVersion.objects.count(), 1)

    def test_changed_submission_with_same_key_rejected(self):
        key = uuid.uuid4()
        self.upload(key=key)
        with self.assertRaises(ValidationError):
            self.upload(key=key, body=b"Changed contents")

    def test_storage_failure_does_not_create_document_or_version(self):
        with patch("evidence.services.put_private", side_effect=StorageUnavailable("Offline")):
            with self.assertRaises(StorageUnavailable):
                self.upload()
        self.assertFalse(Document.objects.exists())
        self.assertFalse(EvidenceVersion.objects.exists())

    def test_revocation_during_storage_upload_is_rechecked(self):
        real_put = put_private
        def revoked(*args, **kwargs):
            pointer = real_put(*args, **kwargs)
            SharingGrant.objects.filter(pk=self.tech_grant.pk).update(revoked_at=timezone.now())
            return pointer
        with patch("evidence.services.put_private", side_effect=revoked):
            with self.assertRaises(Http404):
                self.upload()
        self.assertFalse(EvidenceVersion.objects.exists())
        self.assertFalse(list(Path(self.directory.name).rglob("*.txt")))

    @override_settings(EVIDENCE_SCAN_MODE="external", EVIDENCE_SCAN_COMMAND='["missing-scanner"]')
    def test_scanner_failure_quarantines_and_prevents_download_or_acceptance(self):
        version = self.upload(accept=False)
        self.assertEqual(version.latest_scan.status, "quarantined")
        self.login(self.underwriter)
        self.assertEqual(self.client.get(reverse("evidence_download", args=[self.case.pk, version.pk])).status_code, 404)
        with self.assertRaises(ValidationError):
            review_evidence(self.underwriter, version, "accepted", True, "Cannot bypass quarantine")
        with self.assertRaises(ValidationError):
            self.record(version)
        with override_settings(EVIDENCE_SCAN_MODE="basic"):
            rescan_version(self.underwriter, version)
        self.assertEqual(version.latest_scan.status, "clean")

    def test_wrong_file_type_and_malware_test_rejected(self):
        version = self.upload(body=b"This is not a PDF", name="evidence.pdf", accept=False)
        self.assertEqual(version.latest_scan.status, "rejected")
        version = self.upload(body=b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE", accept=False)
        self.assertEqual(version.latest_scan.status, "rejected")
        with self.assertRaises(ValidationError):
            self.upload(name="evidence.exe")

    def test_empty_and_oversized_files_rejected(self):
        with self.assertRaises(ValidationError):
            self.upload(body=b"")
        with override_settings(EVIDENCE_MAX_BYTES=4):
            with self.assertRaises(ValidationError):
                self.upload(body=b"too long")

    def test_download_requires_permission_and_checks_content_hash(self):
        version = self.upload()
        self.login(self.tech)
        response = self.client.get(reverse("evidence_download", args=[self.case.pk, version.pk]))
        self.assertEqual(b"".join(response.streaming_content), b"Fictional restore evidence")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        path = Path(self.directory.name) / version.storage_key
        path.write_bytes(b"Tampered")
        self.assertEqual(self.client.get(reverse("evidence_download", args=[self.case.pk, version.pk])).status_code, 503)

    def test_revocation_during_download_blocks_response(self):
        version = self.upload()
        self.login(self.tech)
        def revoked(item):
            SharingGrant.objects.filter(pk=self.tech_grant.pk).update(revoked_at=timezone.now())
            return b"Fictional restore evidence"
        with patch("evidence.views.read_private", side_effect=revoked):
            self.assertEqual(self.client.get(reverse("evidence_download", args=[self.case.pk, version.pk])).status_code, 404)

    def test_cross_case_and_revoked_access_denied_all_evidence_surfaces(self):
        version = self.upload()
        test = self.record(version)
        urls = [reverse("evidence_request", args=[self.case.pk, self.restore_request.pk]),
            reverse("evidence_upload", args=[self.case.pk, self.restore_request.pk]),
            reverse("evidence_download", args=[self.case.pk, version.pk]),
            reverse("restore_detail", args=[self.case.pk, test.pk]), reverse("service_inventory", args=[self.case.pk])]
        for user in [self.outsider, self.tech]:
            if user == self.tech:
                SharingGrant.objects.filter(pk=self.tech_grant.pk).update(revoked_at=timezone.now())
            self.login(user)
            for url in urls:
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, 404)

    def test_contributor_cannot_review_change_targets_or_assess(self):
        version = self.upload()
        test = self.record(version)
        self.login(self.tech)
        for url in [reverse("version_review", args=[self.case.pk, version.pk]), reverse("control_review", args=[self.case.pk, self.restore_request.pk]), reverse("policy_create", args=[self.case.pk, self.service.pk])]:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(reverse("restore_detail", args=[self.case.pk, test.pk]), {"scope_confirmed": "yes", "rationale": "Self-approval"}).status_code, 404)

    def test_retest_must_match_service_and_be_later(self):
        version = self.upload()
        test = self.record(version)
        with self.assertRaises(ValidationError):
            self.record(version, retest_of=test, performed_at=test.performed_at - timedelta(minutes=1))
        other = Service.objects.create(case=self.case, name="Other service", owner_name="Owner", created_by=self.owner)
        publish_policy(self.underwriter, other, {"duration_minutes": Decimal("240"), "data_age_minutes": Decimal("60"), "required_scope": "Other service"})
        with self.assertRaises(ValidationError):
            self.record(version, service=other, retest_of=test, performed_at=timezone.now() - timedelta(hours=1))

    def test_wrong_request_evidence_cannot_support_test(self):
        version = self.upload(item=self.control_request)
        with self.assertRaises(ValidationError):
            self.record(version)

    def test_backup_success_does_not_create_or_pass_restore(self):
        version = self.upload(item=self.backup_request)
        job = record_backup(self.tech, self.backup_request, {"service": self.service, "observed_at": self.observed, "status": "success", "coverage": "Database and configuration", "source_version": version})
        self.assertEqual(job.status, "success")
        self.assertFalse(RestoreTest.objects.exists())
        self.assertEqual(control_summary(self.restore_request)["status"], "Missing")

    def test_complete_upload_review_restore_retest_ui_journey(self):
        self.login(self.tech)
        url = reverse("evidence_upload", args=[self.case.pk, self.restore_request.pk])
        response = self.client.post(url, {"submission_key": uuid.uuid4(), "service": self.service.pk, "title": "Restore report", "file": SimpleUploadedFile("restore.txt", b"Total recovery time: 7 hours"), "source": "Supervised log", "scope": "Ordering and dependencies", "observed_at": self.observed.strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(response.status_code, 302)
        version = EvidenceVersion.objects.get()
        self.login(self.underwriter)
        response = self.client.post(reverse("version_review", args=[self.case.pk, version.pk]), {"decision": "accepted", "scope_confirmed": "on", "rationale": "Scope verified"})
        self.assertEqual(response.status_code, 302)
        self.login(self.tech)
        response = self.client.post(reverse("restore_create", args=[self.case.pk, self.restore_request.pk]), {"service": self.service.pk, "performed_at": (timezone.now()-timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"), "supervised_by": "Sample supervisor", "scope": "Ordering and dependencies", "duration_minutes": "420", "data_age_minutes": "35", "functional_checks_passed": "yes", "functional_checks": "Created test order", "evidence_versions": [version.pk]})
        self.assertEqual(response.status_code, 302, response.content[:1000])
        test = RestoreTest.objects.get()
        self.login(self.underwriter)
        response = self.client.post(reverse("restore_detail", args=[self.case.pk, test.pk]), {"scope_confirmed": "yes", "rationale": "Measurements verified"}, follow=True)
        self.assertContains(response, "Target missed")
        self.assertContains(self.client.get(reverse("evidence_request", args=[self.case.pk, self.restore_request.pk])), "420.00")
        self.assertContains(self.client.get(reverse("case", args=[self.case.pk])), "Missing")

    def test_csrf_protects_file_submission(self):
        self.client = Client(enforce_csrf_checks=True)
        self.login(self.tech)
        self.assertEqual(self.client.post(reverse("evidence_upload", args=[self.case.pk, self.restore_request.pk]), {}).status_code, 403)

    def test_upload_handler_stops_oversized_multipart_file(self):
        self.login(self.tech)
        with override_settings(EVIDENCE_MAX_BYTES=4):
            response = self.client.post(reverse("evidence_upload", args=[self.case.pk, self.restore_request.pk]), {
                "submission_key": uuid.uuid4(), "service": self.service.pk, "title": "Large upload",
                "file": SimpleUploadedFile("large.txt", b"more than four bytes"), "source": "Log", "scope": "Ordering",
                "observed_at": self.observed.strftime("%Y-%m-%dT%H:%M"),
            })
        self.assertContains(response, "Files must be no larger than 10 MB")
        self.assertFalse(EvidenceVersion.objects.exists())

    def test_new_critical_service_requires_its_own_restore(self):
        version = self.upload()
        test = self.record(version, duration_minutes=Decimal("180"))
        review_restore(self.underwriter, test, True, "First service verified")
        assess_control(self.underwriter, self.restore_request, "verified", "Initial scope verified", [version])
        Service.objects.create(case=self.case, name="Payroll", owner_name="Payroll owner", created_by=self.owner)
        self.assertEqual(control_summary(self.restore_request)["status"], "Needs review")
        with self.assertRaises(ValidationError):
            assess_control(self.underwriter, self.restore_request, "verified", "Cannot ignore payroll", [version])

    def test_scoped_export_contains_version_provenance_not_storage_locations(self):
        version = self.upload()
        test = self.record(version)
        review_restore(self.underwriter, test, True, "Seven hours verified")
        self.login(self.tech)
        response = self.client.get(reverse("export_case", args=[self.case.pk]))
        payload = response.json()
        self.assertEqual(payload["schema_version"], 2)
        entry = next(item for item in payload["requests"] if item["kind"] == "restore")
        self.assertEqual(entry["documents"][0]["versions"][0]["sha256"], version.sha256)
        self.assertEqual(entry["restore_tests"][0]["reviewed_result"], "Target missed")
        self.assertNotContains(response, version.storage_key)
        self.assertNotIn("internal_notes", payload)

    def test_bad_retest_query_returns_not_found(self):
        self.login(self.tech)
        self.assertEqual(self.client.get(reverse("restore_create", args=[self.case.pk, self.restore_request.pk]) + "?retest=invalid").status_code, 404)

    def test_valid_pdf_and_png_pass_structural_checks(self):
        from pypdf import PdfWriter
        from PIL import Image
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        buffer = io.BytesIO()
        writer.write(buffer)
        pdf = self.upload(body=buffer.getvalue(), name="report.pdf", accept=False)
        self.assertEqual(pdf.latest_scan.status, "clean")
        buffer = io.BytesIO()
        Image.new("RGB", (10, 10), "white").save(buffer, format="PNG")
        png = self.upload(body=buffer.getvalue(), name="report.png", accept=False)
        self.assertEqual(png.latest_scan.status, "clean")

    @override_settings(EVIDENCE_SCAN_MODE="external", EVIDENCE_SCAN_COMMAND='["scanner"]')
    def test_scanner_timeout_remains_quarantined(self):
        import subprocess
        with patch("evidence.scanning.subprocess.run", side_effect=subprocess.TimeoutExpired("scanner", 30)):
            version = self.upload(accept=False)
        self.assertEqual(version.latest_scan.status, "quarantined")


@override_settings(EVIDENCE_STORAGE_BACKEND="s3", EVIDENCE_S3_BUCKET="private-evidence-test", EVIDENCE_S3_REGION="us-east-1", EVIDENCE_S3_ENDPOINT_URL=None, EVIDENCE_S3_KMS_KEY_ID="")
class S3StorageTests(SimpleTestCase):
    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket="private-evidence-test")
        self.s3.put_bucket_versioning(Bucket="private-evidence-test", VersioningConfiguration={"Status": "Enabled"})
        self.s3.put_public_access_block(Bucket="private-evidence-test", PublicAccessBlockConfiguration={name: True for name in ["BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"]})
        self.s3.put_bucket_encryption(Bucket="private-evidence-test", ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})

    def test_s3_put_pins_version_checksum_and_encryption(self):
        data = b"private evidence"
        checksum = hashlib.sha256(data).hexdigest()
        pointer = put_private("cases/case/documents/doc/v1.txt", data, "text/plain", checksum)
        self.assertTrue(pointer["object_version"])
        obj = SimpleNamespace(**pointer, byte_size=len(data), sha256=checksum)
        self.assertEqual(read_private(obj), data)
        head = self.s3.head_object(Bucket=pointer["storage_bucket"], Key=pointer["storage_key"], VersionId=pointer["object_version"])
        self.assertEqual(head["ServerSideEncryption"], "AES256")
        self.assertEqual(head["Metadata"]["sha256"], checksum)
        # Replacing the current S3 object cannot change an already-reviewed version.
        self.s3.put_object(Bucket=pointer["storage_bucket"], Key=pointer["storage_key"], Body=b"new current object")
        self.assertEqual(read_private(obj), data)
        grants = self.s3.get_object_acl(Bucket=pointer["storage_bucket"], Key=pointer["storage_key"])["Grants"]
        self.assertFalse(any("AllUsers" in grant["Grantee"].get("URI", "") for grant in grants))

    def test_unversioned_bucket_is_rejected(self):
        self.s3.put_bucket_versioning(Bucket="private-evidence-test", VersioningConfiguration={"Status": "Suspended"})
        with self.assertRaises(StorageUnavailable):
            check_s3()

    def test_public_bucket_configuration_is_rejected(self):
        self.s3.delete_public_access_block(Bucket="private-evidence-test")
        with self.assertRaises(StorageUnavailable):
            check_s3()

    def test_existing_key_cannot_be_overwritten_by_upload(self):
        data = b"one"
        put_private("test.txt", data, "text/plain", hashlib.sha256(data).hexdigest())
        with self.assertRaises(StorageUnavailable):
            put_private("test.txt", b"two", "text/plain", hashlib.sha256(b"two").hexdigest())

    def test_missing_s3_object_returns_storage_failure(self):
        obj = SimpleNamespace(storage_backend="s3", storage_bucket="private-evidence-test", storage_key="absent", object_version="missing", byte_size=1, sha256="0"*64)
        with self.assertRaises(StorageUnavailable):
            read_private(obj)
