import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import TOTP, format_hotp_value, generate_totp_secret, hotp_value
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .auth import MFAAdapter
from .models import Case, CaseFile, CaseJob, ChecklistVersion, EvidenceRequest, Invitation, Membership, Organization, SharingGrant, User
from .permissions import authorize_job_execution
from .services import accept_invitation, find_invitation, issue_invitation

PASSWORD = "Fictional-Test-Password!724"


def make_user(name):
    user = User.objects.create_user(f"{name}@example.test", PASSWORD)
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    return user


class WorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.underwriter = make_user("underwriter")
        cls.reviewer = make_user("reviewer")
        cls.owner = make_user("owner")
        cls.tech = make_user("tech")
        cls.stranger = make_user("stranger")
        cls.insurer = Organization.objects.create(name="Insurer One", kind="insurer")
        cls.other_insurer = Organization.objects.create(name="Insurer Two", kind="insurer")
        for user, org, role in [(cls.underwriter, cls.insurer, "underwriter"), (cls.reviewer, cls.insurer, "reviewer"), (cls.stranger, cls.other_insurer, "underwriter")]:
            Membership.objects.create(user=user, organization=org, role=role)
        cls.business = Organization.objects.create(name="Maple Business", kind="business")
        requirements = [{"name": "Restore test", "evidence": "Dated restore log", "freshness_days": 90}, {"name": "Access review", "evidence": "Signed access review", "freshness_days": 180}]
        cls.checklist = ChecklistVersion.objects.create(insurer=cls.insurer, version="v1", requirements=requirements, recovery_targets={"duration_minutes": 240, "data_age_minutes": 60})
        cls.other_checklist = ChecklistVersion.objects.create(insurer=cls.other_insurer, version="v1", requirements=requirements)
        cls.case = Case.objects.create(reference="TEST-001", insurer=cls.insurer, applicant=cls.business, checklist=cls.checklist, service="Ordering", assignee=cls.underwriter, created_by=cls.underwriter, internal_notes="PRIVATE-INSURER-NOTE", consent_granted_by=cls.owner, consent_expires_at=timezone.now() + timedelta(days=30))
        cls.other_case = Case.objects.create(reference="OTHER-001", insurer=cls.other_insurer, applicant=cls.business, checklist=cls.other_checklist, service="Other service", assignee=cls.stranger, created_by=cls.stranger)
        cls.owner_grant = SharingGrant.objects.create(case=cls.case, user=cls.owner, role="owner", granted_by=cls.underwriter, expires_at=timezone.now() + timedelta(days=90))
        cls.tech_grant = SharingGrant.objects.create(case=cls.case, user=cls.tech, role="technician", granted_by=cls.owner, expires_at=timezone.now() + timedelta(days=30))
        cls.item = EvidenceRequest.objects.create(case=cls.case, title="Restore test", description="Assigned restore description", freshness_days=90, assigned_to=cls.tech)
        cls.unassigned = EvidenceRequest.objects.create(case=cls.case, title="Access review", description="Unassigned private request", freshness_days=180)
        cls.file = CaseFile.objects.create(request=cls.item, name="brief.txt", storage_key=f"{cls.case.pk}/brief.txt")
        cls.job = CaseJob.objects.create(case=cls.case, requested_by=cls.tech, grant=cls.tech_grant)
        for user in [cls.underwriter, cls.reviewer, cls.owner, cls.tech, cls.stranger]:
            TOTP.activate(user, generate_totp_secret())

    def setUp(self):
        cache.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = override_settings(PRIVATE_FILE_ROOT=self.directory.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        target = Path(self.directory.name) / self.file.storage_key
        target.parent.mkdir(parents=True)
        target.write_text("Fictional private file")

    def login(self, user, client=None):
        client = client or self.client
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        session = client.session
        session["account_authentication_methods"] = [{"method": "password", "at": time.time()}, {"method": "mfa", "at": time.time()}]
        session["account_authentication_methods_user"] = str(user.pk)
        session.save()
        return client

    def case_url(self):
        return reverse("case", args=[self.case.pk])

    def protected_urls(self):
        return [self.case_url(), reverse("download_file", args=[self.case.pk, self.file.pk]), reverse("export_case", args=[self.case.pk]), reverse("job_detail", args=[self.case.pk, self.job.pk])]

    def test_anonymous_requires_signin_on_all_surfaces(self):
        for url in self.protected_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 302)

    def test_cross_insurer_denied_every_surface_and_search(self):
        self.login(self.stranger)
        for url in self.protected_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        response = self.client.get(reverse("underwriter"))
        self.assertNotContains(response, "TEST-001")
        self.assertContains(response, "OTHER-001")

    def test_superuser_has_no_implicit_case_access(self):
        self.stranger.is_superuser = True
        self.stranger.save()
        self.login(self.stranger)
        self.assertEqual(self.client.get(self.case_url()).status_code, 404)

    def test_unassigned_same_insurer_reviewer_denied(self):
        self.login(self.reviewer)
        self.assertEqual(self.client.get(self.case_url()).status_code, 404)
        self.assertNotContains(self.client.get(reverse("underwriter")), "TEST-001")

    def test_create_case_clones_pinned_requirements_and_invites_owner(self):
        self.login(self.underwriter)
        response = self.client.post(reverse("case_create"), {"checklist": self.checklist.pk, "business_name": "New Applicant", "kind": "application", "service": "Checkout", "owner_email": "new-owner@example.test"})
        self.assertContains(response, "Invitation ready")
        case = Case.objects.get(applicant__name="New Applicant")
        self.assertEqual(case.requests.count(), 2)
        self.assertFalse(case.consent_active)
        self.assertEqual(case.invitations.get().role, "owner")
        self.assertEqual(case.checklist_id, self.checklist.pk)

    def test_foreign_checklist_rejected(self):
        self.login(self.underwriter)
        response = self.client.post(reverse("case_create"), {"checklist": self.other_checklist.pk, "business_name": "Invalid Applicant", "kind": "renewal", "service": "Ordering", "owner_email": "owner@example.test"})
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(Case.objects.filter(applicant__name="Invalid Applicant").exists())

    def test_contributor_cannot_create_or_assign_cases(self):
        self.login(self.owner)
        self.assertEqual(self.client.get(reverse("case_create")).status_code, 404)
        self.assertEqual(self.client.post(reverse("assign_case", args=[self.case.pk]), {"assignee": self.owner.pk}).status_code, 404)

    def test_reassign_case_to_same_insurer_reviewer(self):
        self.login(self.underwriter)
        response = self.client.post(reverse("assign_case", args=[self.case.pk]), {"assignee": self.reviewer.pk})
        self.assertEqual(response.status_code, 302)
        self.login(self.reviewer)
        self.assertContains(self.client.get(self.case_url()), "PRIVATE-INSURER-NOTE")
        self.assertEqual(self.client.post(reverse("save_notes", args=[self.case.pk]), {"internal_notes": "bad"}).status_code, 404)

    def test_cross_insurer_assignment_rejected(self):
        self.login(self.underwriter)
        self.client.post(reverse("assign_case", args=[self.case.pk]), {"assignee": self.stranger.pk})
        self.case.refresh_from_db()
        self.assertEqual(self.case.assignee, self.underwriter)

    def test_technician_sees_only_assigned_requests_and_no_internal_notes(self):
        self.login(self.tech)
        response = self.client.get(self.case_url())
        self.assertContains(response, "Assigned restore description")
        self.assertNotContains(response, "Unassigned private request")
        self.assertNotContains(response, "PRIVATE-INSURER-NOTE")
        payload = self.client.get(reverse("export_case", args=[self.case.pk])).json()
        self.assertNotIn("internal_notes", payload)
        self.assertEqual(len(payload["requests"]), 1)

    def test_owner_does_not_see_internal_notes(self):
        self.login(self.owner)
        self.assertNotContains(self.client.get(self.case_url()), "PRIVATE-INSURER-NOTE")
        self.assertNotIn("internal_notes", self.client.get(reverse("export_case", args=[self.case.pk])).json())

    def test_revoked_provider_denied_pages_files_exports_jobs_and_worker(self):
        self.login(self.tech)
        self.tech_grant.revoked_at = timezone.now()
        self.tech_grant.save()
        for url in self.protected_urls():
            self.assertEqual(self.client.get(url).status_code, 404)
        with self.assertRaises(Http404):
            authorize_job_execution(self.job)

    def test_expired_provider_denied(self):
        self.login(self.tech)
        self.tech_grant.expires_at = timezone.now() - timedelta(seconds=1)
        self.tech_grant.save()
        for url in self.protected_urls():
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_business_revokes_insurer_and_provider_evidence_access(self):
        self.login(self.owner)
        self.client.post(reverse("consent", args=[self.case.pk]), {"action": "revoke"})
        self.login(self.underwriter)
        response = self.client.get(self.case_url())
        self.assertContains(response, "Business authorization is needed")
        self.assertNotContains(response, "Assigned restore description")
        self.assertEqual(self.client.get(reverse("export_case", args=[self.case.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("download_file", args=[self.case.pk, self.file.pk])).status_code, 404)
        self.login(self.tech)
        self.assertEqual(self.client.get(self.case_url()).status_code, 404)
        with self.assertRaises(Http404):
            authorize_job_execution(self.job)

    def test_consent_requires_owner_confirmation(self):
        self.login(self.tech)
        self.assertEqual(self.client.post(reverse("consent", args=[self.case.pk]), {"days": 30, "confirm": "on"}).status_code, 404)
        self.login(self.owner)
        self.client.post(reverse("consent", args=[self.case.pk]), {"action": "revoke"})
        self.client.post(reverse("consent", args=[self.case.pk]), {"days": 30})
        self.case.refresh_from_db()
        self.assertFalse(self.case.consent_active)
        self.client.post(reverse("consent", args=[self.case.pk]), {"days": 30, "confirm": "on"})
        self.case.refresh_from_db()
        self.assertTrue(self.case.consent_active)

    def test_expired_consent_denies_worker(self):
        self.case.consent_expires_at = timezone.now() - timedelta(seconds=1)
        self.case.save()
        with self.assertRaises(Http404):
            authorize_job_execution(self.job)

    def test_disabled_membership_denies_case_and_export(self):
        self.login(self.underwriter)
        Membership.objects.filter(user=self.underwriter).update(is_active=False)
        self.assertEqual(self.client.get(self.case_url()).status_code, 404)
        self.assertEqual(self.client.get(reverse("export_case", args=[self.case.pk])).status_code, 404)

    def test_private_download_authorized_and_no_public_media_route(self):
        self.login(self.tech)
        response = self.client.get(reverse("download_file", args=[self.case.pk, self.file.pk]))
        self.assertEqual(b"".join(response.streaming_content), b"Fictional private file")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.get("/media/" + self.file.storage_key).status_code, 404)

    def test_file_id_cannot_be_moved_to_another_case_url(self):
        self.login(self.stranger)
        self.assertEqual(self.client.get(reverse("download_file", args=[self.other_case.pk, self.file.pk])).status_code, 404)

    def test_private_path_escape_denied(self):
        self.file.storage_key = "../outside.txt"
        self.file.save()
        self.login(self.tech)
        self.assertEqual(self.client.get(reverse("download_file", args=[self.case.pk, self.file.pk])).status_code, 404)

    def test_request_assignment_rejects_uninvited_user(self):
        self.login(self.owner)
        url = reverse("assign_request", args=[self.case.pk, self.unassigned.pk])
        self.assertContains(self.client.post(url, {"assigned_to": self.stranger.pk}), "Select a valid choice")
        self.assertEqual(self.client.post(url, {"assigned_to": self.tech.pk}).status_code, 302)
        self.unassigned.refresh_from_db()
        self.assertEqual(self.unassigned.assigned_to, self.tech)

    def test_invite_accept_email_binding_replay_and_digest(self):
        invitation, token = issue_invitation(self.case, self.owner, self.tech.email, "technician")
        self.assertNotEqual(invitation.token_digest, token)
        with self.assertRaises(Http404):
            accept_invitation(token, self.stranger)
        grant = accept_invitation(token, self.tech)
        self.assertEqual(grant.case, self.case)
        with self.assertRaises(Http404):
            accept_invitation(token, self.tech)

    def test_expired_revoked_and_replaced_invites_fail(self):
        invite, token = issue_invitation(self.case, self.owner, "someone@example.test", "technician")
        invite.expires_at = timezone.now() - timedelta(seconds=1)
        invite.save()
        with self.assertRaises(Http404):
            find_invitation(token)
        invite, token = issue_invitation(self.case, self.owner, "someone@example.test", "technician")
        issue_invitation(self.case, self.owner, "someone@example.test", "technician")
        with self.assertRaises(Http404):
            find_invitation(token)

    def test_invite_authority_rechecked_at_acceptance(self):
        invitation, token = issue_invitation(self.case, self.owner, self.tech.email, "technician")
        self.owner_grant.revoked_at = timezone.now()
        self.owner_grant.save()
        with self.assertRaises(Http404):
            accept_invitation(token, self.tech)

    def test_invitation_get_does_not_accept(self):
        invitation, token = issue_invitation(self.case, self.owner, self.tech.email, "technician")
        self.login(self.tech)
        self.assertEqual(self.client.get(reverse("invitation", args=[token])).status_code, 200)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.accepted_at)

    def test_invited_new_account_creation_binds_email(self):
        invitation, token = issue_invitation(self.case, self.owner, "new-tech@example.test", "technician")
        response = self.client.post(reverse("invitation", args=[token]), {"first_name": "New", "last_name": "Tech", "email": "attacker@example.test", "password1": PASSWORD, "password2": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(email="new-tech@example.test").exists())
        self.assertFalse(User.objects.filter(email="attacker@example.test").exists())
        self.assertFalse(SharingGrant.objects.filter(user__email="new-tech@example.test").exists())

    def test_csrf_and_post_only_mutations(self):
        client = self.login(self.owner, Client(enforce_csrf_checks=True))
        url = reverse("consent", args=[self.case.pk])
        self.assertEqual(client.get(url).status_code, 405)
        self.assertEqual(client.post(url, {"action": "revoke"}).status_code, 403)

    def test_checklist_cannot_be_edited_in_place(self):
        self.checklist.version = "changed"
        with self.assertRaises(ValidationError):
            self.checklist.save()

    def test_phase1_demo_preserved(self):
        self.assertContains(self.client.get("/demo/"), "Target missed")
        self.assertContains(self.client.get("/demo/?test=retest"), "Targets met")
        self.assertContains(self.client.get("/demo/?test=retest"), "Missing")

    def test_password_only_session_cannot_access_any_surface(self):
        self.client.force_login(self.tech)
        for url in self.protected_urls():
            self.assertRedirects(self.client.get(url), reverse("mfa_reauthenticate") + "?next=" + url.replace("/", "%2F"), fetch_redirect_response=False)

    def test_mfa_record_from_other_user_not_accepted(self):
        self.login(self.tech)
        session = self.client.session
        session["account_authentication_methods_user"] = str(self.owner.pk)
        session.save()
        self.assertEqual(self.client.get(self.case_url()).status_code, 302)

    def test_complete_case_invitation_consent_and_provider_journey(self):
        underwriter = self.login(self.underwriter, Client())
        owner = self.login(self.owner, Client())
        technician = self.login(self.tech, Client())
        response = underwriter.post(reverse("case_create"), {
            "checklist": self.checklist.pk, "business_name": "Journey Applicant",
            "kind": "renewal", "service": "Ordering", "owner_email": self.owner.email,
        })
        case = response.context["case"]
        invite_path = response.context["invitation_url"].replace("http://testserver", "")
        self.assertEqual(owner.post(invite_path).status_code, 302)
        self.assertContains(owner.get(reverse("case", args=[case.pk])), "Business authorization is needed")
        self.assertEqual(owner.post(reverse("consent", args=[case.pk]), {"days": 30, "confirm": "on"}).status_code, 302)
        response = owner.post(reverse("invite", args=[case.pk]), {"email": self.tech.email, "organization_name": "Fictional IT", "access_days": 30})
        self.assertContains(response, "Invitation ready")
        tech_invite = response.context["invitation_url"].replace("http://testserver", "")
        self.assertEqual(technician.post(tech_invite).status_code, 302)
        self.assertContains(technician.get(reverse("case", args=[case.pk])), "No requests assigned yet")
        item = case.requests.get(title="Restore test")
        owner.post(reverse("assign_request", args=[case.pk, item.pk]), {"assigned_to": self.tech.pk})
        response = technician.get(reverse("case", args=[case.pk]))
        self.assertContains(response, "Dated restore log")
        self.assertNotContains(response, "Signed access review")
        payload = technician.get(reverse("export_case", args=[case.pk])).json()
        self.assertEqual([item["title"] for item in payload["requests"]], ["Restore test"])
        grant = SharingGrant.objects.get(case=case, user=self.tech)
        owner.post(reverse("revoke_grant", args=[case.pk, grant.pk]))
        self.assertEqual(technician.get(reverse("case", args=[case.pk])).status_code, 404)
        self.assertEqual(technician.get(reverse("export_case", args=[case.pk])).status_code, 404)


class LoginCsrfTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user("csrf-login")
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1:8000")
        self.url = reverse("account_login")

    def login_form(self, secure=False):
        response = self.client.get(self.url, secure=secure)
        self.assertEqual(response.status_code, 200)
        return response, {
            "login": self.user.email,
            "password": PASSWORD,
            "csrfmiddlewaretoken": str(response.context["csrf_token"]),
        }

    def test_login_preserves_same_origin_and_accepts_valid_csrf(self):
        page, data = self.login_form()
        # no-referrer makes browsers send Origin: null on normal form posts.
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", page["Cache-Control"])
        response = self.client.post(self.url, data, HTTP_ORIGIN="http://127.0.0.1:8000")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertRedirects(self.client.get("/"), reverse("mfa_activate_totp"), fetch_redirect_response=False)

    def test_https_login_accepts_same_origin_referer_without_origin_header(self):
        page, data = self.login_form(secure=True)
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        response = self.client.post(self.url, data, secure=True, HTTP_REFERER=f"https://127.0.0.1:8000{self.url}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))

    def test_null_and_foreign_origins_remain_rejected_with_valid_token(self):
        _, data = self.login_form()
        for origin in ("null", "https://untrusted.example"):
            with self.subTest(origin=origin):
                response = self.client.post(self.url, data, HTTP_ORIGIN=origin)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_same_origin_still_requires_valid_csrf_token(self):
        _, data = self.login_form()
        for token in (None, "invalid-token"):
            with self.subTest(token=token):
                submission = {key: value for key, value in data.items() if key != "csrfmiddlewaretoken"}
                if token is not None:
                    submission["csrfmiddlewaretoken"] = token
                response = self.client.post(self.url, submission, HTTP_ORIGIN="http://127.0.0.1:8000")
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("_auth_user_id", self.client.session)


class AuthenticationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user("auth")

    def code(self, secret, offset=0):
        return format_hotp_value(hotp_value(secret, int(time.time()) // 30 + offset))

    def test_password_login_requires_mfa_enrollment(self):
        response = self.client.post(reverse("account_login"), {"login": self.user.email, "password": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(self.client.get("/"), reverse("mfa_activate_totp"), fetch_redirect_response=False)
        activation = self.client.get(reverse("mfa_activate_totp"))
        self.assertEqual(activation.status_code, 302)
        self.assertIn(reverse("account_reauthenticate"), activation.url)
        activation = self.client.post(reverse("account_reauthenticate"), {"password": PASSWORD, "next": reverse("mfa_activate_totp")}, follow=True)
        self.assertEqual(activation.status_code, 200, activation.headers)
        secret = self.client.session["mfa.totp.secret"]
        response = self.client.post(reverse("mfa_activate_totp"), {"code": self.code(secret)})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Authenticator.objects.filter(user=self.user, type="totp").exists())
        self.assertEqual(self.client.get("/").status_code, 302)
        response = self.client.post(reverse("mfa_reauthenticate"), {"code": self.code(secret)}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(reverse("portal")).status_code, 200)

    def test_existing_mfa_login_requires_valid_code(self):
        secret = generate_totp_secret()
        TOTP.activate(self.user, secret)
        response = self.client.post(reverse("account_login"), {"login": self.user.email, "password": PASSWORD})
        self.assertRedirects(response, reverse("mfa_authenticate"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(reverse("mfa_authenticate"), {"code": "invalid"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(reverse("mfa_authenticate"), {"code": self.code(secret)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(str(self.user.pk), self.client.session["_auth_user_id"])
        self.assertEqual(self.client.get(reverse("portal")).status_code, 200)

    def test_totp_secret_encrypted_and_replay_blocked(self):
        secret = generate_totp_secret()
        auth = TOTP.activate(self.user, secret)
        self.assertNotIn(secret, auth.instance.data["secret"])
        self.assertEqual(MFAAdapter().decrypt(auth.instance.data["secret"]), secret)
        code = self.code(secret)
        self.assertTrue(auth.validate_code(code))
        self.assertFalse(auth.validate_code(code))

    def test_mfa_cannot_be_disabled(self):
        auth = TOTP.activate(self.user, generate_totp_secret())
        self.assertFalse(MFAAdapter().can_delete_authenticator(auth.instance))

    def test_public_signup_closed(self):
        self.assertContains(self.client.get(reverse("account_signup")), "Sign Up Closed")

    @override_settings(DEBUG=True, LOCAL_PASSWORD_ONLY=True)
    def test_local_password_only_skips_enrollment(self):
        response = self.client.post(reverse("account_login"), {"login": self.user.email, "password": PASSWORD}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertEqual(self.client.get(reverse("portal")).status_code, 200)
        self.assertFalse(Authenticator.objects.filter(user=self.user).exists())

    @override_settings(DEBUG=True, LOCAL_PASSWORD_ONLY=True)
    def test_local_password_only_skips_existing_authenticator_without_deleting_it(self):
        TOTP.activate(self.user, generate_totp_secret())
        response = self.client.post(reverse("account_login"), {"login": self.user.email, "password": PASSWORD}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(reverse("portal")).status_code, 200)
        self.assertTrue(Authenticator.objects.filter(user=self.user, type="totp").exists())

    @override_settings(DEBUG=True, LOCAL_PASSWORD_ONLY=True)
    def test_local_password_only_still_rejects_wrong_password(self):
        self.client.post(reverse("account_login"), {"login": self.user.email, "password": "wrong-password"})
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(DEBUG=False, LOCAL_PASSWORD_ONLY=True, SECURE_SSL_REDIRECT=False)
    def test_local_switch_cannot_disable_staging_authenticator(self):
        TOTP.activate(self.user, generate_totp_secret())
        response = self.client.post(reverse("account_login"), {"login": self.user.email, "password": PASSWORD})
        self.assertRedirects(response, reverse("mfa_authenticate"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(DEBUG=False, LOCAL_PASSWORD_ONLY=True, SECURE_SSL_REDIRECT=False)
    def test_local_switch_cannot_disable_staging_enrollment(self):
        self.client.post(reverse("account_login"), {"login": self.user.email, "password": PASSWORD})
        self.assertRedirects(self.client.get("/"), reverse("mfa_activate_totp"), fetch_redirect_response=False)
