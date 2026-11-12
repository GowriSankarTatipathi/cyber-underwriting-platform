import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from evidence import tests as evidence_tests
from evidence.models import EvidenceReview
from evidence.rules import control_summary
from evidence.services import assess_control, publish_policy, review_evidence, review_restore
from workspaces.models import ChecklistVersion, Membership, Organization, SharingGrant
from workspaces.tests import make_user
from .models import Decision, ExceptionApproval, Finding, FindingEvent, FindingResponse, Report, ReportShare, ReviewSnapshot
from .permissions import visible_findings
from .services import approve_exception, approve_report, create_renewal, draft_report, prepare_decision, record_decision, respond, review_finding, revoke_exception, revoke_share, save_finding, share_report, snapshot_current, withdraw_report


@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic")
class WorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        evidence_tests.EvidenceTests.setUpTestData.__func__(cls)
        cls.reviewer = make_user("workflow-reviewer")
        Membership.objects.create(user=cls.reviewer, organization=cls.insurer, role="reviewer")
        cls.case.assignee = cls.reviewer
        cls.case.internal_notes = "INTERNAL-SECRET-NOTE"
        cls.case.save()
        other = Organization.objects.create(name="Other insurer", kind="insurer")
        Membership.objects.create(user=cls.outsider, organization=other, role="admin")

    setUp = evidence_tests.EvidenceTests.setUp
    login = evidence_tests.EvidenceTests.login
    upload = evidence_tests.EvidenceTests.upload
    record = evidence_tests.EvidenceTests.record

    def finding(self, item=None, **changes):
        item = item or self.restore_request
        data = dict(request=item, title=f"Follow up: {item.title}", description="Provide dated evidence and explain scope.",
            kind="evidence", priority="high", priority_reason="The insurer cannot verify this control.",
            owner=self.tech if item.assigned_to_id == self.tech.pk else self.owner,
            due_date=timezone.localdate() + timedelta(days=7), retest_of=None)
        data.update(changes)
        return save_finding(self.underwriter, self.case, data)

    def decision(self, outcome="deferred", conditions=()):
        snapshot = prepare_decision(self.underwriter, self.case)
        return record_decision(self.underwriter, snapshot, outcome, "INTERNAL-DECISION-RATIONALE", conditions, True)

    def report(self):
        decision = self.decision()
        report = draft_report(self.reviewer, decision, "Business review", "Please provide the outstanding evidence.")
        approve_report(self.underwriter, report, "Reviewed for business sharing.")
        return report

    def share(self, report=None):
        return share_report(self.underwriter, report or self.report(), self.owner, timezone.now() + timedelta(days=3))

    def test_complete_retest_decision_report_and_renewal_journey(self):
        first = self.upload()
        failed = self.record(first)
        review_restore(self.reviewer, failed, True, "Seven-hour recovery missed four-hour target.")
        finding = self.finding(kind="retest", retest_of=failed)
        self.login(self.tech)
        self.assertContains(self.client.get(reverse("workflow", args=[self.case.pk])), finding.title)
        second = self.upload(document=first.document, body=b"Three hour retest log")
        retest = self.record(second, retest_of=failed, duration_minutes=Decimal("180"), performed_at=timezone.now() - timedelta(hours=2))
        respond(self.tech, finding, "Adjusted recovery procedure; retest completed.", [second], retest)
        finding.refresh_from_db()
        with self.assertRaises(ValidationError):
            review_finding(self.reviewer, finding, "close", "Not yet reviewed", finding.revision)
        review_restore(self.reviewer, retest, True, "Three-hour retest and service checks verified.")
        review_finding(self.reviewer, finding, "close", "Accepted the verified retest.", finding.revision)
        assess_control(self.reviewer, self.restore_request, "verified", "Latest retest passed.", [second])
        self.assertEqual(control_summary(self.access_request)["status"], "Missing")
        open_findings = [self.finding(item) for item in (self.access_request, self.backup_request, self.control_request)]
        decision = self.decision("conditional", open_findings)
        self.assertEqual(len(decision.conditions), 3)
        report = draft_report(self.reviewer, decision, "Conditional review", "Restore retest passed. Remaining evidence is due as listed.")
        approve_report(self.underwriter, report, "Approved for the business owner.")
        share = self.share(report)
        self.login(self.owner)
        response = self.client.get(reverse("shared_report", args=[share.pk]))
        self.assertContains(response, "Restore retest passed")
        self.assertContains(response, "Access review")
        self.assertNotContains(response, "INTERNAL-SECRET-NOTE")
        checklist = ChecklistVersion.objects.create(insurer=self.insurer, version="renewal-2", requirements=[{"name": "Access review", "evidence": "New signed review", "freshness_days": 90}], recovery_targets={"duration_minutes": 240, "data_age_minutes": 60})
        renewal, _, _ = create_renewal(self.underwriter, self.case, checklist, timezone.localdate() + timedelta(days=90), self.owner.email)
        self.assertEqual(renewal.renewal_of, self.case)
        self.assertEqual(renewal.applicant, self.case.applicant)
        self.assertFalse(renewal.consent_active)
        self.assertFalse(renewal.grants.exists())
        self.assertFalse(renewal.decisions.exists())
        self.assertEqual(renewal.requests.get().freshness_days, 90)
        self.login(self.underwriter)
        history = self.client.get(reverse("case_history", args=[renewal.pk]))
        self.assertContains(history, self.case.reference)
        saved = self.client.get(reverse("decision_detail", args=[self.case.pk, decision.pk]))
        self.assertContains(saved, "420.00")
        self.assertContains(saved, "180.00")
        self.assertContains(saved, "INTERNAL-DECISION-RATIONALE")

    def test_form_pages_and_recording_decision_via_http(self):
        finding = self.finding()
        snapshot = prepare_decision(self.underwriter, self.case)
        self.login(self.underwriter)
        for name, args in [("workflow", [self.case.pk]), ("finding_create", [self.case.pk]),
            ("finding_detail", [self.case.pk, finding.pk]), ("finding_edit", [self.case.pk, finding.pk]),
            ("finding_respond", [self.case.pk, finding.pk]), ("finding_review", [self.case.pk, finding.pk]),
            ("exception_create", [self.case.pk, finding.pk]), ("renewal_create", [self.case.pk])]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        response = self.client.post(reverse("decision_review", args=[self.case.pk, snapshot.pk]), {"outcome": "deferred", "rationale": "Pending evidence", "confirm": "on"})
        self.assertEqual(response.status_code, 302)
        decision = Decision.objects.get()
        self.assertContains(self.client.get(response.url), "Pending evidence")
        self.assertEqual(self.client.get(reverse("report_create", args=[self.case.pk, decision.pk])).status_code, 200)

    def test_finding_assignment_rejects_unassigned_or_cross_case_owner(self):
        for owner in (self.outsider, self.tech):
            with self.subTest(owner=owner):
                with self.assertRaises(ValidationError):
                    self.finding(self.access_request, owner=owner)

    def test_provider_scope_applies_to_findings_and_responses(self):
        assigned = self.finding()
        other = self.finding(self.access_request)
        self.login(self.tech)
        response = self.client.get(reverse("workflow", args=[self.case.pk]))
        self.assertContains(response, assigned.title)
        self.assertNotContains(response, other.title)
        for name in ("finding_detail", "finding_respond"):
            self.assertEqual(self.client.get(reverse(name, args=[self.case.pk, other.pk])).status_code, 404)
        with self.assertRaises(Http404):
            respond(self.tech, other, "Cross-scope", [])

    def test_reviewers_manage_findings_but_cannot_make_decisions_exceptions_or_report_approvals(self):
        finding = self.finding()
        self.assertEqual(save_finding(self.reviewer, self.case, {**self.finding_data(finding), "priority": "medium"}, finding, finding.revision).priority, "medium")
        with self.assertRaises(Http404):
            prepare_decision(self.reviewer, self.case)
        with self.assertRaises(Http404):
            approve_exception(self.reviewer, finding, "Reason", "Controls", timezone.now() + timedelta(days=1))
        report = draft_report(self.reviewer, self.decision(), "Draft", "Summary")
        with self.assertRaises(Http404):
            approve_report(self.reviewer, report, "Not authorized")

    def finding_data(self, finding):
        return {field: getattr(finding, field) for field in ("request", "title", "description", "kind", "priority", "priority_reason", "owner", "due_date", "retest_of")}

    def test_findings_have_priority_history_and_optimistic_edit_conflicts(self):
        finding = self.finding()
        updated = save_finding(self.underwriter, self.case, {**self.finding_data(finding), "priority": "low", "priority_reason": "Narrowed scope."}, finding, 1)
        self.assertEqual(updated.events.count(), 2)
        self.assertEqual(updated.events.first().snapshot["priority"], "high")
        with self.assertRaises(ValidationError):
            save_finding(self.underwriter, self.case, self.finding_data(finding), finding, 1)

    def test_response_does_not_close_and_only_reviewer_can_close(self):
        finding = self.finding(self.control_request, kind="fix")
        version = self.upload(self.control_request)
        respond(self.tech, finding, "MFA enabled and checked.", [version])
        finding.refresh_from_db()
        self.assertEqual(finding.state, "submitted")
        with self.assertRaises(Http404):
            review_finding(self.tech, finding, "close", "Self approved", finding.revision)
        review_finding(self.reviewer, finding, "close", "Fix verified", finding.revision)
        finding.refresh_from_db()
        self.assertEqual(finding.state, "closed")
        review_finding(self.reviewer, finding, "reopen", "New information needs follow-up", finding.revision)
        finding.refresh_from_db()
        self.assertEqual(finding.state, "open")
        self.assertEqual(finding.events.count(), 4)

    def test_closing_rejects_no_evidence_unaccepted_and_obsolete_versions(self):
        finding = self.finding()
        respond(self.tech, finding, "Need more time", [])
        finding.refresh_from_db()
        with self.assertRaises(ValidationError):
            review_finding(self.reviewer, finding, "close", "No proof", finding.revision)
        version = self.upload(accept=False)
        respond(self.tech, finding, "Uploaded", [version])
        finding.refresh_from_db()
        with self.assertRaises(ValidationError):
            review_finding(self.reviewer, finding, "close", "Unreviewed", finding.revision)
        review_evidence(self.reviewer, version, "accepted", True, "Accepted")
        self.upload(document=version.document, body=b"New replacement")
        with self.assertRaises(ValidationError):
            review_finding(self.reviewer, finding, "close", "Old version", finding.revision)

    def test_response_rejects_wrong_request_evidence_and_unrelated_retest(self):
        finding = self.finding()
        wrong = self.upload(self.control_request)
        with self.assertRaises(ValidationError):
            respond(self.tech, finding, "Wrong evidence", [wrong])
        first = self.upload()
        test = self.record(first)
        finding = self.finding(kind="retest", retest_of=test)
        unrelated = self.record(first, duration_minutes=Decimal("180"))
        with self.assertRaises(ValidationError):
            respond(self.tech, finding, "Unlinked test", [first], unrelated)

    def test_request_changes_and_stale_review_do_not_overwrite_new_response(self):
        finding = self.finding()
        respond(self.tech, finding, "Please clarify scope", [])
        with self.assertRaises(ValidationError):
            review_finding(self.reviewer, finding, "request_changes", "Need dates", 1)
        finding.refresh_from_db()
        review_finding(self.reviewer, finding, "request_changes", "Provide observation dates.", finding.revision)
        finding.refresh_from_db()
        self.assertEqual(finding.state, "open")

    def test_exception_expiry_revocation_and_immutability(self):
        finding = self.finding()
        with self.assertRaises(ValidationError):
            approve_exception(self.underwriter, finding, "Reason", "Controls", timezone.now() - timedelta(seconds=1))
        expires = timezone.now() + timedelta(days=1)
        exception = approve_exception(self.underwriter, finding, "Temporary tolerance", "Daily manual check", expires)
        self.assertTrue(exception.active)
        with patch("django.utils.timezone.now", return_value=expires):
            self.assertFalse(exception.active)
        with self.assertRaises(ValidationError):
            exception.save()
        revoke_exception(self.underwriter, exception, "Tolerance removed")
        exception.refresh_from_db()
        self.assertFalse(exception.active)
        finding.refresh_from_db()
        self.assertEqual(finding.state, "open")

    def test_unconditional_approval_cannot_ignore_missing_evidence(self):
        with self.assertRaises(ValidationError):
            self.decision("approved")

    def test_all_verified_controls_allow_unconditional_approval(self):
        self.access_request.assigned_to = self.tech
        self.access_request.save()
        for item in self.case.requests.all():
            version = self.upload(item)
            if item.kind == "restore":
                test = self.record(version, duration_minutes=Decimal("180"))
                review_restore(self.reviewer, test, True, "Passing restore verified")
            assess_control(self.reviewer, item, "verified", "Evidence scope verified", [version])
        decision = self.decision("approved")
        self.assertEqual(decision.outcome, "approved")
        self.assertEqual(decision.conditions, [])

    def test_finding_and_report_forms_submit_complete_http_workflow(self):
        self.login(self.underwriter)
        fields = {"title": "Check MFA", "request": self.control_request.pk, "kind": "fix", "description": "Verify coverage",
            "priority": "high", "priority_reason": "Privileged accounts need verification", "owner": self.tech.pk,
            "due_date": (timezone.localdate() + timedelta(days=3)).isoformat()}
        response = self.client.post(reverse("finding_create", args=[self.case.pk]), fields)
        self.assertEqual(response.status_code, 302, response.context["form"].errors if response.context else None)
        finding = Finding.objects.get()
        fields.update(revision=finding.revision, priority="medium")
        response = self.client.post(reverse("finding_edit", args=[self.case.pk, finding.pk]), fields)
        self.assertEqual(response.status_code, 302, response.context["form"].errors if response.context else None)
        finding.refresh_from_db()
        self.assertEqual(finding.priority, "medium")
        version = self.upload(self.control_request)
        self.login(self.tech)
        response = self.client.post(reverse("finding_respond", args=[self.case.pk, finding.pk]), {"message": "Fix completed", "evidence_versions": [version.pk]})
        self.assertEqual(response.status_code, 302)
        finding.refresh_from_db()
        self.login(self.underwriter)
        response = self.client.post(reverse("finding_review", args=[self.case.pk, finding.pk]), {"action": "close", "rationale": "Verified", "revision": finding.revision})
        self.assertEqual(response.status_code, 302)
        finding.refresh_from_db()
        self.assertEqual(finding.state, "closed")
        decision = self.decision()
        response = self.client.post(reverse("report_create", args=[self.case.pk, decision.pk]), {"title": "Business report", "summary": "Evidence still pending"})
        self.assertEqual(response.status_code, 302)
        report = Report.objects.get()
        self.assertContains(self.client.get(response.url), "DRAFT")
        response = self.client.post(reverse("report_action", args=[self.case.pk, report.pk, "approve"]), {"rationale": "Checked for sharing", "confirm": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response.url), "APPROVED REPORT")
        response = self.client.post(reverse("report_share", args=[self.case.pk, report.pk]), {"recipient": self.owner.pk, "expires_at": (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")})
        self.assertContains(response, "Secure report link ready")
        share = ReportShare.objects.get()
        response = self.client.post(reverse("share_revoke", args=[self.case.pk, share.pk]), {"rationale": "Review completed", "confirm": "on"})
        self.assertEqual(response.status_code, 302)
        self.login(self.owner)
        self.assertEqual(self.client.get(reverse("shared_report", args=[share.pk])).status_code, 404)

    def test_exception_forms_record_and_revoke_without_closing_finding(self):
        finding = self.finding()
        self.login(self.underwriter)
        response = self.client.post(reverse("exception_create", args=[self.case.pk, finding.pk]), {
            "rationale": "Short-term tolerance", "compensating_controls": "Daily manual checks",
            "expires_at": (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(response.status_code, 302)
        exception = ExceptionApproval.objects.get()
        response = self.client.post(reverse("exception_revoke", args=[self.case.pk, exception.pk]), {"rationale": "Tolerance ended", "confirm": "on"})
        self.assertEqual(response.status_code, 302)
        exception.refresh_from_db()
        finding.refresh_from_db()
        self.assertFalse(exception.active)
        self.assertEqual(finding.state, "open")

    def test_disabled_or_reassigned_provider_loses_followup_access(self):
        finding = self.finding()
        self.restore_request.assigned_to = None
        self.restore_request.save()
        with self.assertRaises(Http404):
            respond(self.tech, finding, "Reassigned", [])
        self.restore_request.assigned_to = self.tech
        self.restore_request.save()
        self.tech.is_active = False
        self.tech.save()
        with self.assertRaises(Http404):
            respond(self.tech, finding, "Disabled", [])

    def test_withdrawn_report_cannot_be_reshared_with_cached_object(self):
        report = self.report()
        self.assertTrue(report.approved)
        withdraw_report(self.underwriter, report, "Withdrawn")
        with self.assertRaises(ValidationError):
            self.share(report)

    def test_conditional_approval_requires_explicit_coverage_of_every_gap(self):
        one = self.finding(self.access_request)
        with self.assertRaises(ValidationError):
            self.decision("conditional", [one])
        with self.assertRaises(ValidationError):
            self.decision("conditional")
        findings = [one] + [self.finding(item) for item in (self.restore_request, self.backup_request, self.control_request)]
        decision = self.decision("conditional", findings)
        self.assertEqual(len(decision.conditions), 4)

    def test_active_exception_can_cover_a_gap_but_expired_exception_cannot(self):
        findings = [self.finding(item) for item in self.case.requests.all()]
        exception = approve_exception(self.underwriter, findings[0], "Temporary", "Manual validation", timezone.now() + timedelta(days=1))
        self.decision("conditional", findings[1:])
        revoke_exception(self.underwriter, exception, "Expired tolerance")
        with self.assertRaises(ValidationError):
            self.decision("conditional", findings[1:])

    def test_snapshot_changes_on_new_upload_review_policy_or_finding(self):
        version = self.upload()
        for action in (lambda: review_evidence(self.reviewer, version, "rejected", False, "Scope mismatch"),
            lambda: publish_policy(self.reviewer, self.service, {"duration_minutes": 120, "data_age_minutes": 30, "required_scope": "Full service"}),
            lambda: self.finding(), lambda: self.upload(document=version.document, body=b"Revised evidence")):
            snapshot = prepare_decision(self.underwriter, self.case)
            self.assertTrue(snapshot_current(snapshot))
            action()
            self.assertFalse(snapshot_current(snapshot))
            with self.assertRaises(ValidationError):
                record_decision(self.underwriter, snapshot, "deferred", "Stale", [], True)

    def test_expiry_between_review_and_submit_invalidates_snapshot(self):
        finding = self.finding()
        expires = timezone.now() + timedelta(hours=1)
        approve_exception(self.underwriter, finding, "Temporary", "Controls", expires)
        snapshot = prepare_decision(self.underwriter, self.case)
        with patch("django.utils.timezone.now", return_value=expires):
            with self.assertRaises(ValidationError):
                record_decision(self.underwriter, snapshot, "deferred", "Changed", [], True)

    def test_decision_snapshot_is_immutable_and_preserves_exact_review_versions(self):
        version = self.upload()
        original_review = version.latest_review
        decision = self.decision()
        original_payload = json.dumps(decision.snapshot.payload, sort_keys=True)
        self.upload(document=version.document, body=b"New content")
        review_evidence(self.reviewer, version, "rejected", False, "Later correction")
        decision.refresh_from_db()
        self.assertEqual(json.dumps(decision.snapshot.payload, sort_keys=True), original_payload)
        self.assertIn(version.sha256, original_payload)
        self.assertIn(str(original_review.pk), original_payload)
        for obj in (decision, decision.snapshot):
            with self.assertRaises(ValidationError):
                obj.save()
            with self.assertRaises(ValidationError):
                obj.delete()

    def test_two_pending_reviews_cannot_silently_supersede_a_new_decision(self):
        first = prepare_decision(self.underwriter, self.case)
        second = prepare_decision(self.underwriter, self.case)
        record_decision(self.underwriter, first, "deferred", "First", [], True)
        with self.assertRaises(ValidationError):
            record_decision(self.underwriter, second, "declined", "Stale review", [], True)
        newest = self.decision("declined")
        self.assertEqual(newest.number, 2)

    def test_report_allowlist_excludes_internal_notes_and_decision_rationale(self):
        report = self.report()
        payload = json.dumps(report.payload)
        for secret in ("INTERNAL-SECRET-NOTE", "INTERNAL-DECISION-RATIONALE", "storage_key", "storage_bucket"):
            self.assertNotIn(secret, payload)
        self.assertEqual(report.payload["snapshot_sha256"], report.decision.snapshot.sha256)

    def test_report_cannot_be_shared_before_approval_or_to_provider(self):
        report = draft_report(self.reviewer, self.decision(), "Draft", "Public summary")
        with self.assertRaises(ValidationError):
            self.share(report)
        approve_report(self.underwriter, report, "Checked")
        with self.assertRaises(ValidationError):
            share_report(self.underwriter, report, self.tech, timezone.now() + timedelta(days=1))
        with self.assertRaises(ValidationError):
            share_report(self.underwriter, report, self.owner, self.owner_grant.expires_at + timedelta(seconds=1))

    def test_approval_rejects_stale_or_superseded_decision(self):
        report = draft_report(self.reviewer, self.decision(), "Draft", "Summary")
        self.finding()
        with self.assertRaises(ValidationError):
            approve_report(self.underwriter, report, "Stale report")
        self.decision("declined")
        with self.assertRaises(ValidationError):
            approve_report(self.underwriter, report, "Superseded report")

    def test_shared_report_requires_exact_recipient_mfa_and_active_access(self):
        share = self.share()
        url = reverse("shared_report", args=[share.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        for user in (self.tech, self.outsider, self.underwriter):
            self.login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.get(url + "?download=json").status_code, 404)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url + "?download=json").status_code, 200)
        SharingGrant.objects.filter(pk=self.owner_grant.pk).update(revoked_at=timezone.now())
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_share_expiry_revocation_and_report_withdrawal_block_download(self):
        share = self.share()
        self.login(self.owner)
        url = reverse("shared_report", args=[share.pk]) + "?download=json"
        with patch("django.utils.timezone.now", return_value=share.expires_at):
            self.assertFalse(share.active)
        # Keep the browser session current while exercising an expired grant.
        ReportShare.objects.filter(pk=share.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(url).status_code, 404)
        ReportShare.objects.filter(pk=share.pk).update(expires_at=share.expires_at)
        revoke_share(self.underwriter, share, "Recipient no longer needs report")
        self.assertEqual(self.client.get(url).status_code, 404)
        other = self.share(share.report)
        withdraw_report(self.underwriter, share.report, "Report replaced")
        self.assertEqual(self.client.get(reverse("shared_report", args=[other.pk]) + "?download=json").status_code, 404)

    def test_private_decisions_and_drafts_never_leak_to_contributors(self):
        decision = self.decision()
        report = draft_report(self.reviewer, decision, "Private draft", "Not released")
        for user in (self.owner, self.tech, self.outsider):
            self.login(user)
            for name, ident in (("decision_detail", decision.pk), ("report_detail", report.pk)):
                for suffix in ("", "?download=json"):
                    self.assertEqual(self.client.get(reverse(name, args=[self.case.pk, ident]) + suffix).status_code, 404)

    def test_revoked_consent_blocks_insurer_decisions_and_provider_work(self):
        finding = self.finding()
        decision = self.decision()
        self.case.consent_revoked_at = timezone.now()
        self.case.save()
        with self.assertRaises(Http404):
            prepare_decision(self.underwriter, self.case)
        with self.assertRaises(Http404):
            respond(self.tech, finding, "Blocked", [])
        self.login(self.underwriter)
        self.assertEqual(self.client.get(reverse("decision_detail", args=[self.case.pk, decision.pk])).status_code, 404)

    def test_new_renewal_access_does_not_grant_prior_case_history(self):
        self.decision()
        renewal, _, _ = create_renewal(self.underwriter, self.case, self.checklist, timezone.localdate() + timedelta(days=10), self.owner.email)
        self.case.created_by = self.reviewer
        self.case.assignee = self.reviewer
        self.case.save()
        self.login(self.underwriter)
        response = self.client.get(reverse("case_history", args=[renewal.pk]))
        self.assertNotContains(response, self.case.reference)

    def test_renewal_rejects_cross_insurer_product_or_duplicate(self):
        with self.assertRaises(ValidationError):
            create_renewal(self.underwriter, self.case, self.checklist, timezone.localdate(), self.owner.email)
        self.decision()
        wrong = ChecklistVersion.objects.create(insurer=self.insurer, product="Different product", version="v1")
        with self.assertRaises(ValidationError):
            create_renewal(self.underwriter, self.case, wrong, timezone.localdate(), self.owner.email)
        create_renewal(self.underwriter, self.case, self.checklist, timezone.localdate(), self.owner.email)
        with self.assertRaises(ValidationError):
            create_renewal(self.underwriter, self.case, self.checklist, timezone.localdate(), self.owner.email)

    def test_csrf_and_post_only_preparation(self):
        self.login(self.underwriter)
        url = reverse("decision_prepare", args=[self.case.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.cookies = self.client.cookies
        self.assertEqual(client.post(url).status_code, 403)
        self.assertFalse(ReviewSnapshot.objects.exists())

    def test_cross_case_ids_cannot_be_used_in_routes(self):
        finding = self.finding()
        decision = self.decision()
        renewal, _, _ = create_renewal(self.underwriter, self.case, self.checklist, timezone.localdate(), self.owner.email)
        renewal.consent_granted_by = self.owner
        renewal.consent_expires_at = timezone.now() + timedelta(days=2)
        renewal.save()
        self.login(self.underwriter)
        self.assertEqual(self.client.get(reverse("finding_detail", args=[renewal.pk, finding.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("decision_detail", args=[renewal.pk, decision.pk])).status_code, 404)
