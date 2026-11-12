from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from evidence.models import EvidenceVersion
from evidence.permissions import require_reviewer
from underwriting.permissions import require_decider
from underwriting.snapshots import checksum
from workspaces.permissions import can_manage, get_case, insurer_role, is_owner, require_evidence_access, require_owner
from .errors import ProcessingError
from .forms import ConfirmForm, ConsentForm, FactReviewForm, PublishForm, RejectForm
from .models import FollowUpDraft, ProcessingConsent, ReviewJob
from .permissions import require_current_result, require_result_access
from .rules import discrepancies, fact_rows, rule_preview
from .validation import LABELS
from . import services


def provider_label():
    return "Local demo parser — no live AI" if settings.AI_REVIEW_PROVIDER == "demo" else "OpenAI" if settings.AI_REVIEW_PROVIDER == "openai" else "Not configured"


def form_page(request, case, form, title, intro, button, callback, **extra):
    if request.method == "POST" and form.is_valid():
        try:
            return callback(dict(form.cleaned_data))
        except ValidationError as error:
            form.add_error(None, error)
        except ProcessingError as error:
            form.add_error(None, error.message)
    return render(request, "ai_review/form.html", {"case": case, "form": form, "title": title, "intro": intro, "button": button, **extra})


@login_required
def overview(request, case_id):
    case = get_case(request.user, case_id)
    require_evidence_access(request.user, case)
    staff = bool(insurer_role(request.user, case))
    owner = is_owner(request.user, case)
    if not staff and not owner:
        raise Http404
    return render(request, "ai_review/overview.html", {"case": case, "staff": staff, "owner": owner,
        "jobs": case.ai_jobs.select_related("version__document", "requested_by") if staff else [],
        "consents": case.ai_consents.select_related("authorized_by").order_by("-created_at"), "provider_label": provider_label()})


@login_required
def consent_create(request, case_id):
    case = get_case(request.user, case_id)
    require_owner(request.user, case)
    def save(data):
        services.authorize_external(request.user, case, **data)
        messages.success(request, "External processing authorization recorded. Reviewers still choose each document to process.")
        return redirect("ai_overview", case.pk)
    return form_page(request, case, ConsentForm(request.POST or None), "Authorize external evidence processing",
        "OpenAI may receive the extracted text of documents selected by an insurer reviewer. Local demo parsing stays on this machine. You can revoke future processing at any time; already completed local results remain subject to case access.", "Authorize external processing", save)


@login_required
@require_POST
def consent_revoke(request, case_id, consent_id):
    case = get_case(request.user, case_id)
    consent = get_object_or_404(ProcessingConsent, pk=consent_id, case=case)
    services.revoke_external(request.user, consent)
    messages.success(request, "External processing authorization revoked. Running jobs will discard results if their authorization check fails.")
    return redirect("ai_overview", case.pk)


