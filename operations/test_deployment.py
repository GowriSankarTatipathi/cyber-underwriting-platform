from io import StringIO
from pathlib import Path
import tempfile
from unittest.mock import patch
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, override_settings
from deploy.ci_config import prepare
from deploy.healthcheck import check
from deploy.preflight import inspect_files


class DeploymentTests(SimpleTestCase):
    def test_offline_preflight_and_ci_generator_never_overwrite_operator_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare(root)
            env = root / ".env.staging"
            original = env.read_bytes()
            self.assertEqual(inspect_files(root, env), [])
            with self.assertRaises(FileExistsError):
                prepare(root)
            self.assertEqual(env.read_bytes(), original)

    def test_preflight_reports_missing_files_and_unsafe_credentials_without_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(inspect_files(root, root / ".env.staging"))
            prepare(root)
            env = root / ".env.staging"
            with env.open("a") as stream:
                stream.write("POSTGRES_PASSWORD=DO-NOT-PRINT-THIS-SECRET\nLOCAL_PASSWORD_ONLY=true\nDJANGO_CSRF_TRUSTED_ORIGINS=http://foreign.example\n")
            env.chmod(0o644)
            problems = inspect_files(root, env)
            self.assertTrue(any("administrator credentials" in p for p in problems))
            self.assertTrue(any("HTTPS CSRF" in p for p in problems))
            self.assertTrue(any("chmod 600" in p for p in problems))
            self.assertTrue(any("LOCAL_PASSWORD_ONLY" in p for p in problems))
            self.assertNotIn("DO-NOT-PRINT-THIS-SECRET", str(problems))

    def test_preflight_rejects_mismatched_tls_key(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, second = Path(a), Path(b)
            prepare(first)
            prepare(second)
            (first / ".local/staging/tls/server.key").write_bytes((second / ".local/staging/tls/server.key").read_bytes())
            self.assertTrue(any("do not match" in problem for problem in inspect_files(first, first / ".env.staging")))

    def test_readiness_uses_allowed_host_but_only_contacts_loopback(self):
        with patch.dict("os.environ", {"DJANGO_ALLOWED_HOSTS": "staging.example.test"}), patch("deploy.healthcheck.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200
            check()
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8000/health/ready/")
            self.assertEqual(request.get_header("Host"), "staging.example.test")

    @override_settings(DEBUG=False, SECRET_KEY="a-secure-test-only-key-" * 4, ALLOWED_HOSTS=["staging.example.test"], EVIDENCE_STORAGE_BACKEND="s3",
        EVIDENCE_S3_BUCKET="private-test-bucket", EVIDENCE_SCAN_MODE="external", EVIDENCE_SCAN_COMMAND='["clamscan"]', AI_REVIEW_PROVIDER="off", LOCAL_PASSWORD_ONLY=False)
    def test_application_configuration_refuses_database_admin_secret(self):
        with patch.dict("os.environ", {"POSTGRES_PASSWORD": "never-print-secret"}), patch("shutil.which", return_value="/usr/bin/clamscan"):
            with self.assertRaises(CommandError) as error:
                call_command("check_staging", stdout=StringIO())
            self.assertIn("administrator credentials", str(error.exception))
            self.assertNotIn("never-print-secret", str(error.exception))
