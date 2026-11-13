import json
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from evidence.permissions import require_reviewer
from underwriting.permissions import require_decider
from workspaces.permissions import can_manage, get_case, insurer_role, is_owner, require_evidence_access, require_owner
from . import forms, services
from .models import Dependency, Permission, Relationship, Submission, ValidationRun


def access(request, case_id, staff=False, manager=False):
    case = get_case(request.user, case_id)
    require_evidence_access(request.user, case)
    if manager:
        require_decider(request.user, case)
    elif staff:
        require_reviewer(request.user, case)
    elif not insurer_role(request.user, case) and not is_owner(request.user, case):
        raise Http404
    return case


def edit(request, case, form, title, intro, save):
    if request.method == "POST" and form.is_valid():
        try:
            result = save(dict(form.cleaned_data))
            if isinstance(result, ValidationRun):
                return redirect("portfolio_validation_detail", case.pk, result.pk)
            if isinstance(result, Submission):
                return redirect("capacity_detail", case.pk, result.pk)
            return redirect("portfolio", case.pk)
        except ValidationError as error:
            form.add_error(None, "; ".join(error.messages))
    return render(request, "workspaces/form.html", {"case": case, "form": form, "title": title, "intro": intro, "button": "Save"})


@login_required
def overview(request, case_id):
    case = access(request, case_id)
    staff, owner = bool(insurer_role(request.user, case)), is_owner(request.user, case)
    permitted = services.permission_for(case)
    rows = services.outcomes(request.user, case.insurer) if staff else []
    totals = {}
    for row in rows:
        group = totals.setdefault(row.currency, {"periods": 0, "incidents": 0, "loss": 0})
        group["periods"] += 1
        group["incidents"] += row.incidents
        group["loss"] += row.incurred_loss
    runs = []
    if staff:
        for run in ValidationRun.objects.filter(insurer=case.insurer).order_by("-created_at")[:20]:
            try:
                runs.append(services.require_validation(request.user, run))
            except Http404:
                pass
    return render(request, "portfolio/overview.html", {"case": case, "staff": staff, "owner": owner, "manager": can_manage(request.user, case),
        "permission": permitted, "outcomes": [o for o in rows if o.case_id == case.pk], "totals": totals,
        "dependencies": [d for d in case.provider_dependencies.select_related("provider", "service").filter(retirement__isnull=True) if services.permission_active(d.permission)] if staff and permitted else [],
        "concentration": services.concentration(request.user, case.insurer) if staff else [],
        "eligible_count": len(services.eligible_cases(request.user, case.insurer)) if staff else 0,
        "runs": runs, "submissions": case.capacity_submissions.select_related("relationship").order_by("-created_at"),
        "relationships": Relationship.objects.filter(insurer=case.insurer).select_related("recipient") if can_manage(request.user, case) else []})


@login_required
def authorize(request, case_id):
    case = access(request, case_id)
    require_owner(request.user, case)
    return edit(request, case, forms.PermissionForm(request.POST or None), "Authorize portfolio research",
        "This permission is separate from case access and capacity-partner sharing. Revocation excludes pinned records from future portfolio views and exports; retained records remain audit history.",
        lambda data: services.authorize(request.user, case, **data))


@login_required
@require_POST
def revoke_permission(request, case_id, permission_id):
    case = access(request, case_id)
    services.revoke_permission(request.user, get_object_or_404(Permission, pk=permission_id, case=case))
    return redirect("portfolio", case.pk)


@login_required
def outcome_create(request, case_id):
    case = access(request, case_id, staff=True)
    return edit(request, case, forms.OutcomeForm(case, request.POST or None), "Record an observed outcome",
        "Link an actual completed observation period to an earlier decision and its exact evidence snapshot. Corrections append a replacement; developing losses are excluded from validation. A newly decided pilot case will not have eligible history yet.",
        lambda data: services.record_outcome(request.user, case, data))


@login_required
def dependency_create(request, case_id):
    case = access(request, case_id, staff=True)
    return edit(request, case, forms.DependencyForm(case, request.POST or None), "Record provider dependency",
        "Identify the provider and supporting source. Matching canonical keys group dependencies only within this insurer and your permitted cases.",
        lambda data: services.record_dependency(request.user, case, **data))


@login_required
def dependency_retire(request, case_id, dependency_id):
    case = access(request, case_id, staff=True)
    dependency = get_object_or_404(Dependency, pk=dependency_id, case=case)
    return edit(request, case, forms.RationaleForm(request.POST or None), "Retire provider dependency", "Record why this dependency no longer applies. History is retained.",
        lambda data: services.retire_dependency(request.user, dependency, **data))


