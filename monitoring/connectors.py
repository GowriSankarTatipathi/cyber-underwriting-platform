"""Read-only adapters. Fixed provider hosts; never scan applicant infrastructure."""
from datetime import datetime, timedelta
import ipaddress
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.utils import timezone
from underwriting.snapshots import checksum


class ConnectorError(Exception):
    def __init__(self, code, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_json(url, *, host, headers=None, body=None, missing=None):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != host or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ConnectorError("invalid_provider_url")
    request = Request(url, data=body, headers={"User-Agent": "CyberUnderwritingPilot/1.0", **(headers or {})})
    try:
        with build_opener(NoRedirect).open(request, timeout=20) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ConnectorError("provider_size_limit")
        return json.loads(raw)
    except HTTPError as error:
        if error.code == 404 and missing is not None:
            return missing
        raise ConnectorError("provider_unavailable" if error.code == 429 or error.code >= 500 else "provider_access_denied", error.code == 429 or error.code >= 500) from None
    except (URLError, TimeoutError, OSError):
        raise ConnectorError("provider_unavailable", True) from None
    except (ValueError, TypeError):
        raise ConnectorError("invalid_provider_response") from None


def secret(connection):
    value = os.getenv(connection.credential_ref, "")
    if not value:
        raise ConnectorError("credential_not_configured")
    return value


def row(asset, kind, data, now, source, url="", observed=None, known=False, event=None, note=""):
    return {"asset_id": str(asset.pk), "kind": kind, "data": data,
        "observed_at": observed or now, "source_time_known": known, "source_event_id": event or checksum(data),
        "provenance": {"source": source, "url": url, "retrieved_at": now.isoformat(), "note": note, "digest": checksum(data)}}


def demo(connection, assets, now):
    risk = connection.config.get("scenario", "risk") == "risk"
    rows = []
    for asset in assets:
        kind, data = {
            "tenant": ("identity", {"total": 10, "registered": 8 if risk else 10, "capable": 8 if risk else 10}),
            "aws_resource": ("backup", {"status": "failed" if risk else "success", "job_id": "fictional-backup", "completed_at": (now - timedelta(hours=2)).isoformat()}),
            "ip": ("exposure", {"ports": [443, 3389] if risk else [443], "cves": ["CVE-2021-44228"] if risk else []}),
            "domain": ("credential", {"affected_aliases": 2 if risk else 0, "affected_sites": 1 if risk else 0}),
        }[asset.kind]
        rows.append(row(asset, kind, data, now, "Fictional local fixture", observed=now, known=True, note="DEMO ONLY: no provider or applicant infrastructure was queried."))
    return rows


def entra(connection, assets, now, guard):
    asset = assets[0]
    token = read_json(f"https://login.microsoftonline.com/{asset.identifier}/oauth2/v2.0/token", host="login.microsoftonline.com",
        headers={"Content-Type": "application/x-www-form-urlencoded"}, body=urlencode({"grant_type": "client_credentials", "client_id": connection.config["client_id"], "client_secret": secret(connection), "scope": "https://graph.microsoft.com/.default"}).encode())
    if not isinstance(token.get("access_token"), str):
        raise ConnectorError("provider_access_denied")
    url = "https://graph.microsoft.com/v1.0/reports/authenticationMethods/userRegistrationDetails"
    values, seen, dates = [], set(), []
    for _ in range(25):
        guard()
        payload = read_json(url, host="graph.microsoft.com", headers={"Authorization": "Bearer " + token["access_token"]})
        page = payload.get("value")
        if not isinstance(page, list):
            raise ConnectorError("invalid_provider_response")
        for item in page:
            if not isinstance(item.get("id"), str) or type(item.get("isMfaRegistered")) is not bool or type(item.get("isMfaCapable")) is not bool:
                raise ConnectorError("invalid_provider_response")
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            values.append((item["isMfaRegistered"], item["isMfaCapable"]))
            if item.get("lastUpdatedDateTime"):
                dates.append(datetime.fromisoformat(item["lastUpdatedDateTime"].replace("Z", "+00:00")))
        url = payload.get("@odata.nextLink")
        if not url:
            break
        if not url.startswith("https://graph.microsoft.com/v1.0/reports/authenticationMethods/userRegistrationDetails?"):
            raise ConnectorError("invalid_provider_url")
    else:
        raise ConnectorError("provider_page_limit")
    known = bool(values) and len(dates) == len(values)
    data = {"total": len(values), "registered": sum(v[0] for v in values), "capable": sum(v[1] for v in values)}
    return [row(asset, "identity", data, now, "Microsoft Entra ID", "https://learn.microsoft.com/en-us/graph/api/authenticationmethodsroot-list-userregistrationdetails?view=graph-rest-1.0",
        observed=min(dates) if known else now, known=known, note="Registration/capability report; excludes disabled users. Does not establish MFA enforcement or privileged-account coverage.")]


def aws_backup(connection, assets, now, guard):
    cfg = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})
    options = {"region_name": connection.config["region"], "config": cfg}
    try:
        if connection.config.get("role_arn"):
            role_args = {"RoleArn": connection.config["role_arn"], "RoleSessionName": "cyber-underwriting-monitor"}
            if connection.credential_ref:
                role_args["ExternalId"] = secret(connection)
            creds = boto3.client("sts", **options).assume_role(**role_args)["Credentials"]
            options.update(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])
        guard()
        if boto3.client("sts", **options).get_caller_identity()["Account"] != connection.config["account_id"]:
            raise ConnectorError("aws_account_mismatch")
        client, rows = boto3.client("backup", **options), []
        for asset in assets:
            jobs, cursor = [], None
            for _ in range(20):
                guard()
                args = {"ByResourceArn": asset.identifier, "ByCreatedAfter": now - timedelta(days=30), "MaxResults": 100}
                if cursor:
                    args["NextToken"] = cursor
                page = client.list_backup_jobs(**args)
                jobs.extend(page.get("BackupJobs", []))
                cursor = page.get("NextToken")
                if not cursor:
                    break
            else:
                raise ConnectorError("provider_page_limit")
            if any(j.get("ResourceArn") != asset.identifier for j in jobs):
                raise ConnectorError("provider_asset_mismatch")
            job = max(jobs, key=lambda j: j["CreationDate"], default=None)
            status = "unknown"
            if job:
                state = job.get("State")
                status = "success" if state == "COMPLETED" and job.get("MessageCategory") == "SUCCESS" else "partial" if state in ("PARTIAL", "COMPLETED") else "failed" if state in ("FAILED", "ABORTED", "EXPIRED") else "pending"
            data = {"status": status, "job_id": job.get("BackupJobId", "") if job else "", "completed_at": job["CompletionDate"].isoformat() if job and job.get("CompletionDate") else None}
            rows.append(row(asset, "backup", data, now, "AWS Backup", "https://docs.aws.amazon.com/aws-backup/latest/devguide/monitoring.html",
                observed=job.get("CompletionDate") or job["CreationDate"] if job else now, known=bool(job), event=data["job_id"] or "no-jobs",
                note="Latest job within the provider's 30-day window. Backup status does not verify a restore."))
        return rows
    except (BotoCoreError, ClientError):
        raise ConnectorError("aws_access_or_service_error", True) from None


