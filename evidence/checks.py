import json
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def private_evidence_configuration(app_configs, **kwargs):
    if settings.DEBUG:
        return []
    errors = []
    if settings.EVIDENCE_STORAGE_BACKEND != "s3" or not settings.EVIDENCE_S3_BUCKET:
        errors.append(Error("Production evidence storage requires a configured private S3 bucket.", id="evidence.E001"))
    if settings.EVIDENCE_SCAN_MODE != "external":
        errors.append(Error("Production evidence requires an external malware scanner.", id="evidence.E002"))
    try:
        command = json.loads(settings.EVIDENCE_SCAN_COMMAND)
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise ValueError
    except (ValueError, TypeError):
        errors.append(Error("Set EVIDENCE_SCAN_COMMAND to a nonempty JSON array of scanner arguments.", id="evidence.E003"))
    if settings.EVIDENCE_S3_ENDPOINT_URL and not settings.EVIDENCE_S3_ENDPOINT_URL.startswith("https://"):
        errors.append(Error("Production S3 endpoints must use HTTPS.", id="evidence.E004"))
    return errors
