import os
import time
import uuid
from unittest import skipUnless
from unittest.mock import patch
from celery.contrib.testing.worker import start_worker
from django.test import TransactionTestCase, override_settings
from evidence import tests as evidence_tests
from pilot.celery import app
from . import tests as monitoring_tests
from .models import Observation, SyncRun
from .services import queue_sync
from .tasks import poll_connection


@skipUnless(os.getenv("AI_RUN_QUEUE_TEST") == "1", "Start Redis and set AI_RUN_QUEUE_TEST=1")
@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic", MONITORING_LIVE_ENABLED=False)
class MonitoringQueueTests(TransactionTestCase):
    def setUp(self):
        evidence_tests.EvidenceTests.setUpTestData.__func__(type(self))
        evidence_tests.EvidenceTests.setUp(self)

    asset = monitoring_tests.MonitoringTests.asset
    policy = monitoring_tests.MonitoringTests.policy
    setup_connection = monitoring_tests.MonitoringTests.setup_connection

    def test_worker_processes_authorized_monitoring_run(self):
        queue = "monitor-test-" + uuid.uuid4().hex
        connection, _ = self.setup_connection()
        try:
            with start_worker(app, pool="solo", concurrency=1, queues=[queue], perform_ping_check=False, loglevel="ERROR", shutdown_timeout=10):
                with patch("monitoring.tasks.dispatch", side_effect=lambda run_id: poll_connection.apply_async(args=[str(run_id)], queue=queue, retry=False)):
                    run = queue_sync(self.underwriter, connection)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    run.refresh_from_db()
                    if run.status in ("completed", "failed", "blocked"):
                        break
                    time.sleep(0.1)
                self.assertEqual(run.status, "completed", run.error_code)
                self.assertEqual(Observation.objects.count(), 1)
        finally:
            with app.connection_for_write() as broker:
                broker.default_channel.queue_delete(queue)
