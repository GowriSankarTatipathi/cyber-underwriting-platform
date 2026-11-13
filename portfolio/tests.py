from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import math
from io import StringIO
from unittest.mock import patch
from allauth.account.models import EmailAddress
from django.core.management import call_command, CommandError
from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from evidence import tests as evidence_tests
from evidence.models import Service
from underwriting.models import Decision, ReportWithdrawal
from underwriting.services import approve_exception, approve_report, draft_report, prepare_decision, record_decision, save_finding
from workspaces.models import Case, Membership, Organization, SharingGrant
from workspaces.tests import make_user
from . import services
from .analytics import validate_dataset
from .models import Outcome, Permission, Relationship, Submission, ValidationRun


@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic")
class PortfolioTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        evidence_tests.EvidenceTests.setUpTestData.__func__(cls)

    setUp = evidence_tests.EvidenceTests.setUp
    login = evidence_tests.EvidenceTests.login

    def permission(self):
        return services.authorize(self.owner, self.case, timezone.now() + timedelta(days=10), True)

    def decision(self, historical=False):
        snapshot = prepare_decision(self.underwriter, self.case)
        decision = record_decision(self.underwriter, snapshot, "deferred", "PRIVATE-DECISION-RATIONALE", [], True)
        if historical:
            Decision.objects.filter(pk=decision.pk).update(created_at=timezone.now() - timedelta(days=400))
            decision.refresh_from_db()
        return decision

    def outcome_data(self, **extra):
        data = dict(decision=self.decision(historical=True), period_start=timezone.localdate() - timedelta(days=300),
            period_end=timezone.localdate() - timedelta(days=100), incidents=2, incurred_loss=Decimal("12000.00"),
            currency="USD", source="Fictional loss register", maturity="mature", rationale="Closed observed period; no outstanding reserve changes.", supersedes=None)
        data.update(extra)
        return data

    def dependency(self, **extra):
        data = dict(service=self.service, provider_name="Shared Cloud", provider_key="shared.example", critical=True,
            source="Reviewed inventory version 1", observed_at=timezone.now() - timedelta(days=1))
        data.update(extra)
        return services.record_dependency(self.underwriter, self.case, **data)

    def submission(self, released=False):
        self.case.internal_notes = "PRIVATE-CASE-NOTE"
        self.case.save()
        report = draft_report(self.underwriter, self.decision(), "Partner report", "Outstanding evidence needs review.")
        approve_report(self.underwriter, report, "Reviewed summary for named partner.")
        relationship = services.create_relationship(self.underwriter, self.case, "Fictional Capacity", self.outsider, "Existing agreement 1", timezone.now() + timedelta(days=5), True)
        submission = services.prepare_submission(self.underwriter, self.case, relationship, report, timezone.now() + timedelta(days=2))
        if released:
            services.consent_submission(self.owner, submission, True)
            services.release_submission(self.underwriter, submission, "Business approved the exact report.")
        return submission

    def test_owner_permission_separate_scoped_expiring_and_revocable(self):
        with self.assertRaises(ValidationError):
            self.dependency()
        for user in (self.underwriter, self.tech, self.outsider):
            with self.subTest(user=user.email), self.assertRaises(Http404):
                services.authorize(user, self.case, timezone.now() + timedelta(days=1), True)
        with self.assertRaises(ValidationError):
            services.authorize(self.owner, self.case, timezone.now() + timedelta(days=40), True)
        permission = self.permission()
        self.assertTrue(services.permission_active(permission))
        self.dependency()
        services.revoke_permission(self.owner, permission)
        self.assertEqual(services.concentration(self.underwriter, self.insurer), [])
        self.permission()
        self.assertEqual(services.concentration(self.underwriter, self.insurer), [])

    def test_expired_or_revoked_owner_grant_removes_research(self):
        permission = self.permission()
        self.dependency()
        SharingGrant.objects.filter(pk=self.owner_grant.pk).update(revoked_at=timezone.now())
        self.assertFalse(services.permission_active(permission))
        self.assertEqual(services.concentration(self.underwriter, self.insurer), [])

    def test_outcome_links_decision_snapshot_and_appends_correction(self):
        self.permission()
        data = self.outcome_data()
        first = services.record_outcome(self.underwriter, self.case, data)
        second = services.record_outcome(self.underwriter, self.case, {**data, "incurred_loss": Decimal("13500"), "supersedes": first})
        self.assertEqual(second.decision.snapshot.sha256, data["decision"].snapshot.sha256)
        self.assertEqual(services.outcomes(self.underwriter, self.insurer), [second])
        first.refresh_from_db()
        self.assertEqual(first.incurred_loss, Decimal("12000"))
        with self.assertRaises(ValidationError):
            first.save()
        with self.assertRaises(ValidationError):
            services.record_outcome(self.underwriter, self.case, {**data, "supersedes": first})

    def test_outcome_requires_chronology_valid_loss_and_currency(self):
        self.permission()
        data = self.outcome_data()
        for bad in ({"period_start": timezone.localdate() - timedelta(days=500)}, {"period_end": timezone.localdate() + timedelta(days=1)},
                    {"period_end": data["period_start"]}, {"incidents": 0}, {"incurred_loss": Decimal("-1")},
                    {"incurred_loss": Decimal("NaN")}, {"currency": "US"}, {"maturity": "unverified"}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                services.record_outcome(self.underwriter, self.case, {**data, **bad})
        row = services.record_outcome(self.underwriter, self.case, {**data, "incidents": 0, "incurred_loss": Decimal("0"), "currency": "usd"})
        self.assertEqual(row.currency, "USD")

    def test_overlapping_applicant_periods_cannot_double_count(self):
        self.permission()
        data = self.outcome_data()
        services.record_outcome(self.underwriter, self.case, data)
        with self.assertRaises(ValidationError):
            services.record_outcome(self.underwriter, self.case, data)
        renewal = Case.objects.create(reference="RENEWAL", insurer=self.insurer, applicant=self.business, checklist=self.checklist,
            service="Ordering", created_by=self.underwriter, assignee=self.underwriter, renewal_of=self.case,
            consent_granted_by=self.owner, consent_expires_at=timezone.now() + timedelta(days=20))
        SharingGrant.objects.create(case=renewal, user=self.owner, role="owner", granted_by=self.underwriter, expires_at=timezone.now() + timedelta(days=20))
        services.authorize(self.owner, renewal, timezone.now() + timedelta(days=10), True)
        # Independent historical decision on the renewal, but same applicant/exposure.
        snapshot = prepare_decision(self.underwriter, renewal)
        decision = record_decision(self.underwriter, snapshot, "deferred", "History", [], True)
        Decision.objects.filter(pk=decision.pk).update(created_at=timezone.now() - timedelta(days=400))
        decision.refresh_from_db()
        with self.assertRaises(ValidationError):
            services.record_outcome(self.underwriter, renewal, {**data, "decision": decision})

    def test_dependencies_count_distinct_cases_and_retirement(self):
        self.permission()
        first = self.dependency()
        with self.assertRaises(ValidationError):
            self.dependency(provider_key="SHARED.EXAMPLE")
        second_service = Service.objects.create(case=self.case, name="Email", created_by=self.underwriter)
        self.dependency(service=second_service, observed_at=timezone.now() - timedelta(days=100))
        group = services.concentration(self.underwriter, self.insurer)[0]
        self.assertEqual((group["case_count"], group["critical_count"], group["stale"]), (1, 1, 1))
        services.retire_dependency(self.underwriter, first, "Moved service to a different provider")
        self.assertEqual(len(services.concentration(self.underwriter, self.insurer)), 1)

    def test_portfolio_respects_tenant_and_case_assignment(self):
        self.permission()
        self.dependency()
        reviewer = make_user("unassigned-portfolio-reviewer")
        Membership.objects.create(user=reviewer, organization=self.insurer, role="reviewer")
        other = Organization.objects.create(name="Other insurer", kind="insurer")
        Membership.objects.create(user=self.outsider, organization=other, role="admin")
        for user in (reviewer, self.outsider, self.tech, self.owner):
            self.assertEqual(services.concentration(user, self.insurer), [])
            self.assertEqual(services.outcomes(user, self.insurer), [])

    def test_validation_insufficient_does_not_invent_metrics(self):
        run = services.validate_model(self.underwriter, self.case, timezone.localdate() - timedelta(days=365), "USD")
        self.assertEqual(run.metrics["status"], "insufficient_data")
        self.assertNotIn("baseline", run.metrics)
        self.assertEqual(run.dataset, [])
        self.assertEqual(self.case.decisions.count(), 0)

    def test_validation_filters_maturity_currency_and_preserves_provenance(self):
        self.permission()
        row = services.record_outcome(self.underwriter, self.case, self.outcome_data())
        run = services.validate_model(self.underwriter, self.case, timezone.localdate() - timedelta(days=90), "USD")
        self.assertEqual(run.dataset[0]["snapshot_sha256"], row.decision.snapshot.sha256)
        self.assertEqual(run.dataset[0]["permission_id"], row.permission_id)
        self.assertGreater(run.dataset[0]["years"], 0)
        other_currency = services.validate_model(self.underwriter, self.case, timezone.localdate() - timedelta(days=90), "EUR")
        self.assertEqual(other_currency.dataset, [])
        Outcome.objects.filter(pk=row.pk).update(maturity="developing")
        developing = services.validate_model(self.underwriter, self.case, timezone.localdate() - timedelta(days=90), "USD")
        self.assertEqual(developing.dataset, [])

    def test_revoked_permission_blocks_saved_validation_and_export(self):
        permission = self.permission()
        services.record_outcome(self.underwriter, self.case, self.outcome_data())
        run = services.validate_model(self.underwriter, self.case, timezone.localdate() - timedelta(days=90), "USD")
        self.login(self.underwriter)
        url = reverse("portfolio_validation_export", args=[self.case.pk, run.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        services.revoke_permission(self.owner, permission)
        with self.assertRaises(Http404):
            services.require_validation(self.underwriter, run)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_revoked_outcomes_do_not_appear_in_correction_choices(self):
        permission = self.permission()
        row = services.record_outcome(self.underwriter, self.case, self.outcome_data())
        services.revoke_permission(self.owner, permission)
        self.permission()
        self.login(self.underwriter)
        response = self.client.get(reverse("portfolio_outcome", args=[self.case.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(row.pk, list(response.context["form"].fields["supersedes"].queryset.values_list("pk", flat=True)))

    def test_partner_summary_preserves_approved_exceptions_without_internal_rationale(self):
        finding = save_finding(self.underwriter, self.case, dict(request=self.access_request, title="Access review exception", description="Review pending",
            kind="evidence", priority="high", priority_reason="Access exposure", owner=self.owner,
            due_date=timezone.localdate() + timedelta(days=7), retest_of=None))
        exception = approve_exception(self.underwriter, finding, "PRIVATE-EXCEPTION-RATIONALE", "Daily access monitoring", timezone.now() + timedelta(days=3))
        submission = self.submission()
        self.assertEqual(submission.payload["exceptions"][0]["finding"], finding.title)
        self.assertEqual(submission.payload["exceptions"][0]["compensating_controls"], exception.compensating_controls)
        self.assertNotIn("PRIVATE-EXCEPTION-RATIONALE", str(submission.payload))
        self.assertEqual(submission.payload["report_sha256"], submission.report.sha256)

    def test_capacity_requires_owner_then_underwriter_release(self):
        submission = self.submission()
        with self.assertRaises(Http404):
            services.require_partner(self.outsider, submission)
        with self.assertRaises(ValidationError):
            services.release_submission(self.underwriter, submission, "Release")
        services.consent_submission(self.owner, submission, True)
        with self.assertRaises(Http404):
            services.require_partner(self.outsider, submission)
        with self.assertRaises(Http404):
            services.release_submission(self.tech, submission, "Release")
        services.release_submission(self.underwriter, submission, "Business approved exact report")
        self.assertEqual(services.require_partner(self.outsider, submission).pk, submission.pk)
        with self.assertRaises(Http404):
            services.require_partner(self.tech, submission)

    def test_capacity_complete_http_journey_and_no_case_access(self):
        submission = self.submission()
        self.login(self.owner)
        url = reverse("capacity_consent", args=[self.case.pk, submission.pk])
        self.assertContains(self.client.get(url), "Outstanding evidence needs review")
        self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 302)
        self.login(self.underwriter)
        url = reverse("capacity_release", args=[self.case.pk, submission.pk])
        self.assertEqual(self.client.post(url, {"rationale": "Approved to release"}).status_code, 302)
        self.login(self.outsider)
        self.assertContains(self.client.get(reverse("capacity_inbox")), self.case.reference)
        url = reverse("capacity_partner", args=[submission.pk])
        self.assertEqual(self.client.post(url, {"status": "clarification", "message": "Please clarify recovery scope."}).status_code, 302)
        self.assertContains(self.client.get(url), "Please clarify recovery scope")
        self.assertEqual(self.client.get(reverse("case", args=[self.case.pk])).status_code, 404)
        response = self.client.get(reverse("capacity_export", args=[submission.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "PRIVATE-CASE-NOTE")
        self.assertNotContains(response, "PRIVATE-DECISION-RATIONALE")
        self.assertNotContains(response, "storage_key")
        self.assertNotContains(response, self.owner.email)
        self.assertEqual(Decision.objects.count(), 1)

    def test_partner_access_rechecked_for_case_owner_report_and_relationship(self):
        submission = self.submission(released=True)
        for model, pk, changes in ((Case, self.case.pk, {"consent_revoked_at": timezone.now()}),
            (SharingGrant, self.owner_grant.pk, {"revoked_at": timezone.now()}),
            (Relationship, submission.relationship_id, {"expires_at": timezone.now() - timedelta(days=1)}),
            (Submission, submission.pk, {"expires_at": timezone.now() - timedelta(days=1)})):
            original = model.objects.filter(pk=pk).values(*changes).get()
            model.objects.filter(pk=pk).update(**changes)
            with self.subTest(model=model.__name__), self.assertRaises(Http404):
                services.require_partner(self.outsider, submission)
            model.objects.filter(pk=pk).update(**original)
        ReportWithdrawal.objects.create(report=submission.report, actor=self.underwriter, rationale="Report correction required")
        with self.assertRaises(Http404):
            services.require_partner(self.outsider, submission)

    def test_relationship_revocation_blocks_all_its_submissions(self):
        submission = self.submission(released=True)
        services.revoke_relationship(self.underwriter, self.case, submission.relationship)
        with self.assertRaises(Http404):
            services.require_partner(self.outsider, submission)
        with self.assertRaises(ValidationError):
            services.prepare_submission(self.underwriter, self.case, submission.relationship, submission.report, timezone.now() + timedelta(days=1))

    def test_owner_submission_revocation_blocks_response_and_export(self):
        submission = self.submission(released=True)
        services.revoke_submission(self.owner, submission)
        with self.assertRaises(Http404):
            services.partner_response(self.outsider, submission, "received", "Received")
        self.login(self.outsider)
        self.assertEqual(self.client.get(reverse("capacity_export", args=[submission.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("capacity_inbox")), self.case.reference)

    def test_new_decision_or_changed_evidence_blocks_release(self):
        submission = self.submission()
        services.consent_submission(self.owner, submission, True)
        self.decision()
        with self.assertRaises(ValidationError):
            services.release_submission(self.underwriter, submission, "Old snapshot")

    def test_tampered_payload_is_not_shared(self):
        submission = self.submission(released=True)
        Submission.objects.filter(pk=submission.pk).update(payload={"altered": True})
        with self.assertRaises(Http404):
            services.require_partner(self.outsider, submission)

    def test_form_pages_render_for_expected_roles(self):
        self.permission()
        dependency = self.dependency()
        submission = self.submission()
        run = services.validate_model(self.underwriter, self.case, timezone.localdate() - timedelta(days=1), "USD")
        self.login(self.underwriter)
        urls = [("portfolio", [self.case.pk]), ("portfolio_outcome", [self.case.pk]), ("portfolio_dependency", [self.case.pk]),
            ("portfolio_retire", [self.case.pk, dependency.pk]), ("portfolio_validate", [self.case.pk]),
            ("portfolio_validation_detail", [self.case.pk, run.pk]), ("capacity_relationship", [self.case.pk]),
            ("capacity_prepare", [self.case.pk]), ("capacity_detail", [self.case.pk, submission.pk]), ("capacity_release", [self.case.pk, submission.pk])]
        for name, args in urls:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        self.login(self.owner)
        self.assertEqual(self.client.get(reverse("portfolio_authorize", args=[self.case.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("portfolio", args=[self.case.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("portfolio_outcome", args=[self.case.pk])).status_code, 404)
        self.login(self.tech)
        self.assertEqual(self.client.get(reverse("portfolio", args=[self.case.pk])).status_code, 404)

    def test_post_forms_reject_invalid_inputs_without_server_errors(self):
        self.permission()
        self.login(self.underwriter)
        response = self.client.post(reverse("portfolio_dependency", args=[self.case.pk]), {
            "service": self.service.pk, "provider_name": "Cloud", "provider_key": "cloud.example", "critical": "on",
            "source": "Inventory", "observed_at": (timezone.now() - timedelta(days=1)).isoformat()})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse("portfolio_validate", args=[self.case.pk]), {"currency": "USD", "cutoff": timezone.localdate().isoformat()})
        self.assertContains(response, "Choose a past holdout cutoff")
        response = self.client.post(reverse("capacity_relationship", args=[self.case.pk]), {"partner_name": "Partner", "recipient_email": "missing@example.test", "confirm": "on", "contract_reference": "Contract", "expires_at": (timezone.now() + timedelta(days=1)).isoformat()})
        self.assertContains(response, "An active account with this exact email")

    def test_valid_outcome_relationship_and_submission_forms(self):
        self.permission()
        data = self.outcome_data()
        data["decision"] = data["decision"].pk
        data["supersedes"] = ""
        self.login(self.underwriter)
        response = self.client.post(reverse("portfolio_outcome", args=[self.case.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Outcome.objects.count(), 1)
        response = self.client.post(reverse("capacity_relationship", args=[self.case.pk]), {"partner_name": "Partner", "recipient_email": self.outsider.email,
            "confirm": "on", "contract_reference": "Contract", "expires_at": (timezone.now() + timedelta(days=5)).isoformat()})
        self.assertEqual(response.status_code, 302)
        relationship = Relationship.objects.get()
        report = draft_report(self.underwriter, self.decision(), "Shareable report", "Summary")
        approve_report(self.underwriter, report, "Reviewed")
        response = self.client.post(reverse("capacity_prepare", args=[self.case.pk]), {"relationship": relationship.pk, "report": report.pk,
            "expires_at": (timezone.now() + timedelta(days=2)).isoformat()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Submission.objects.get().relationship, relationship)

    def test_partner_provisioning_grants_no_case_or_staff_access_and_never_resets(self):
        password = "Partner-test-aR7#94cf2mE"
        with patch("portfolio.management.commands.provision_capacity_partner.getpass", return_value=password):
            call_command("provision_capacity_partner", email="capacity@example.test", email_verified=True, stdout=StringIO())
        address = EmailAddress.objects.get(email="capacity@example.test")
        user = address.user
        self.assertTrue(address.verified)
        self.assertTrue(user.check_password(password))
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertFalse(Membership.objects.filter(user=user).exists())
        self.assertFalse(SharingGrant.objects.filter(user=user).exists())
        with self.assertRaises(CommandError):
            call_command("provision_capacity_partner", email=user.email, email_verified=True, stdout=StringIO())

    def test_cross_insurer_relationship_and_cross_case_report_rejected(self):
        submission = self.submission()
        other = Organization.objects.create(name="Other capacity insurer", kind="insurer")
        relationship = Relationship.objects.create(insurer=other, partner_name="Other partner", recipient=self.outsider,
            contract_reference="Other contract", expires_at=timezone.now() + timedelta(days=5), created_by=self.underwriter)
        with self.assertRaises(Http404):
            services.prepare_submission(self.underwriter, self.case, relationship, submission.report, timezone.now() + timedelta(days=1))
        with self.assertRaises(Http404):
            services.revoke_relationship(self.underwriter, self.case, relationship)
        unrelated = Case.objects.create(reference="UNRELATED", insurer=self.insurer, applicant=self.business, checklist=self.checklist,
            service="Ordering", created_by=self.underwriter, assignee=self.underwriter, consent_granted_by=self.owner, consent_expires_at=timezone.now() + timedelta(days=20))
        with self.assertRaises(Http404):
            services.prepare_submission(self.underwriter, unrelated, submission.relationship, submission.report, timezone.now() + timedelta(days=1))

    def test_partner_response_requires_csrf_and_mfa(self):
        submission = self.submission(released=True)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.outsider, backend="django.contrib.auth.backends.ModelBackend")
        with override_settings(LOCAL_PASSWORD_ONLY=True):
            response = client.post(reverse("capacity_partner", args=[submission.pk]), {"status": "received", "message": "Test"})
        self.assertEqual(response.status_code, 403)
        with override_settings(LOCAL_PASSWORD_ONLY=False):
            response = client.get(reverse("capacity_partner", args=[submission.pk]))
            self.assertEqual(response.status_code, 302)
            self.assertIn("reauthenticate", response.url)


class AnalyticsTests(SimpleTestCase):
    def dataset(self):
        rows = []
        for i in range(40):
            train = i < 30
            high = i % 2 == 0
            rows.append({"id": str(i), "applicant": str(i), "start": "2020-01-01" if train else "2022-01-01",
                "end": "2021-01-01" if train else "2023-01-01", "years": 1., "incidents": 2 if high else 1,
                "loss": 4000. if high else 1000., "band": "higher" if high else "lower"})
        return rows

    def test_transparent_frequency_severity_and_heldout_metrics(self):
        result = validate_dataset(self.dataset(), "2022-01-01")
        self.assertEqual(result["status"], "research_evaluation")
        self.assertAlmostEqual(result["baseline"]["frequency"], 1.5)
        self.assertAlmostEqual(result["baseline"]["severity"], 2500 / 1.5)
        self.assertEqual(result["risk_bands"]["higher"], {"frequency": 2., "severity": 2000.})
        self.assertEqual(result["banded_holdout"]["mean_count_poisson_deviance"], 0.)
        self.assertEqual(result["banded_holdout"]["incident_weighted_severity_mae"], 0.)

    def test_holdout_outcomes_never_fit_coefficients(self):
        rows = self.dataset()
        before = validate_dataset(rows, "2022-01-01")
        for row in rows[30:]:
            row["incidents"] = 50
            row["loss"] = 1000000.
        after = validate_dataset(rows, "2022-01-01")
        self.assertEqual(before["baseline"], after["baseline"])
        self.assertEqual(before["risk_bands"], after["risk_bands"])
        self.assertNotEqual(before["banded_holdout"], after["banded_holdout"])

    def test_applicant_separation_and_cross_cutoff_exclusion(self):
        rows = self.dataset()
        old = {**rows[30], "id": "old-holdout", "start": "2020-01-01", "end": "2021-01-01"}
        crossing = {**rows[0], "id": "crossing", "start": "2021-01-01", "end": "2023-01-01"}
        result = validate_dataset(rows + [old, crossing], "2022-01-01")
        self.assertNotIn("old-holdout", result["train_ids"])
        self.assertNotIn("crossing", result["train_ids"] + result["holdout_ids"])
        self.assertEqual(result["excluded_rows"], 2)

    def test_empty_event_band_falls_back_without_infinite_predictions(self):
        rows = deepcopy(self.dataset())
        for row in rows[:30]:
            if row["band"] == "lower":
                row["incidents"], row["loss"] = 0, 0.
        result = validate_dataset(rows, "2022-01-01")
        self.assertEqual(result["risk_bands"]["lower"], result["baseline"])
        self.assertTrue(math.isfinite(result["banded_holdout"]["mean_count_poisson_deviance"]))

    def test_small_sample_does_not_fit(self):
        result = validate_dataset(self.dataset()[:5], "2022-01-01")
        self.assertEqual(result["status"], "insufficient_data")
        self.assertNotIn("risk_bands", result)
