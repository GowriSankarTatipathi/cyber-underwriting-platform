import hashlib
import json
import logging
from pathlib import Path
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.http import Http404
from django.utils import timezone

from workspaces.models import Case, EvidenceRequest
from workspaces.permissions import get_case
from workspaces.services import audit
from .models import BackupJob, ControlAssessment, Document, EvidenceReview, EvidenceVersion, FileScan, RecoveryPolicy, RestoreReview, RestoreTest, Service
from .permissions import get_request, require_inventory_editor, require_reviewer, require_version
from .rules import current_restore_status, evaluate_restore, evidence_snapshot, restore_control_tests, usable_version
from .scanning import TYPES, scan_file
from .storage import delete_private, put_private, read_private

logger = logging.getLogger(__name__)


def check_date(value):
    if value is None or timezone.is_naive(value) or value > timezone.now():
        raise ValidationError("Use a valid observation date and time in the past or present (UTC).")


def check_versions(versions, item, service=None):
    if not versions:
        raise ValidationError("Select at least one supporting evidence version.")
    for version in versions:
        if version.document.request_id != item.pk or (service and version.document.service_id != service.pk):
            raise ValidationError("Evidence must belong to this request and service.")
        scan = version.latest_scan
        if not scan or scan.status != "clean":
            raise ValidationError("Quarantined or rejected files cannot support a review or test.")


@transaction.atomic
def ensure_case_service(case):
    Case.objects.select_for_update().get(pk=case.pk)
    service = case.services.first()
    if service:
        return service
    service = Service.objects.create(case=case, name=case.service, owner_name=case.applicant.name,
                                     description="Primary service from the underwriting case.", created_by=case.created_by)
    targets = case.checklist.recovery_targets
    if targets.get("duration_minutes") is not None and targets.get("data_age_minutes") is not None:
        RecoveryPolicy.objects.create(service=service, checklist=case.checklist, number=1,
            duration_minutes=targets["duration_minutes"], data_age_minutes=targets["data_age_minutes"],
            required_scope=f"Restore {service.name}, its required dependencies, and verify the service is usable.", created_by=case.created_by)
    return service


@transaction.atomic
def create_service(user, case, data):
    require_inventory_editor(user, case)
    service = Service(case=case, created_by=user, **data)
    service.full_clean()
    service.save()
    targets = case.checklist.recovery_targets
    if targets.get("duration_minutes") is not None and targets.get("data_age_minutes") is not None:
        RecoveryPolicy.objects.create(service=service, checklist=case.checklist, number=1,
            duration_minutes=targets["duration_minutes"], data_age_minutes=targets["data_age_minutes"],
            required_scope=f"Restore {service.name}, its required dependencies, and verify the service is usable.", created_by=user)
    audit(case, user, "Service added", service.name)
    return service


@transaction.atomic
def publish_policy(user, service, data):
    service = Service.objects.select_for_update().get(pk=service.pk)
    require_reviewer(user, service.case)
    number = (service.policies.aggregate(n=Max("number"))["n"] or 0) + 1
    policy = RecoveryPolicy(service=service, checklist=service.case.checklist, number=number, created_by=user, **data)
    policy.full_clean()
    policy.save()
    audit(service.case, user, "Recovery targets versioned", f"{service.name} · v{number}")
    return policy


