import hashlib
import json

from django.core.serializers.json import DjangoJSONEncoder
from evidence.exports import request_payload
from .permissions import finding_owners


def normalize(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def checksum(value):
    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def records(queryset, exclude=()):
    fields = [field.attname for field in queryset.model._meta.concrete_fields if field.name not in exclude]
    return list(queryset.order_by("pk").values(*fields))


def finding_data(finding):
    return normalize({"id": finding.pk, "request_id": finding.request_id, "title": finding.title,
        "description": finding.description, "kind": finding.kind, "priority": finding.priority,
        "priority_reason": finding.priority_reason, "owner_id": finding.owner_id,
        "owner_has_access": finding_owners(finding.case, finding.request).filter(pk=finding.owner_id).exists(),
        "owner": finding.owner.email, "due_date": finding.due_date, "state": finding.state,
        "revision": finding.revision, "retest_of": finding.retest_of_id})


def case_snapshot(case):
    """Canonical decision material. Private object locations never enter reports or exports."""
    requests = []
    for item in case.requests.order_by("pk"):
        payload = request_payload(item)
        # Fix otherwise unspecified queryset order so repeated previews compare reliably.
        payload["documents"].sort(key=lambda value: value["id"])
        payload["backup_jobs"].sort(key=lambda value: value["id"])
        payload["restore_tests"].sort(key=lambda value: value["id"])
        for test in payload["restore_tests"]:
            test["evidence_version_ids"].sort()
        payload["due_date"] = item.due_date
        payload["versions"] = []
        for doc in item.documents.order_by("pk"):
            for version in doc.versions.order_by("pk"):
                data = records(type(version).objects.filter(pk=version.pk), exclude=("storage_key", "storage_bucket", "object_version", "submission_key", "fingerprint"))[0]
                data["reviews"] = records(version.reviews.all())
                data["scans"] = records(version.scans.all())
                payload["versions"].append(data)
        payload["assessments"] = records(item.assessments.all())
        for assessment in payload["assessments"]:
            assessment["version_ids"] = list(item.assessments.get(pk=assessment["id"]).evidence_versions.order_by("pk").values_list("pk", flat=True))
        payload["test_records"] = records(item.restore_tests.all())
        for test in payload["test_records"]:
            obj = item.restore_tests.get(pk=test["id"])
            test["reviews"] = records(obj.reviews.all())
        requests.append(payload)
    findings = []
    for finding in case.findings.order_by("pk"):
        data = finding_data(finding)
        data["events"] = records(finding.events.all())
        data["responses"] = records(finding.responses.all())
        for response in data["responses"]:
            response["version_ids"] = list(finding.responses.get(pk=response["id"]).evidence_versions.order_by("pk").values_list("pk", flat=True))
        data["exceptions"] = []
        for exception in finding.exceptions.order_by("pk"):
            row = records(finding.exceptions.filter(pk=exception.pk))[0]
            row["active"] = exception.active
            row["revocation"] = records(type(exception.revocation).objects.filter(pk=exception.revocation.pk))[0] if hasattr(exception, "revocation") else None
            data["exceptions"].append(row)
        findings.append(data)
    from ai_review.models import DraftDisposition
    ai_followups = [{"finding_id": row.finding_id, "disposition_id": row.pk, "draft_id": row.draft_id,
        "review_snapshot": row.draft.review_snapshot, "published_content": row.published_content,
        "actor_id": row.actor_id, "created_at": row.created_at}
        for row in DraftDisposition.objects.filter(finding__case=case, action="publish").select_related("draft").order_by("pk")]
    # Preserve comparisons with Phase 4 snapshots until AI actually contributes a request.
    ai_material = {"ai_followups": ai_followups} if ai_followups else {}
    monitoring_material = {}
    if case.monitored_assets.exists():
        from monitoring.rules import case_material
        monitoring_material = {"monitoring": case_material(case)}
    return normalize({"schema_version": 3 if monitoring_material else 2 if ai_followups else 1, "rule_versions": ["evidence-v1", "restore-v1", "underwriting-v1"], **ai_material, **monitoring_material,
        "case": {"id": case.pk, "reference": case.reference, "kind": case.kind, "applicant": case.applicant.name,
            "insurer": case.insurer.name, "renewal_of": case.renewal_of_id, "due_date": case.due_date,
            "internal_notes": case.internal_notes},
        "checklist": {"id": case.checklist_id, "product": case.checklist.product, "version": case.checklist.version,
            "requirements": case.checklist.requirements, "recovery_targets": case.checklist.recovery_targets},
        "services": [{**row, "policies": records(case.services.get(pk=row["id"]).policies.all())} for row in records(case.services.all())],
        "requests": requests, "findings": findings})
