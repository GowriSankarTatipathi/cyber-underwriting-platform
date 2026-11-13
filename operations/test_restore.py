import os
from pathlib import Path
import tempfile
from unittest import skipUnless
from cryptography.fernet import Fernet
from django.test import TransactionTestCase, override_settings
from evidence import tests as evidence_tests
from .recovery import create_bundle, verify_bundle


@skipUnless(os.getenv("RECOVERY_RUN_TEST") == "1", "Set RECOVERY_RUN_TEST=1 with matching pg_dump/pg_restore and CREATEDB permission")
@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND="local", EVIDENCE_SCAN_MODE="basic")
class RestoreIntegrationTests(TransactionTestCase):
    def setUp(self):
        evidence_tests.EvidenceTests.setUpTestData.__func__(type(self))
        evidence_tests.EvidenceTests.setUp(self)

    upload = evidence_tests.EvidenceTests.upload

    def test_database_and_two_exact_evidence_versions_restore(self):
        first = self.upload(body=b"Fictional seven-hour restore evidence")
        second = self.upload(document=first.document, body=b"Fictional three-hour retest evidence")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key, bundle = root / "key", root / "bundle"
            key.write_bytes(Fernet.generate_key())
            key.chmod(0o600)
            summary = create_bundle(bundle, key)
            self.assertEqual(summary["files"], 2)
            drill = verify_bundle(bundle, key)
            self.assertTrue(drill.verified)
            self.assertEqual(drill.evidence_files, 2)
            self.assertEqual(drill.bundle_sha256, summary["sha256"])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertNotEqual(first.sha256, second.sha256)
