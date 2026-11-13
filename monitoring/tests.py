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
from evidence.models import BackupJob, ControlAssessment, RestoreTest
from underwriting.models import Finding
from underwriting.snapshots import case_snapshot, checksum
from workspaces.models import Case
from .connectors import ConnectorError, collect, demo
from .models import Alert, AuthorizationRevocation, Connection, Observation, ObservationGroup, Policy, Receipt, Reminder, SyncRun
from .rules import latest_observations, reconciliation_context, reconciliation_state
from .services import acknowledge, authorize, create_asset, create_connection, ingest, publish_policy, queue_sync, reconcile, record_inventory, refresh_reminders, retry_sync, revoke
from .tasks import dispatch, run_sync, schedule_monitoring


@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic", MONITORING_LIVE_ENABLED=False)
class MonitoringTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        evidence_tests.EvidenceTests.setUpTestData.__func__(cls)

    setUp = evidence_tests.EvidenceTests.setUp
    login = evidence_tests.EvidenceTests.login
    upload = evidence_tests.EvidenceTests.upload

    def asset(self, kind="tenant", identifier=None):
        defaults = {"tenant": str(uuid.uuid4()), "aws_resource": "arn:aws:ec2:us-east-1:123456789012:volume/vol-123", "ip": "192.0.2.10", "domain": "example.test"}
        return create_asset(self.underwriter, self.case, {"request": self.backup_request if kind == "aws_resource" else self.control_request,
            "service": self.service, "kind": kind, "identifier": identifier or defaults[kind], "name": kind + " asset"})

    def setup_connection(self, asset=None, scenario="risk", automatic=True):
        asset = asset or self.asset()
        if not self.case.monitoring_policies.exists():
            self.policy(automatic)
        connection = create_connection(self.underwriter, self.case, "Fictional source", "demo", [asset], {"scenario": scenario})
        auth = authorize(self.owner, connection, timezone.now() + timedelta(days=1), True)
        return connection, auth

    def policy(self, automatic=True):
        return publish_policy(self.underwriter, self.case, {"min_mfa_percent": Decimal("100"), "max_backup_age_hours": 24,
            "freshness_hours": 48, "poll_minutes": 60, "blocked_ports": [3389], "due_days": 7, "auto_followup": automatic, "rationale": "Insurer workshop rules"})

    def poll(self, connection):
        run = queue_sync(self.underwriter, connection)
        run_sync(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "completed", run.error_code)
        return run

    def test_risk_observation_creates_internal_work_and_preserves_sources(self):
        connection, auth = self.setup_connection()
        run = self.poll(connection)
        observation = Observation.objects.get()
        self.assertEqual(observation.group.asset.case_id, self.case.pk)
        self.assertEqual(observation.provenance["source"], "Fictional local fixture")
        alert = Alert.objects.get()
        self.assertEqual(alert.finding.owner, self.underwriter)
        self.assertEqual(alert.snapshot["authorization_id"], auth.pk)
        self.assertEqual(alert.snapshot["rule"]["policy_number"], 1)
        self.assertEqual((ControlAssessment.objects.count(), RestoreTest.objects.count(), BackupJob.objects.count()), (0, 0, 0))
        snapshot = case_snapshot(self.case)
        self.assertEqual(snapshot["monitoring"]["assets"][0]["observations"][0]["id"], str(observation.pk))
        self.assertEqual(run.receipts.count(), 1)

    def test_healthy_backup_is_only_observation_and_never_restore_verification(self):
        connection, _ = self.setup_connection(self.asset("aws_resource"), "healthy")
        self.poll(connection)
        self.assertEqual(Observation.objects.get().data["status"], "success")
        self.assertFalse(Finding.objects.exists())
        self.assertFalse(RestoreTest.objects.exists())
        self.assertFalse(BackupJob.objects.exists())

    def test_duplicate_slot_and_worker_delivery_do_not_duplicate_work(self):
        connection, _ = self.setup_connection()
        with self.captureOnCommitCallbacks(execute=True), patch("monitoring.tasks.dispatch") as dispatch_mock:
            first = queue_sync(self.underwriter, connection)
            second = queue_sync(self.underwriter, connection)
        self.assertEqual(first.pk, second.pk)
        dispatch_mock.assert_called_once_with(first.pk)
        run_sync(first.pk)
        with patch("monitoring.tasks.collect") as provider:
            run_sync(first.pk)
            provider.assert_not_called()
        self.assertEqual(Observation.objects.count(), 1)
        self.assertEqual(Finding.objects.count(), 1)

    def test_repeated_source_events_group_receipts_without_duplicate_findings(self):
        connection, authorization = self.setup_connection()
        timestamp = timezone.now() - timedelta(minutes=10)
        payload = demo(connection, list(connection.assets.all()), timestamp)
        with patch("monitoring.tasks.collect", return_value=payload):
            self.poll(connection)
            later = SyncRun.objects.create(connection=connection, authorization=authorization, policy=self.case.monitoring_policies.first(), requested_by=self.underwriter, slot="another-interval")
            run_sync(later.pk)
        self.assertEqual(ObservationGroup.objects.count(), 1)
        self.assertEqual(Observation.objects.count(), 1)
        self.assertEqual(Receipt.objects.count(), 2)
        self.assertEqual(Finding.objects.count(), 1)

    def test_late_observation_is_history_not_new_current_state(self):
        connection, authorization = self.setup_connection(scenario="healthy")
        self.poll(connection)
        old = demo(connection, list(connection.assets.all()), timezone.now() - timedelta(days=5))
        old[0]["data"]["registered"] = 0
        run = SyncRun.objects.create(connection=connection, authorization=authorization, policy=self.case.monitoring_policies.first(), requested_by=self.underwriter, slot="late")
        with patch("monitoring.tasks.collect", return_value=old):
            run_sync(run.pk)
        self.assertEqual(Observation.objects.count(), 2)
        self.assertEqual(latest_observations(connection.assets.get())[0].data["registered"], 10)
        self.assertFalse(Finding.objects.exists())

    def test_automatic_followup_requires_explicit_rule_enablement(self):
        connection, _ = self.setup_connection(automatic=False)
        self.poll(connection)
        self.assertFalse(Finding.objects.exists())

    def test_conflicts_can_be_reconciled_and_new_source_invalidates_review(self):
        asset = self.asset()
        first, _ = self.setup_connection(asset)
        second, _ = self.setup_connection(asset, "healthy")
        self.poll(first)
        self.poll(second)
        self.assertTrue(reconciliation_state(asset, "identity")["conflict"])
        context = reconciliation_context(asset, "identity")
        selected = latest_observations(asset)[0]
        reconcile(self.underwriter, asset, "identity", selected, "Verified selected scope", checksum(context))
        self.assertTrue(reconciliation_state(asset, "identity")["current"])
        third, _ = self.setup_connection(asset)
        self.poll(third)
        self.assertFalse(reconciliation_state(asset, "identity")["current"])
        with self.assertRaises(ValidationError):
            reconcile(self.underwriter, asset, "identity", selected, "Stale tab", checksum(context))

    def test_revoked_authorization_blocks_provider_and_results(self):
        connection, authorization = self.setup_connection()
        run = queue_sync(self.underwriter, connection)
        revoke(self.owner, authorization)
        with patch("monitoring.tasks.collect") as provider:
            run_sync(run.pk)
            provider.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")
        self.assertFalse(Observation.objects.exists())

    def test_revocation_during_collection_discards_every_observation(self):
        connection, authorization = self.setup_connection()
        run = queue_sync(self.underwriter, connection)
        def revoked(connection, assets, guard):
            revoke(self.owner, authorization)
            return demo(connection, assets, timezone.now())
        with patch("monitoring.tasks.collect", side_effect=revoked):
            run_sync(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")
        self.assertFalse(Observation.objects.exists())
        self.assertFalse(Finding.objects.exists())

    def test_changed_rules_block_old_run_and_new_policy_is_pinned(self):
        connection, _ = self.setup_connection()
        old = queue_sync(self.underwriter, connection)
        policy = self.policy(False)
        run_sync(old.pk)
        old.refresh_from_db()
        self.assertEqual(old.status, "blocked")
        new = queue_sync(self.underwriter, connection)
        self.assertNotEqual(old.pk, new.pk)
        self.assertEqual(new.policy_id, policy.pk)

    def test_changed_authorized_scope_blocks_even_if_model_was_changed_outside_forms(self):
        connection, _ = self.setup_connection()
        run = queue_sync(self.underwriter, connection)
        Connection.objects.filter(pk=connection.pk).update(config={"scenario": "healthy"})
        run_sync(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")

    def test_unmapped_asset_or_future_date_rolls_back_entire_poll(self):
        connection, _ = self.setup_connection()
        run = queue_sync(self.underwriter, connection)
        rows = demo(connection, list(connection.assets.all()), timezone.now())
        rows.append({**rows[0], "asset_id": str(uuid.uuid4())})
        with patch("monitoring.tasks.collect", return_value=rows):
            run_sync(run.pk)
        self.assertFalse(Observation.objects.exists())
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

    def test_retries_are_bounded_and_explicit_retry_preserves_attempts(self):
        connection, _ = self.setup_connection()
        run = queue_sync(self.underwriter, connection)
        with patch("monitoring.tasks.collect", side_effect=ConnectorError("provider_unavailable", True)):
            self.assertTrue(run_sync(run.pk))
            self.assertTrue(run_sync(run.pk))
            self.assertFalse(run_sync(run.pk))
        retry_sync(self.underwriter, run)
        run_sync(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.attempts, run.retry_base), ("completed", 4, 3))
        self.assertEqual(run.history.count(), 4)

    def test_queue_failure_is_reported_and_authorized_scheduler_deduplicates(self):
        connection, _ = self.setup_connection()
        run = queue_sync(self.underwriter, connection)
        with patch("monitoring.tasks.poll_connection.apply_async", side_effect=RuntimeError("private")):
            dispatch(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.error_code, "queue_unavailable")
        with patch("monitoring.tasks.close_old_connections"):
            schedule_monitoring()
            schedule_monitoring()
        self.assertEqual(SyncRun.objects.count(), 1)

    def test_owner_authorization_expiry_and_provider_configuration_boundaries(self):
        connection, _ = self.setup_connection()
        with self.assertRaises(Http404):
            authorize(self.underwriter, connection, timezone.now() + timedelta(days=1), True)
        with self.assertRaises(ValidationError):
            authorize(self.owner, connection, timezone.now() + timedelta(days=31), True)
        with self.assertRaises(ValidationError):
            create_connection(self.underwriter, self.case, "Bad", "entra", list(connection.assets.all()), {"client_id": str(uuid.uuid4()), "url": "http://localhost"}, "MONITOR_SECRET_TEST")
        with self.assertRaises(ValidationError):
            self.asset("ip", "127.0.0.1")

    def test_role_scoping_export_mfa_csrf_and_rendered_pages(self):
        connection, authorization = self.setup_connection()
        self.poll(connection)
        asset = connection.assets.get()
        self.login(self.underwriter)
        pages = [("monitoring", [self.case.pk]), ("monitoring_asset", [self.case.pk]), ("monitoring_connection", [self.case.pk]),
            ("monitoring_policy", [self.case.pk]), ("monitoring_export", [self.case.pk]), ("monitoring_inventory", [self.case.pk, asset.pk]),
            ("monitoring_history", [self.case.pk, asset.pk]), ("monitoring_reconcile", [self.case.pk, asset.pk, "identity"])]
        for name, args in pages:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200, name)
        csrf = Client(enforce_csrf_checks=True)
        csrf.cookies = self.client.cookies
        self.assertEqual(csrf.post(reverse("monitoring_poll", args=[self.case.pk, connection.pk])).status_code, 403)
        for user in (self.owner, self.tech, self.outsider):
            self.login(user)
            self.assertEqual(self.client.get(reverse("monitoring_export", args=[self.case.pk])).status_code, 404)
        self.login(self.owner)
        self.assertEqual(self.client.get(reverse("monitoring_authorize", args=[self.case.pk, connection.pk])).status_code, 200)
        self.assertNotContains(self.client.get(reverse("monitoring", args=[self.case.pk])), "Current observations")
        self.login(self.underwriter)
        Case.objects.filter(pk=self.case.pk).update(consent_revoked_at=timezone.now())
        self.assertEqual(self.client.get(reverse("monitoring_export", args=[self.case.pk])).status_code, 404)

    def test_reminders_group_by_subject_and_due_date_and_ack_does_not_close_work(self):
        connection, _ = self.setup_connection()
        self.poll(connection)
        self.case.due_date = timezone.localdate() + timedelta(days=10)
        self.case.save(update_fields=["due_date"])
        refresh_reminders(self.case)
        initial = Reminder.objects.count()
        self.assertGreater(initial, 0)
        refresh_reminders(self.case)
        self.assertEqual(Reminder.objects.count(), initial)
        acknowledge(self.underwriter, Reminder.objects.first())
        self.assertEqual(Finding.objects.get().state, "open")

    def test_renewal_comparison_requires_access_to_both_cases(self):
        connection, _ = self.setup_connection()
        self.poll(connection)
        previous = self.case
        renewal = Case.objects.create(reference="MONITOR-RENEWAL", insurer=self.insurer, applicant=self.business, checklist=self.checklist,
            kind="renewal", service="Ordering", created_by=self.underwriter, assignee=self.underwriter, renewal_of=previous,
            consent_granted_by=self.owner, consent_expires_at=timezone.now() + timedelta(days=10))
        self.login(self.underwriter)
        self.assertEqual(self.client.get(reverse("monitoring_renewal", args=[renewal.pk])).status_code, 200)
        Case.objects.filter(pk=previous.pk).update(consent_revoked_at=timezone.now())
        self.assertEqual(self.client.get(reverse("monitoring_renewal", args=[renewal.pk])).status_code, 404)
