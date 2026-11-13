from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import reverse
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from evidence import tests as evidence_tests
from portfolio import tests as portfolio_tests
from workspaces.models import Case, Membership
from workspaces.tests import make_user
from . import services


@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic")
class RelationshipAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        evidence_tests.EvidenceTests.setUpTestData.__func__(cls)

    setUp = evidence_tests.EvidenceTests.setUp
    login = evidence_tests.EvidenceTests.login
    decision = portfolio_tests.PortfolioTests.decision
    submission = portfolio_tests.PortfolioTests.submission

    def test_assigned_underwriter_cannot_revoke_another_creators_company_relationship(self):
        submission = self.submission(released=True)
        colleague = make_user("another-relationship-underwriter")
        TOTP.activate(colleague, generate_totp_secret())
        membership = Membership.objects.create(user=colleague, organization=self.insurer, role="underwriter")
        Case.objects.filter(pk=self.case.pk).update(assignee=colleague)
        with self.assertRaises(Http404):
            services.revoke_relationship(colleague, self.case, submission.relationship)
        self.assertTrue(services.relationship_active(submission.relationship))
        self.login(colleague)
        page = self.client.get(reverse("portfolio", args=[self.case.pk]))
        self.assertNotContains(page, 'action="' + reverse("capacity_relationship_revoke", args=[self.case.pk, submission.relationship_id]) + '"')
        membership.role = "admin"
        membership.save(update_fields=["role"])
        services.revoke_relationship(colleague, self.case, submission.relationship)
        with self.assertRaises(Http404):
            services.require_partner(self.outsider, submission)

    def test_partner_payload_renders_readably_and_case_navigation_stays_in_portfolio(self):
        submission = self.submission()
        self.login(self.underwriter)
        page = self.client.get(reverse("capacity_detail", args=[self.case.pk, submission.pk]))
        self.assertContains(page, "Control results")
        self.assertContains(page, "Awaiting business approval")
        self.assertContains(page, f'href="{reverse("portfolio", args=[self.case.pk])}" aria-current="page"')
        self.assertNotContains(page, "PRIVATE-CASE-NOTE")
        self.assertNotContains(page, "PRIVATE-DECISION-RATIONALE")
