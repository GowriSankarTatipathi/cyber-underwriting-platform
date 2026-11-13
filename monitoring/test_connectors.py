from datetime import timedelta
import os
from types import SimpleNamespace
import uuid
from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from .connectors import ConnectorError, aws_backup, collect, entra, hibp, internetdb, kev, read_json


class ConnectorTests(SimpleTestCase):
    def asset(self, kind="tenant", value=None):
        return SimpleNamespace(pk=uuid.uuid4(), kind=kind, identifier=value or str(uuid.uuid4()))

    def connection(self, provider, config=None):
        return SimpleNamespace(provider=provider, config=config or {}, credential_ref="MONITOR_SECRET_TEST")

    def test_fixed_host_rejects_cross_host_pagination_and_insecure_urls(self):
        for url in ("http://graph.microsoft.com/path", "https://evil.example/path", "https://graph.microsoft.com@evil.example/path", "https://graph.microsoft.com:8443/path"):
            with self.assertRaises(ConnectorError):
                read_json(url, host="graph.microsoft.com")

    @override_settings(MONITORING_LIVE_ENABLED=False)
    def test_live_connectors_are_disabled_by_default(self):
        with patch("monitoring.connectors.read_json") as network:
            with self.assertRaises(ConnectorError):
                collect(self.connection("entra"), [self.asset()], lambda: None)
            network.assert_not_called()

    @patch.dict(os.environ, {"MONITOR_SECRET_TEST": "test-only"})
    def test_entra_paginates_counts_and_does_not_store_account_names(self):
        first = {"value": [{"id": "1", "userPrincipalName": "private@example.test", "isMfaRegistered": False, "isMfaCapable": False}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/reports/authenticationMethods/userRegistrationDetails?$skiptoken=next"}
        second = {"value": [{"id": "2", "userPrincipalName": "another@example.test", "isMfaRegistered": True, "isMfaCapable": True}]}
        with patch("monitoring.connectors.read_json", side_effect=[{"access_token": "not-a-real-token"}, first, second]) as network:
            rows = entra(self.connection("entra", {"client_id": str(uuid.uuid4())}), [self.asset()], timezone.now(), lambda: None)
        self.assertEqual(rows[0]["data"], {"total": 2, "registered": 1, "capable": 1})
        self.assertFalse(rows[0]["source_time_known"])
        self.assertNotIn("private@", str(rows))
        self.assertEqual(network.call_count, 3)

    @patch.dict(os.environ, {"MONITOR_SECRET_TEST": "test-only"})
    def test_entra_refuses_partial_report_and_foreign_pagination(self):
        with patch("monitoring.connectors.read_json", side_effect=[{"access_token": "test"}, {"value": [], "@odata.nextLink": "https://evil.example/steal"}]):
            with self.assertRaises(ConnectorError) as error:
                entra(self.connection("entra", {"client_id": str(uuid.uuid4())}), [self.asset()], timezone.now(), lambda: None)
        self.assertEqual(error.exception.code, "invalid_provider_url")

    def test_aws_account_binding_and_completed_with_issues(self):
        asset = self.asset("aws_resource", "arn:aws:ec2:us-east-1:123456789012:volume/vol-123")
        connection = self.connection("aws_backup", {"region": "us-east-1", "account_id": "123456789012"})
        client = MagicMock()
        client.get_caller_identity.return_value = {"Account": "123456789012"}
        client.list_backup_jobs.return_value = {"BackupJobs": [{"BackupJobId": "job", "ResourceArn": asset.identifier,
            "State": "COMPLETED", "MessageCategory": "PARTIAL_FAILURE", "CreationDate": timezone.now() - timedelta(hours=2), "CompletionDate": timezone.now() - timedelta(hours=1)}]}
        with patch("monitoring.connectors.boto3.client", return_value=client):
            result = aws_backup(connection, [asset], timezone.now(), lambda: None)
            self.assertEqual(result[0]["data"]["status"], "partial")
            self.assertEqual(client.list_backup_jobs.call_args.kwargs["ByResourceArn"], asset.identifier)
            client.get_caller_identity.return_value = {"Account": "999999999999"}
            with self.assertRaises(ConnectorError) as error:
                aws_backup(connection, [asset], timezone.now(), lambda: None)
            self.assertEqual(error.exception.code, "aws_account_mismatch")

    def test_internetdb_no_data_is_not_a_clean_observation(self):
        with patch("monitoring.connectors.read_json", return_value={"absent": True}):
            with self.assertRaises(ConnectorError) as error:
                internetdb(self.connection("internetdb"), [self.asset("ip", "1.1.1.1")], timezone.now(), lambda: None)
        self.assertEqual(error.exception.code, "no_provider_observation")
        with patch("monitoring.connectors.read_json") as network:
            with self.assertRaises(ConnectorError):
                internetdb(self.connection("internetdb"), [self.asset("ip", "192.0.2.1")], timezone.now(), lambda: None)
            network.assert_not_called()

    @patch.dict(os.environ, {"MONITOR_SECRET_TEST": "test-only"})
    def test_licensed_alerts_keep_counts_and_attribution_without_aliases(self):
        with patch("monitoring.connectors.read_json", return_value={"private-person": ["site.example"], "other-person": ["site.example", "second.example"]}):
            result = hibp(self.connection("hibp"), [self.asset("domain", "example.test")], timezone.now(), lambda: None)
        self.assertEqual(result[0]["data"], {"affected_aliases": 2, "affected_sites": 2})
        self.assertNotIn("private-person", str(result))
        self.assertEqual(result[0]["provenance"]["url"], "https://haveibeenpwned.com/")

    def test_kev_matches_exact_inventoried_cves_only(self):
        asset = self.asset("ip", "192.0.2.1")
        asset.inventories = MagicMock()
        asset.inventories.order_by.return_value.first.return_value = SimpleNamespace(pk=1, cves=["CVE-2021-44228", "CVE-2026-999999"], observed_at=timezone.now() - timedelta(hours=2))
        with patch("monitoring.connectors.read_json", return_value={"catalogVersion": "test-1", "count": 1, "vulnerabilities": [{"cveID": "CVE-2021-44228"}]}):
            result = kev(self.connection("kev"), [asset], timezone.now(), lambda: None)
        self.assertEqual(result[0]["data"]["cves"], ["CVE-2021-44228"])
        self.assertEqual(result[0]["data"]["inventory_id"], 1)
