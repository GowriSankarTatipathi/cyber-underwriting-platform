"""Optional real Redis/Celery smoke test using only the Django test database."""
import os
import time
import uuid
from unittest import skipUnless
from unittest.mock import patch

from celery.contrib.testing.worker import start_worker
from django.test import TransactionTestCase, override_settings

from evidence import tests as evidence_tests
from pilot.celery import app
from .models import ReviewJob
from .rules import rule_preview
from .services import queue_review
from .tasks import process_evidence
from .test_extraction import LOG


@skipUnless(os.getenv("AI_RUN_QUEUE_TEST") == "1", "Start the private Redis queue and set AI_RUN_QUEUE_TEST=1")
@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic", AI_REVIEW_PROVIDER="demo")
class QueueIntegrationTests(TransactionTestCase):
    def setUp(self):
        evidence_tests.EvidenceTests.setUpTestData.__func__(type(self))
        evidence_tests.EvidenceTests.setUp(self)

    upload = evidence_tests.EvidenceTests.upload

    def test_real_worker_consumes_private_queue_and_persists_cited_result(self):
        queue_name = "phase5-test-" + uuid.uuid4().hex
        # Isolate from all development work and delete only this test's queue.
        def dispatch(job_id):
            process_evidence.apply_async(args=[str(job_id)], queue=queue_name, retry=False)
        try:
            with start_worker(app, pool="solo", concurrency=1, queues=[queue_name],
                perform_ping_check=False, loglevel="ERROR", shutdown_timeout=10):
                with patch("ai_review.services.dispatch_job", side_effect=dispatch):
                    job, _ = queue_review(self.underwriter, self.upload(body=LOG, accept=False))
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    job = ReviewJob.objects.get(pk=job.pk)
                    if job.status in ("completed", "failed", "blocked"):
                        break
                    time.sleep(0.1)
                self.assertEqual(job.status, "completed", job.error_code)
                self.assertEqual(rule_preview(job.result, False)["status"], "Target missed")
                self.assertEqual(job.runs.count(), 1)
        finally:
            with app.connection_for_write() as connection:
                connection.default_channel.queue_delete(queue_name)