@login_required
def validation_create(request, case_id):
    case = access(request, case_id, manager=True)
    return edit(request, case, forms.ValidationForm(request.POST or None), "Evaluate historical risk bands",
        "Research only: compare incident frequency and loss severity baselines using completed, mature periods in one currency. Later holdout applicants are excluded from training. Insufficient data stops fitting. Results never set premiums, approve cases or change priorities.",
        lambda data: services.validate_model(request.user, case, **data))


@login_required
def validation_detail(request, case_id, run_id, export=False):
    case = access(request, case_id, staff=True)
    run = get_object_or_404(ValidationRun, pk=run_id, insurer=case.insurer)
    services.require_validation(request.user, run)
    data = {"model_version": run.model_version, "cutoff": run.cutoff.isoformat(), "currency": run.currency,
        "dataset_sha256": run.dataset_sha256, "dataset": run.dataset, "metrics": run.metrics}
    if export:
        response = JsonResponse(data)
        response["Content-Disposition"] = 'attachment; filename="portfolio-research.json"'
        return response
    return render(request, "portfolio/validation.html", {"case": case, "run": run, "details": json.dumps(data, indent=2)})


@login_required
def relationship_create(request, case_id):
    case = access(request, case_id, manager=True)
    def save(data):
        data["recipient"] = data.pop("recipient_email")
        return services.create_relationship(request.user, case, **data)
    return edit(request, case, forms.RelationshipForm(request.POST or None), "Record commercial relationship",
        "Use an existing provisioned partner account and contract reference. Recording a relationship does not share a case. Each submission needs business approval and an underwriter release.", save)


@login_required
@require_POST
def relationship_revoke(request, case_id, relationship_id):
    case = access(request, case_id, manager=True)
    services.revoke_relationship(request.user, case, get_object_or_404(Relationship, pk=relationship_id, insurer=case.insurer))
    return redirect("portfolio", case.pk)


@login_required
def submission_create(request, case_id):
    case = access(request, case_id, manager=True)
    return edit(request, case, forms.SubmissionForm(case, request.POST or None), "Prepare capacity submission",
        "Prepare a fixed summary from an approved current report. The business owner previews and approves it, then an underwriter releases it to the named authenticated recipient. No email is sent.",
        lambda data: services.prepare_submission(request.user, case, **data))


@login_required
def submission_detail(request, case_id, submission_id):
    case = access(request, case_id)
    submission = get_object_or_404(Submission, pk=submission_id, case=case)
    return render(request, "portfolio/submission.html", {"case": case, "submission": submission,
        "owner": is_owner(request.user, case), "manager": can_manage(request.user, case), "payload": json.dumps(submission.payload, indent=2)})


@login_required
def submission_action(request, case_id, submission_id, action):
    case = access(request, case_id)
    submission = get_object_or_404(Submission, pk=submission_id, case=case)
    if action == "consent":
        require_owner(request.user, case)
        form = forms.ConsentForm(request.POST or None)
        save = lambda data: services.consent_submission(request.user, submission, **data)
        title = "Approve this submission for sharing"
    else:
        require_decider(request.user, case)
        form = forms.RationaleForm(request.POST or None)
        save = lambda data: services.release_submission(request.user, submission, **data)
        title = "Release this submission to the partner"
    if request.method == "POST" and form.is_valid():
        try:
            save(dict(form.cleaned_data))
            return redirect("capacity_detail", case.pk, submission.pk)
        except ValidationError as error:
            form.add_error(None, "; ".join(error.messages))
    return render(request, "portfolio/submission.html", {"case": case, "submission": submission, "title": title, "form": form, "payload": json.dumps(submission.payload, indent=2)})


@login_required
@require_POST
def submission_revoke(request, case_id, submission_id):
    case = access(request, case_id)
    services.revoke_submission(request.user, get_object_or_404(Submission, pk=submission_id, case=case))
    return redirect("capacity_detail", case.pk, submission_id)


@login_required
def partner_inbox(request):
    submissions = []
    for submission in Submission.objects.filter(relationship__recipient=request.user, release__isnull=False).order_by("-created_at"):
        try:
            submissions.append(services.require_partner(request.user, submission))
        except Http404:
            pass
    return render(request, "portfolio/inbox.html", {"submissions": submissions})


@login_required
def partner_detail(request, submission_id, export=False):
    submission = services.require_partner(request.user, get_object_or_404(Submission, pk=submission_id))
    if export:
        response = JsonResponse({"submission_id": str(submission.pk), "sha256": submission.sha256, "expires_at": submission.expires_at.isoformat(), "report": submission.payload})
        response["Content-Disposition"] = 'attachment; filename="capacity-submission.json"'
        return response
    form = forms.ResponseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.partner_response(request.user, submission, **form.cleaned_data)
            return redirect("capacity_partner", submission.pk)
        except ValidationError as error:
            form.add_error(None, "; ".join(error.messages))
    return render(request, "portfolio/submission.html", {"submission": submission, "partner": True, "form": form, "payload": json.dumps(submission.payload, indent=2)})