def upload_evidence(user, item, upload, service, title, source, scope, observed_at, submission_key, document=None):
    user.refresh_from_db()
    case, item = get_request(user, item.case_id, item.pk)
    check_date(observed_at)
    if service.case_id != case.pk:
        raise Http404
    if document and (document.request_id != item.pk or document.service_id != service.pk):
        raise Http404
    if not all(value.strip() for value in (title, source, scope)):
        raise ValidationError("Provide the document title, source, and covered scope.")
    if upload.size > settings.EVIDENCE_MAX_BYTES:
        raise ValidationError("Files must be no larger than 10 MB.")
    data = upload.read(settings.EVIDENCE_MAX_BYTES + 1)
    if not data or len(data) > settings.EVIDENCE_MAX_BYTES:
        raise ValidationError("Upload a nonempty file up to 10 MB.")
    name = Path(upload.name.replace("\\", "/")).name[:180]
    suffix = Path(name).suffix.lower()
    if suffix not in TYPES:
        raise ValidationError("Use PDF, DOCX, TXT, CSV, JSON, PNG, or JPEG.")
    sha256 = hashlib.sha256(data).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([sha256, source, scope, observed_at.isoformat(), str(service.pk), name], ensure_ascii=True).encode()).hexdigest()
    pointer = None
    try:
        with transaction.atomic():
            # Serialize intake for a request, including first-document creation.
            item = EvidenceRequest.objects.select_for_update().get(pk=item.pk)
            existing = EvidenceVersion.objects.filter(submission_key=submission_key).first()
            if existing:
                if existing.document.request_id == item.pk and existing.fingerprint == fingerprint:
                    return existing, False
                raise ValidationError("This submission key has already been used. Reload the upload form.")
            if document:
                document = Document.objects.select_for_update().get(pk=document.pk)
                existing = document.versions.filter(fingerprint=fingerprint).first()
                if existing:
                    return existing, False
            else:
                document = Document.objects.create(request=item, service=service, title=title, created_by=user)
            number = (document.versions.aggregate(n=Max("number"))["n"] or 0) + 1
            version_id = uuid.uuid4()
            key = f"cases/{case.pk}/documents/{document.pk}/{version_id}{suffix}"
            pointer = put_private(key, data, TYPES[suffix], sha256)
            # A storage round trip must not bypass an intervening revocation.
            user.refresh_from_db()
            case, item = get_request(user, case.pk, item.pk)
            version = EvidenceVersion.objects.create(id=version_id, document=document, number=number,
                submission_key=submission_key, fingerprint=fingerprint, original_name=name,
                content_type=TYPES[suffix], byte_size=len(data), sha256=sha256, source=source,
                scope=scope, observed_at=observed_at, uploaded_by=user, **pointer)
            FileScan.objects.create(version=version, status="quarantined", engine="intake", detail="Waiting for file checks.", actor=user)
            audit(case, user, "Evidence version uploaded", f"{document.title} · v{number} · {version.pk}")
    except Exception:
        if pointer:
            try:
                delete_private(pointer)
            except Exception:
                logger.error("Failed intake left an orphaned storage object; reconcile unreferenced objects.")
        raise
    # Commit the quarantined version before running the scanner. A crash leaves
    # a visible retryable record, never a released or silently lost upload.
    rescan_version(user, version, data=data, initial=True)
    return version, True


def rescan_version(user, version, data=None, initial=False):
    user.refresh_from_db()
    case = require_version(user, version)
    if not initial:
        require_reviewer(user, case)
    elif version.uploaded_by_id != user.pk:
        raise Http404
    data = data if data is not None else read_private(version)
    status, engine, detail = scan_file(data, version.original_name)
    with transaction.atomic():
        user.refresh_from_db()
        case = require_version(user, version)
        if not initial:
            require_reviewer(user, case)
        scan = FileScan.objects.create(version=version, status=status, engine=engine, detail=detail, actor=user)
        audit(case, user, "Evidence file checks completed", f"{version.pk} · {status}")
    return scan


@transaction.atomic
def review_evidence(user, version, decision, scope_confirmed, rationale):
    case = require_version(user, version)
    require_reviewer(user, case)
    Document.objects.select_for_update().get(pk=version.document_id)
    if decision == "accepted":
        if not version.latest_scan or version.latest_scan.status != "clean":
            raise ValidationError("A quarantined or unsafe file cannot be accepted.")
        if not scope_confirmed:
            raise ValidationError("Confirm that the evidence covers the required scope before accepting it.")
    review = EvidenceReview(version=version, decision=decision, scope_confirmed=scope_confirmed, rationale=rationale, reviewer=user)
    review.full_clean()
    review.save()
    audit(case, user, "Evidence reviewed", f"{version.pk} · {decision}")
    return review