def internetdb(connection, assets, now, guard):
    rows = []
    for asset in assets:
        if not ipaddress.ip_address(asset.identifier).is_global:
            raise ConnectorError("nonpublic_asset")
        guard()
        url = "https://internetdb.shodan.io/" + quote(asset.identifier, safe="")
        data = read_json(url, host="internetdb.shodan.io", missing={"absent": True})
        if data.get("absent"):
            raise ConnectorError("no_provider_observation")  # Absence is not a clean scan.
        if data.get("ip") != asset.identifier:
            raise ConnectorError("provider_asset_mismatch")
        rows.append(row(asset, "exposure", {"ports": sorted(set(data["ports"])), "cves": sorted(set(data.get("vulns", [])))}, now,
            "Shodan InternetDB", "https://internetdb.shodan.io/", note="Passive third-party metadata, updated weekly. Source observation time unavailable; CVEs are potential matches, not verified vulnerabilities."))
    return rows


def hibp(connection, assets, now, guard):
    rows = []
    for asset in assets:
        guard()
        payload = read_json("https://haveibeenpwned.com/api/v3/stealerLogsByEmailDomain/" + quote(asset.identifier, safe=""), host="haveibeenpwned.com", headers={"hibp-api-key": secret(connection)}, missing={})
        if not isinstance(payload, dict) or any(not isinstance(sites, list) or any(not isinstance(site, str) for site in sites) for sites in payload.values()):
            raise ConnectorError("invalid_provider_response")
        data = {"affected_aliases": len(payload), "affected_sites": len({site for sites in payload.values() for site in sites})}
        rows.append(row(asset, "credential", data, now, "Have I Been Pwned", "https://haveibeenpwned.com/", note="Historical stealer-log exposure counts. No passwords or account aliases retained; no claim of current compromise or absence of compromise."))
    return rows


def kev(connection, assets, now, guard):
    guard()
    url = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
    feed = read_json(url, host="www.cisa.gov")
    if not isinstance(feed.get("vulnerabilities"), list) or len(feed["vulnerabilities"]) != feed.get("count"):
        raise ConnectorError("invalid_provider_response")
    known = {item["cveID"] for item in feed["vulnerabilities"]}
    rows = []
    for asset in assets:
        inventory = asset.inventories.order_by("-observed_at", "-pk").first()
        if not inventory:
            raise ConnectorError("asset_inventory_missing")
        data = {"cves": sorted(known.intersection(inventory.cves)), "catalog_version": str(feed["catalogVersion"]), "inventory_id": inventory.pk}
        rows.append(row(asset, "vulnerability", data, now, "CISA Known Exploited Vulnerabilities", "https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
            observed=inventory.observed_at, known=True, note="Exact CVE match against the recorded asset inventory. Applicability and remediation require review; this does not discover assets or infer affected versions."))
    return rows


def collect(connection, assets, guard):
    guard()
    now = timezone.now()
    if connection.provider == "demo":
        if not settings.DEBUG:
            raise ConnectorError("demo_disabled")
        return demo(connection, assets, now)
    if not settings.MONITORING_LIVE_ENABLED:
        raise ConnectorError("live_monitoring_disabled")
    return {"entra": entra, "aws_backup": aws_backup, "internetdb": internetdb, "hibp": hibp, "kev": kev}[connection.provider](connection, assets, now, guard)
