from threading import Event, Thread
from unittest.mock import patch
from django.db import close_old_connections, connection, transaction
from django.http import Http404
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from workspaces import tests as workspace_tests
from workspaces.models import Case, SharingGrant, User
from workspaces.services import accept_invitation, issue_invitation, lock_case


class MutationAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        workspace_tests.WorkspaceTests.setUpTestData.__func__(cls)

    login = workspace_tests.WorkspaceTests.login

    def test_revocation_while_waiting_for_case_lock_blocks_consent(self):
        self.login(self.owner)
        before = self.case.consent_expires_at
        def revoked(case):
            SharingGrant.objects.filter(pk=self.owner_grant.pk).update(revoked_at=timezone.now())
            return lock_case(case)
        with patch("workspaces.views.lock_case", side_effect=revoked):
            response = self.client.post(reverse("consent", args=[self.case.pk]), {"days": 365, "confirm": "on"})
        self.assertEqual(response.status_code, 404)
        self.case.refresh_from_db()
        self.assertEqual(self.case.consent_expires_at, before)

    def test_provider_revoked_between_form_validation_and_assignment_is_rejected(self):
        self.login(self.underwriter)
        def revoked(case):
            SharingGrant.objects.filter(pk=self.tech_grant.pk).update(revoked_at=timezone.now())
            return lock_case(case)
        with patch("workspaces.views.lock_case", side_effect=revoked):
            response = self.client.post(reverse("assign_request", args=[self.case.pk, self.unassigned.pk]), {"assigned_to": self.tech.pk})
        self.assertEqual(response.status_code, 302)
        self.unassigned.refresh_from_db()
        self.assertIsNone(self.unassigned.assigned_to_id)

    def test_mfa_redirects_are_not_cacheable(self):
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        with override_settings(LOCAL_PASSWORD_ONLY=False):
            response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("no-store", response["Cache-Control"])

    def test_friendly_csrf_failure_keeps_null_origin_rejected(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("account_login"), {"login": self.owner.email, "password": workspace_tests.PASSWORD}, HTTP_ORIGIN="null")
        self.assertContains(response, "Refresh this page and try again", status_code=403)
        self.assertNotContains(response, "Traceback", status_code=403)
        self.assertNotIn("_auth_user_id", client.session)

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False)
    def test_production_not_found_and_demo_catalogue_are_private(self):
        self.login(self.underwriter)
        page = self.client.get("/not-a-real-case/")
        self.assertContains(page, "This page isn't available", status_code=404)
        self.assertEqual(self.client.get(reverse("walkthrough")).status_code, 404)
        response = self.client.get(reverse("case", args=[self.case.pk]))
        self.assertNotContains(response, "Demo walkthrough")


class InvitationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        workspace_tests.WorkspaceTests.setUpTestData.__func__(type(self))

    def test_replacement_does_not_deadlock_with_acceptance(self):
        old, token = issue_invitation(self.case, self.underwriter, self.owner.email, "owner")
        waiting, done, result = Event(), Event(), []
        def accept():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '3s'")
                def signal_wait(execute, sql, params, many, context):
                    if "FOR UPDATE" in sql and '"workspaces_case"' in sql:
                        waiting.set()
                    return execute(sql, params, many, context)
                with connection.execute_wrapper(signal_wait):
                    accept_invitation(token, User.objects.get(pk=self.owner.pk))
                result.append("accepted")
            except Http404:
                result.append("revoked")
            except Exception as error:
                result.append(type(error).__name__)
            finally:
                connection.close()
                done.set()
        thread = Thread(target=accept, daemon=True)
        try:
            with transaction.atomic():
                Case.objects.select_for_update().get(pk=self.case.pk)
                thread.start()
                self.assertTrue(waiting.wait(3), "Acceptance must reach the locked case")
                # If acceptance already owns the invitation lock this update
                # deadlocks. Both paths must instead serialize on the case.
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '3s'")
                issue_invitation(self.case, self.underwriter, self.owner.email, "owner")
            self.assertTrue(done.wait(5))
            self.assertEqual(result, ["revoked"])
            old.refresh_from_db()
            self.assertIsNone(old.accepted_at)
            self.assertIsNotNone(old.revoked_at)
        finally:
            thread.join(timeout=5)
