import io
import uuid
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from workspaces.permissions import get_case, insurer_role, is_owner, require_evidence_access, visible_requests
from workspaces.services import audit
from .forms import BackupForm, ControlForm, EvidenceReviewForm, PolicyForm, RestoreForm, RestoreReviewForm, ServiceForm, UploadForm
from .models import Document, EvidenceVersion, RestoreTest, Service
from .permissions import get_request, require_inventory_editor, require_reviewer, require_version
from .rules import control_summary, current_restore_status, freshness
from .services import assess_control, create_service, publish_policy, record_backup, record_restore, rescan_version, review_evidence, review_restore, upload_evidence
from .storage import StorageUnavailable, read_private


def handle_form(request, form, title, intro, case, submit, callback):
    if request.method == "POST" and form.is_valid():
        try:
            target = callback(form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        except IntegrityError:
            form.add_error(None, "A record with these details already exists. Reload and try again.")
        else:
            return target
    return render(request, "evidence/form.html", {"form": form, "title": title, "intro": intro, "case": case, "button": submit})


@login_required
def request_detail(request, case_id, request_id):
    case, item = get_request(request.user, case_id, request_id)
    summary = control_summary(item)
    documents = [{"document": doc, "versions": [{"version": version, "scan": version.latest_scan, "review": version.latest_review,
        "freshness": freshness(version.observed_at, item.freshness_days)} for version in doc.versions.all()]} for doc in item.documents.select_related("service")]
    tests = [{"test": test, "review": test.latest_review, "status": current_restore_status(test)} for test in item.restore_tests.select_related("service", "policy", "retest_of")]
    return render(request, "evidence/request.html", {"case": case, "item": item, "summary": summary, "documents": documents,
        "reviewer": bool(insurer_role(request.user, case)), "tests": tests,
        "assessments": item.assessments.select_related("reviewer").order_by("-created_at"),
        "backups": item.backup_jobs.select_related("service", "source_version").order_by("-observed_at")})


@login_required
def upload(request, case_id, request_id, document_id=None):
    case, item = get_request(request.user, case_id, request_id)
    document = get_object_or_404(Document, pk=document_id, request=item) if document_id else None
    form = UploadForm(case, request.POST or None, request.FILES or None, document=document)
    if getattr(request, "evidence_upload_too_large", False):
        form.is_valid()
        form.add_error("file", "Files must be no larger than 10 MB.")
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        data["upload"] = data.pop("file")
        try:
            version, created = upload_evidence(request.user, item, document=document, **data)
        except ValidationError as error:
            form.add_error(None, error)
        except StorageUnavailable as error:
            form.add_error(None, str(error))
        else:
            if created:
                messages.success(request, f"Version {version.number} received. File-check status: {version.latest_scan.get_status_display()}.")
            else:
                messages.info(request, "This exact submission already exists; no duplicate version was created.")
            return redirect("evidence_request", case.pk, item.pk)
    return render(request, "evidence/form.html", {"form": form, "title": "Upload a new document version" if document else "Submit evidence", "intro": item.title + ". Source, scope, and observation time are saved with this exact file.", "case": case, "item": item, "button": "Upload evidence", "upload": True})


@login_required
def download(request, case_id, version_id):
    case = get_case(request.user, case_id)
    version = get_object_or_404(EvidenceVersion.objects.select_related("document__request"), pk=version_id, document__request__in=visible_requests(request.user, case))
    scan = version.latest_scan
    if not scan or scan.status != "clean":
        raise Http404
    try:
        data = read_private(version)
    except StorageUnavailable as error:
        return render(request, "evidence/unavailable.html", {"case": case, "error": str(error)}, status=503)
    # Recheck after the storage fetch; no reusable public or presigned URL is exposed.
    request.user.refresh_from_db()
    require_version(request.user, version)
    scan = version.latest_scan
    if not scan or scan.status != "clean":
        raise Http404
    audit(case, request.user, "Evidence version downloaded", str(version.pk))
    response = FileResponse(io.BytesIO(data), as_attachment=True, filename=version.original_name, content_type="application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def rescan(request, case_id, version_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    version = get_object_or_404(EvidenceVersion, pk=version_id, document__request__case=case)
    try:
        scan = rescan_version(request.user, version)
        messages.info(request, scan.detail)
    except StorageUnavailable as error:
        messages.error(request, str(error))
    return redirect("evidence_request", case.pk, version.document.request_id)


@login_required
def version_review(request, case_id, version_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    version = get_object_or_404(EvidenceVersion, pk=version_id, document__request__case=case)
    def save(data):
        review_evidence(request.user, version, **data)
        messages.success(request, "Evidence review recorded against this version.")
        return redirect("evidence_request", case.pk, version.document.request_id)
    return handle_form(request, EvidenceReviewForm(request.POST or None), f"Review {version}", f"Source: {version.source}. Scope: {version.scope}. Observation: {version.observed_at:%Y-%m-%d %H:%M} UTC. File checks: {version.latest_scan.get_status_display() if version.latest_scan else 'Quarantined'}.", case, "Record evidence review", save)


@login_required
def control_review(request, case_id, request_id):
    case, item = get_request(request.user, case_id, request_id)
    require_reviewer(request.user, case)
    def save(data):
        assess_control(request.user, item, **data)
        messages.success(request, "Control assessment saved with exact evidence and review references.")
        return redirect("evidence_request", case.pk, item.pk)
    return handle_form(request, ControlForm(item, request.POST or None), "Assess control", item.title + ". Accepted evidence and a control result are recorded separately.", case, "Save control assessment", save)


@login_required
def inventory(request, case_id):
    case = get_case(request.user, case_id)
    require_evidence_access(request.user, case)
    # A technician can read service metadata only while assigned work in this case.
    if not insurer_role(request.user, case) and not is_owner(request.user, case) and not visible_requests(request.user, case).exists():
        raise Http404
    return render(request, "evidence/inventory.html", {"case": case, "services": case.services.all(),
        "editor": is_owner(request.user, case) or bool(insurer_role(request.user, case)), "reviewer": bool(insurer_role(request.user, case))})


@login_required
def service_create(request, case_id):
    case = get_case(request.user, case_id)
    require_inventory_editor(request.user, case)
    def save(data):
        create_service(request.user, case, data)
        messages.success(request, "Service added with the case checklist's default recovery targets.")
        return redirect("service_inventory", case.pk)
    return handle_form(request, ServiceForm(request.POST or None), "Add a critical service", "Record the business owner and dependencies that must be included in recovery.", case, "Add service", save)


@login_required
def policy_create(request, case_id, service_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    service = get_object_or_404(Service, pk=service_id, case=case)
    current = service.current_policy
    initial = {name: getattr(current, name) for name in ["duration_minutes", "data_age_minutes", "required_scope"]} if current else {}
    def save(data):
        publish_policy(request.user, service, data)
        messages.success(request, "New recovery target version published. Historical tests retain their original targets.")
        return redirect("service_inventory", case.pk)
    return handle_form(request, PolicyForm(request.POST or None, initial=initial), "Publish recovery requirements", service.name + ". This creates a new version for subsequent restore tests.", case, "Publish target version", save)


@login_required
def backup_create(request, case_id, request_id):
    case, item = get_request(request.user, case_id, request_id)
    if item.kind != "backup":
        raise Http404
    def save(data):
        record_backup(request.user, item, data)
        messages.success(request, "Backup job recorded. This does not establish that recovery targets were met.")
        return redirect("evidence_request", case.pk, item.pk)
    return handle_form(request, BackupForm(item, request.POST or None), "Record a backup job", "Record job health and coverage separately from a supervised restore test.", case, "Record backup job", save)


@login_required
def restore_create(request, case_id, request_id):
    case, item = get_request(request.user, case_id, request_id)
    if item.kind != "restore":
        raise Http404
    initial = {}
    if request.GET.get("retest"):
        try:
            parent_id = uuid.UUID(request.GET["retest"])
        except (ValueError, TypeError):
            raise Http404
        parent = get_object_or_404(RestoreTest, pk=parent_id, request=item)
        initial = {"retest_of": parent, "service": parent.service}
    def save(data):
        test = record_restore(request.user, item, data)
        messages.success(request, "Restore test recorded. An insurer reviewer must verify the evidence and scope.")
        return redirect("restore_detail", case.pk, test.pk)
    return handle_form(request, RestoreForm(item, request.POST or None, initial=initial), "Record a supervised restore", "Use measured minutes. Missing measurements remain unknown. A retest creates a linked record.", case, "Record test", save)


@login_required
def restore_detail(request, case_id, test_id):
    case = get_case(request.user, case_id)
    test = get_object_or_404(RestoreTest.objects.select_related("service", "policy", "request", "retest_of"), pk=test_id, request__in=visible_requests(request.user, case))
    reviewer = bool(insurer_role(request.user, case))
    form = RestoreReviewForm(request.POST or None)
    if request.method == "POST":
        require_reviewer(request.user, case)
        if form.is_valid():
            review_restore(request.user, test, **form.cleaned_data)
            messages.success(request, "Restore review saved. The insurer's rule calculated the result.")
            return redirect("restore_detail", case.pk, test.pk)
    return render(request, "evidence/restore.html", {"case": case, "test": test, "form": form, "reviewer": reviewer,
        "current_status": current_restore_status(test), "reviews": test.reviews.select_related("reviewer").order_by("-created_at", "-pk"),
        "versions": test.evidence_versions.select_related("document"), "retests": test.retests.all()})