@login_required
def queue(request, case_id, version_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    version = get_object_or_404(EvidenceVersion, pk=version_id, document__request__case=case)
    def save(data):
        job, created = services.queue_review(request.user, version)
        messages.success(request, "Review queued. Manual evidence review remains available." if created else "This exact processing request already exists.")
        return redirect("ai_job", case.pk, job.pk)
    return form_page(request, case, ConfirmForm(request.POST or None), "Extract evidence facts",
        f"{version}. Provider: {provider_label()}. Processing proposes cited facts; it does not accept evidence or record an underwriting decision.", "Queue document review", save)


def get_job(request, case_id, job_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    return case, get_object_or_404(ReviewJob.objects.select_related("version__document__request", "policy"), pk=job_id, case=case)


@login_required
def job_detail(request, case_id, job_id):
    case, job = get_job(request, case_id, job_id)
    result = getattr(job, "result", None)
    if result:
        require_result_access(request.user, job)
    warning = ""
    if result:
        try:
            require_current_result(request.user, result)
        except ProcessingError as error:
            warning = error.message
    return render(request, "ai_review/job.html", {"case": case, "job": job, "result": result, "warning": warning,
        "rows": fact_rows(result) if result else [], "candidate_preview": rule_preview(result, False) if result else None,
        "reviewed_preview": rule_preview(result) if result else None, "discrepancies": discrepancies(result) if result else [],
        "drafts": result.drafts.order_by("-created_at") if result else [], "runs": job.runs.order_by("number"),
        "can_retry": job.status in ("queued", "failed", "blocked", "cancelled", "retry_wait") or job.stalled})


@login_required
def job_status(request, case_id, job_id):
    case, job = get_job(request, case_id, job_id)
    return JsonResponse({"status": job.status, "label": job.get_status_display(), "stalled": bool(job.stalled)})


@login_required
@require_POST
def job_action(request, case_id, job_id, action):
    case, job = get_job(request, case_id, job_id)
    try:
        if action == "retry":
            services.retry_job(request.user, job)
        elif action == "cancel":
            services.cancel_job(request.user, job)
        else:
            raise Http404
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    return redirect("ai_job", case.pk, job.pk)


@login_required
def fact_review(request, case_id, job_id, fact_key):
    case, job = get_job(request, case_id, job_id)
    result = getattr(job, "result", None)
    if not result:
        raise Http404
    require_result_access(request.user, job)
    fact = next((row for row in result.facts if row["key"] == fact_key), None)
    if not fact:
        raise Http404
    latest = result.reviews.filter(fact_key=fact_key).order_by("-created_at", "-pk").first()
    citation = (latest.citations if latest else fact["citations"])
    citation = citation[0] if citation else {}
    initial = {"previous_review_id": latest.pk if latest else 0, "value": fact["value"],
        "passage_id": citation.get("passage_id", ""), "quote": citation.get("quote", "")}
    def save(data):
        services.review_fact(request.user, result, fact_key, **data)
        messages.success(request, "Suggestion review recorded. Evidence acceptance and restore verification remain separate.")
        return redirect("ai_job", case.pk, job.pk)
    return form_page(request, case, FactReviewForm(result, request.POST or None, initial=initial), f"Review {LABELS[fact['field']]}",
        "Check the original document. For a correction, select a passage and paste an exact quote. Citation validation verifies source matching, not the truth of the underlying evidence.",
        "Record suggestion review", save, result=result, job=job, fact=fact)


@login_required
@require_POST
def draft_prepare(request, case_id, job_id):
    case, job = get_job(request, case_id, job_id)
    result = getattr(job, "result", None)
    if not result:
        raise Http404
    try:
        draft = services.prepare_followup(request.user, result)
        return redirect("ai_draft", case.pk, draft.pk)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    except ProcessingError as error:
        messages.error(request, error.message)
    return redirect("ai_job", case.pk, job.pk)


@login_required
def draft_detail(request, case_id, draft_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    draft = get_object_or_404(FollowUpDraft, pk=draft_id, result__job__case=case)
    require_result_access(request.user, draft.result.job)
    current = checksum(services.draft_context(draft.result)) == draft.context_sha256
    try:
        require_current_result(request.user, draft.result)
    except ProcessingError:
        current = False
    form = PublishForm(case, request.POST or None, initial=draft.content) if can_manage(request.user, case) else None
    if request.method == "POST":
        require_decider(request.user, case)
        if form.is_valid():
            data = dict(form.cleaned_data)
            rationale, confirm = data.pop("rationale"), data.pop("confirm")
            try:
                disposition = services.publish_followup(request.user, draft, data, rationale, confirm)
                messages.success(request, "Reviewed request sent to the assigned case workspace. No email was sent.")
                return redirect("finding_detail", case.pk, disposition.finding_id)
            except ValidationError as error:
                form.add_error(None, error)
            except ProcessingError as error:
                form.add_error(None, error.message)
    return render(request, "ai_review/draft.html", {"case": case, "draft": draft, "form": form, "current": current})


@login_required
def draft_reject(request, case_id, draft_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    draft = get_object_or_404(FollowUpDraft, pk=draft_id, result__job__case=case)
    require_result_access(request.user, draft.result.job)
    def save(data):
        services.reject_followup(request.user, draft, **data)
        return redirect("ai_draft", case.pk, draft.pk)
    return form_page(request, case, RejectForm(request.POST or None), "Reject follow-up draft", draft.content["title"], "Reject draft", save)
