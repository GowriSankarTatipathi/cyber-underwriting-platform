import ipaddress
import re
import uuid
from datetime import datetime
from typing import Literal
from django.core.exceptions import ValidationError
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field

CVE_RE = re.compile(r"CVE-\d{4}-\d{4,19}\Z")
PROVIDER_KINDS = {"entra": {"tenant"}, "aws_backup": {"aws_resource"}, "internetdb": {"ip"}, "hibp": {"domain"}, "kev": {"ip", "aws_resource"}, "demo": {"tenant", "aws_resource", "ip", "domain"}}


def identifier(kind, value):
    value = value.strip()
    try:
        if kind == "tenant":
            return str(uuid.UUID(value))
        if kind == "ip":
            address = ipaddress.ip_address(value)
            # Documentation networks are accepted for local demo assets, never queried live.
            if not address.is_global and not any(address in ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24") if address.version == 4):
                raise ValueError
            return str(address)
        if kind == "domain":
            domain = value.rstrip(".").encode("idna").decode().lower()
            if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
                raise ValueError
            return domain
        if kind == "aws_resource" and re.fullmatch(r"arn:aws:[a-z0-9-]+:[a-z0-9-]+:\d{12}:[A-Za-z0-9:/_.+=,@-]+", value):
            return value
    except (ValueError, UnicodeError):
        pass
    raise ValidationError("Use a valid tenant UUID, public IP, domain, or AWS resource ARN for the selected asset kind.")


def validate_config(provider, config, assets, credential_ref):
    if provider not in PROVIDER_KINDS or not isinstance(config, dict):
        raise ValidationError("Select a supported monitoring provider and JSON configuration.")
    if not assets or len(assets) > 20 or any(a.kind not in PROVIDER_KINDS[provider] for a in assets):
        raise ValidationError("Choose 1–20 assets supported by this provider.")
    allowed = {"entra": {"client_id"}, "aws_backup": {"account_id", "region", "role_arn"}, "demo": {"scenario"}, "internetdb": set(), "hibp": set(), "kev": set()}[provider]
    if set(config) - allowed or any(not isinstance(value, str) for value in config.values()):
        raise ValidationError("Configuration contains unsupported fields. Secrets belong in server environment variables.")
    if credential_ref and not re.fullmatch(r"MONITOR_SECRET_[A-Z0-9_]{1,80}", credential_ref):
        raise ValidationError("Use an environment variable name beginning MONITOR_SECRET_.")
    if provider in ("entra", "hibp") and not credential_ref:
        raise ValidationError("Provide the server-side secret reference for this provider.")
    if provider == "entra":
        try:
            uuid.UUID(config.get("client_id", ""))
        except ValueError:
            raise ValidationError("Set client_id to the registered Entra application UUID.") from None
        if len(assets) != 1:
            raise ValidationError("Use one tenant per Entra connection.")
    if provider == "aws_backup":
        account, region = config.get("account_id", ""), config.get("region", "")
        if not re.fullmatch(r"\d{12}", account) or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", region):
            raise ValidationError("Set the AWS account_id and region.")
        if any(a.identifier.split(":")[3:5] != [region, account] for a in assets):
            raise ValidationError("AWS resource ARNs must match the configured account and region.")
        if config.get("role_arn") and not re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9/+=,.@_-]+", config["role_arn"]):
            raise ValidationError("The role ARN must belong to the configured AWS account.")
    if provider == "demo" and config.get("scenario", "risk") not in ("risk", "healthy"):
        raise ValidationError("Demo scenario must be risk or healthy.")


class ObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: uuid.UUID
    kind: Literal["identity", "backup", "exposure", "credential", "vulnerability"]
    source_event_id: str = Field(min_length=1, max_length=200)
    observed_at: datetime
    source_time_known: bool
    data: dict
    provenance: dict


def validate_observation(value, assets):
    row = ObservationInput.model_validate(value)
    asset = next((a for a in assets if a.pk == row.asset_id), None)
    if not asset or timezone.is_naive(row.observed_at) or row.observed_at > timezone.now() + timezone.timedelta(minutes=5):
        raise ValidationError("Observation has an invalid asset or observation date.")
    expected = {"identity": {"tenant"}, "backup": {"aws_resource"}, "exposure": {"ip"}, "credential": {"domain"}, "vulnerability": {"ip", "aws_resource"}}
    if asset.kind not in expected[row.kind]:
        raise ValidationError("Observation does not match the asset type.")
    data = row.data
    fields = {"identity": {"total", "registered", "capable"}, "backup": {"status", "job_id", "completed_at"},
        "exposure": {"ports", "cves"}, "credential": {"affected_aliases", "affected_sites"}, "vulnerability": {"cves", "catalog_version", "inventory_id"}}
    if set(data) != fields[row.kind]:
        raise ValidationError("Observation fields do not match its type.")
    def count(v):
        return type(v) is int and 0 <= v <= 100_000_000
    if row.kind == "identity" and (not all(count(v) for v in data.values()) or max(data["registered"], data["capable"]) > data["total"]):
        raise ValidationError("Invalid identity counts.")
    if row.kind == "credential" and not all(count(v) for v in data.values()):
        raise ValidationError("Invalid credential alert counts.")
    if row.kind == "backup":
        if data["status"] not in ("success", "failed", "partial", "pending", "unknown") or not isinstance(data["job_id"], str) or len(data["job_id"]) > 150:
            raise ValidationError("Invalid backup job status.")
        if data["completed_at"]:
            moment = datetime.fromisoformat(data["completed_at"])
            if timezone.is_naive(moment) or moment > timezone.now() + timezone.timedelta(minutes=5):
                raise ValidationError("Invalid backup completion time.")
    if "ports" in data and (not isinstance(data["ports"], list) or len(data["ports"]) > 65535 or any(type(p) is not int or not 1 <= p <= 65535 for p in data["ports"])):
        raise ValidationError("Invalid port observations.")
    if "cves" in data and (not isinstance(data["cves"], list) or len(data["cves"]) > 1000 or any(not isinstance(c, str) or not CVE_RE.fullmatch(c) for c in data["cves"])):
        raise ValidationError("Invalid vulnerability identifiers.")
    if set(row.provenance) - {"source", "url", "note", "retrieved_at", "digest"} or any(not isinstance(v, str) or len(v) > 1000 for v in row.provenance.values()):
        raise ValidationError("Invalid source provenance.")
    return row, asset