@transaction.atomic
def assess_control(user, item, result, rationale, evidence_versions):
    case, item = get_request(user, item.case_id, item.pk)
    require_reviewer(user, case)
    EvidenceRequest.objects.select_for_update().get(pk=item.pk)
    versions = list(evidence_versions)
    check_versions(versions, item)
    if result == "verified":
        current = [doc.latest_version for doc in item.documents.all()]
        if {v.pk for v in versions} != {v.pk for v in current if v}:
            raise ValidationError("Review and cite every current document version before verifying this control.")
        if any(not usable_version(v, item.freshness_days) for v in versions):
            raise ValidationError("Verification requires accepted, scope-confirmed, fresh evidence.")
        if item.kind == "restore":
            latest_tests = restore_control_tests(item)
            if not latest_tests:
                raise ValidationError("Every critical service needs a passing latest restore against current targets first.")
            for latest in latest_tests:
                if not set(latest.evidence_versions.values_list("pk", flat=True)).issubset({v.pk for v in versions}):
                    raise ValidationError("Include the evidence versions used by each service's latest restore.")
    assessment = ControlAssessment(request=item, result=result, rationale=rationale, review_snapshot=evidence_snapshot(versions),
        checklist_version=case.checklist.version, freshness_days=item.freshness_days, reviewer=user)
    assessment.full_clean()
    assessment.save()
    assessment.evidence_versions.set(versions)
    audit(case, user, "Control assessed", f"{item.title} · {result}")
    return assessment


@transaction.atomic
def record_backup(user, item, data):
    case, item = get_request(user, item.case_id, item.pk)
    if item.kind != "backup":
        raise ValidationError("Use a backup coverage request to record a backup job.")
    service = data["service"]
    if service.case_id != case.pk:
        raise Http404
    check_date(data["observed_at"])
    check_versions([data["source_version"]], item, service)
    job = BackupJob(request=item, recorded_by=user, **data)
    job.full_clean()
    job.save()
    audit(case, user, "Backup job recorded", f"{service.name} · {job.status}")
    return job


@transaction.atomic
def record_restore(user, item, data):
    case, item = get_request(user, item.case_id, item.pk)
    if item.kind != "restore":
        raise ValidationError("Use a supervised restore request to record a test.")
    data = dict(data)
    versions = list(data.pop("evidence_versions"))
    service = data["service"]
    if service.case_id != case.pk:
        raise Http404
    Service.objects.select_for_update().get(pk=service.pk)
    policy = service.current_policy
    if not policy:
        raise ValidationError("An insurer reviewer must set recovery targets for this service first.")
    parent = data.get("retest_of")
    if parent and (parent.service_id != service.pk or parent.request_id != item.pk):
        raise ValidationError("A retest must link to a test of the same service and request.")
    if parent and data["performed_at"] <= parent.performed_at:
        raise ValidationError("A retest must occur after the original test.")
    check_date(data["performed_at"])
    check_versions(versions, item, service)
    test = RestoreTest(request=item, policy=policy, recorded_by=user,
        service_snapshot={"name": service.name, "owner": service.owner_name, "dependencies": service.dependencies}, **data)
    test.full_clean()
    test.save()
    test.evidence_versions.set(versions)
    audit(case, user, "Restore retest recorded" if parent else "Restore test recorded", str(test.pk))
    return test


@transaction.atomic
def review_restore(user, test, scope_confirmed, rationale):
    case, item = get_request(user, test.request.case_id, test.request_id)
    require_reviewer(user, case)
    if not rationale.strip():
        raise ValidationError("A review rationale is required.")
    RestoreTest.objects.select_for_update().get(pk=test.pk)
    result, reasons = evaluate_restore(test, scope_confirmed)
    review = RestoreReview.objects.create(test=test, scope_confirmed=scope_confirmed, result=result,
        reasons=reasons, evidence_snapshot=evidence_snapshot(test.evidence_versions.all()), rationale=rationale, reviewer=user)
    audit(case, user, "Restore test reviewed", f"{test.pk} · {result}")
    return review
