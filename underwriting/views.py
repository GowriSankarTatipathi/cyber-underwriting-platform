import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from evidence.permissions import require_reviewer
from workspaces.permissions import can_manage, get_case, insurer_role, require_evidence_access
from workspaces.services import audit
from workspaces.views import invitation_created
from .forms import DecisionForm, ExceptionForm, FindingForm, FindingReviewForm, RationaleForm, RenewalForm, ReportForm, ResponseForm, ShareForm
from .models import Decision, ExceptionApproval, Report, ReportShare, ReviewSnapshot
from .permissions import require_decider, require_responder, require_shared_report, visible_findings
from . import services


def form_page(request, case, form, title, intro, button, callback):
    if request.method == "POST" and form.is_valid():
        try:
            return callback(dict(form.cleaned_data))
        except ValidationError as error:
            form.add_error(None, error)
        except IntegrityError:
            form.add_error(None, "This action was already recorded or the record changed. Reload to review the latest state.")
    return render(request, "underwriting/form.html", {"case": case, "form": form, "title": title, "intro": intro, "button": button})


@login_required
def workflow(request, case_id):
    case = get_case(request.user, case_id)
    findings = visible_findings(request.user, case)
    if request.GET.get("state") in ("open", "submitted", "closed"):
        findings = findings.filter(state=request.GET["state"])
    staff = bool(insurer_role(request.user, case))
    shares = []
    for share in ReportShare.objects.filter(report__decision__case=case, recipient=request.user).select_related("report__decision__case"):
        try:
            require_shared_report(request.user, share)
            shares.append(share)
        except Http404:
            pass
    exceptions = ExceptionApproval.objects.filter(finding__in=visible_findings(request.user, case)).select_related("finding", "approved_by").order_by("-created_at")
    return render(request, "underwriting/workflow.html", {"case": case, "findings": findings, "staff": staff,
        "manager": can_manage(request.user, case), "decisions": case.decisions.all() if staff else [],
        "shares": shares, "exceptions": exceptions, "selected_state": request.GET.get("state", "")})


@login_required
def finding_edit(request, case_id, finding_id=None):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    finding = get_object_or_404(case.findings, pk=finding_id) if finding_id else None
    initial = {"request": request.GET.get("request"), "kind": "evidence", "priority": "medium"}
    form = FindingForm(case, request.POST or None, instance=finding, initial=initial if not finding else None)
    def save(data):
        revision = data.pop("revision", None)
        result = services.save_finding(request.user, case, data, finding, revision)
        messages.success(request, "Finding saved with its owner, deadline, and priority rationale.")
        return redirect("finding_detail", case.pk, result.pk)
    return form_page(request, case, form, "Update finding" if finding else "Create finding / follow-up request",
        "Describe the work and evidence the assignee should provide. Findings and review comments are visible to their authorized owner.", "Save finding", save)


@login_required
def finding_detail(request, case_id, finding_id):
    case = get_case(request.user, case_id)
    finding = get_object_or_404(visible_findings(request.user, case), pk=finding_id)
    staff = bool(insurer_role(request.user, case))
    return render(request, "underwriting/finding.html", {"case": case, "finding": finding, "staff": staff,
        "manager": can_manage(request.user, case), "responder": staff or finding.owner_id == request.user.pk,
        "responses": finding.responses.all(), "events": finding.events.select_related("actor"), "exceptions": finding.exceptions.all()})


@login_required
def finding_respond(request, case_id, finding_id):
    case = get_case(request.user, case_id)
    finding = get_object_or_404(visible_findings(request.user, case), pk=finding_id)
    require_responder(request.user, finding)
    def save(data):
        services.respond(request.user, finding, **data)
        messages.success(request, "Response sent for reviewer verification. The finding remains open until accepted.")
        return redirect("finding_detail", case.pk, finding.pk)
    return form_page(request, case, ResponseForm(finding, request.POST or None), "Respond to finding", finding.title, "Submit response", save)


@login_required
def finding_review(request, case_id, finding_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    finding = get_object_or_404(case.findings, pk=finding_id)
    def save(data):
        services.review_finding(request.user, finding, **data)
        return redirect("finding_detail", case.pk, finding.pk)
    return form_page(request, case, FindingReviewForm(request.POST or None, initial={"revision": finding.revision}),
        "Review finding response", finding.title + ". Review the response and its exact evidence before closing.", "Record review", save)


@login_required
def exception_create(request, case_id, finding_id):
    case = get_case(request.user, case_id)
    require_decider(request.user, case)
    finding = get_object_or_404(case.findings, pk=finding_id)
    def save(data):
        services.approve_exception(request.user, finding, **data)
        return redirect("finding_detail", case.pk, finding.pk)
    return form_page(request, case, ExceptionForm(request.POST or None), "Approve a time-limited exception",
        finding.title + ". An exception does not close the finding or turn failed evidence into a pass.", "Approve exception", save)


@login_required
def exception_revoke(request, case_id, exception_id):
    case = get_case(request.user, case_id)
    require_decider(request.user, case)
    exception = get_object_or_404(ExceptionApproval, pk=exception_id, finding__case=case)
    def save(data):
        services.revoke_exception(request.user, exception, data["rationale"])
        return redirect("finding_detail", case.pk, exception.finding_id)
    return form_page(request, case, RationaleForm(request.POST or None), "Revoke exception", exception.finding.title, "Revoke exception", save)


@login_required
@require_POST
def decision_prepare(request, case_id):
    case = get_case(request.user, case_id)
    snapshot = services.prepare_decision(request.user, case)
    return redirect("decision_review", case.pk, snapshot.pk)


@login_required
def decision_review(request, case_id, snapshot_id):
    case = get_case(request.user, case_id)
    require_decider(request.user, case)
    snapshot = get_object_or_404(ReviewSnapshot, pk=snapshot_id, case=case, created_by=request.user)
    form = DecisionForm(case, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            decision = services.record_decision(request.user, snapshot, **form.cleaned_data)
            messages.success(request, "Decision saved against the exact reviewed snapshot.")
            return redirect("decision_detail", case.pk, decision.pk)
        except ValidationError as error:
            form.add_error(None, error)
        except IntegrityError:
            form.add_error(None, "This decision was already recorded. Return to the case workflow to review it.")
    return render(request, "underwriting/decision.html", {"case": case, "snapshot": snapshot, "form": form,
        "current": services.snapshot_current(snapshot), "snapshot_json": json.dumps(snapshot.payload, indent=2)})


@login_required
def decision_detail(request, case_id, decision_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    decision = get_object_or_404(Decision, pk=decision_id, case=case)
    if request.GET.get("download") == "json":
        audit(case, request.user, "Decision snapshot exported", str(decision.pk))
        response = JsonResponse({"decision_id": str(decision.pk), "number": decision.number, "outcome": decision.outcome,
            "rationale": decision.rationale, "conditions": decision.conditions, "decided_at": decision.created_at,
            "decided_by": decision.decided_by.email, "rule_version": decision.rule_version,
            "snapshot_sha256": decision.snapshot.sha256, "snapshot": decision.snapshot.payload}, json_dumps_params={"indent": 2})
        response["Content-Disposition"] = f'attachment; filename="decision-{decision.pk}.json"'
        return response
    return render(request, "underwriting/decision.html", {"case": case, "decision": decision, "snapshot": decision.snapshot,
        "reports": decision.reports.order_by("-created_at"), "current": services.snapshot_current(decision.snapshot),
        "superseded": case.decisions.first().pk != decision.pk, "snapshot_json": json.dumps(decision.snapshot.payload, indent=2)})


@login_required
def report_create(request, case_id, decision_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    decision = get_object_or_404(Decision, pk=decision_id, case=case)
    def save(data):
        report = services.draft_report(request.user, decision, **data)
        return redirect("report_detail", case.pk, report.pk)
    return form_page(request, case, ReportForm(request.POST or None, initial={"title": f"Underwriting review · {case.reference}"}),
        "Draft a business report", "Write the summary intended for the business. Review the report before approving it for sharing.", "Create report draft", save)


def render_report(request, case, report, share=None):
    if request.GET.get("download") == "json":
        if not report.approved:
            raise Http404
        if share:
            require_shared_report(request.user, share)
        audit(case, request.user, "Approved report exported", str(report.pk))
        response = JsonResponse({"report_id": str(report.pk), "sha256": report.sha256,
            "approved_at": report.approval.created_at, "report": report.payload}, json_dumps_params={"indent": 2})
        response["Content-Disposition"] = f'attachment; filename="report-{report.pk}.json"'
        return response
    return render(request, "underwriting/report.html", {"case": case, "report": report, "shared": bool(share),
        "manager": not share and can_manage(request.user, case), "shares": report.shares.select_related("recipient") if not share else [],
        "superseded": case.decisions.first().pk != report.decision_id,
        "report_json": json.dumps(report.payload, indent=2)})


@login_required
def report_detail(request, case_id, report_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    report = get_object_or_404(Report, pk=report_id, decision__case=case)
    return render_report(request, case, report)


@login_required
def report_action(request, case_id, report_id, action):
    case = get_case(request.user, case_id)
    require_decider(request.user, case)
    report = get_object_or_404(Report, pk=report_id, decision__case=case)
    callback = {"approve": services.approve_report, "withdraw": services.withdraw_report}.get(action)
    if not callback:
        raise Http404
    def save(data):
        callback(request.user, report, data["rationale"])
        return redirect("report_detail", case.pk, report.pk)
    return form_page(request, case, RationaleForm(request.POST or None), f"{action.title()} report", report.title,
        f"{action.title()} report", save)


@login_required
def report_share(request, case_id, report_id):
    case = get_case(request.user, case_id)
    require_decider(request.user, case)
    report = get_object_or_404(Report, pk=report_id, decision__case=case)
    def save(data):
        share = services.share_report(request.user, report, **data)
        return render(request, "underwriting/shared_link.html", {"case": case, "share": share,
            "share_url": request.build_absolute_uri(reverse("shared_report", args=[share.pk]))})
    return form_page(request, case, ShareForm(case, request.POST or None), "Share approved report",
        "The named business owner must sign in with MFA and retain case access. Copy the link after saving; no message is sent automatically.", "Create secure share", save)


@login_required
def share_revoke(request, case_id, share_id):
    case = get_case(request.user, case_id)
    require_decider(request.user, case)
    share = get_object_or_404(ReportShare, pk=share_id, report__decision__case=case)
    def save(data):
        services.revoke_share(request.user, share, data["rationale"])
        return redirect("report_detail", case.pk, share.report_id)
    return form_page(request, case, RationaleForm(request.POST or None), "Revoke report sharing", share.recipient.email, "Revoke sharing", save)


@login_required
def shared_report(request, share_id):
    share = get_object_or_404(ReportShare.objects.select_related("report__decision__case"), pk=share_id)
    case = require_shared_report(request.user, share)
    audit(case, request.user, "Shared report opened", str(share.pk))
    return render_report(request, case, share.report, share)


@login_required
def renewal_create(request, case_id):
    case = get_case(request.user, case_id)
    require_decider(request.user, case)
    def save(data):
        renewal, invitation, token = services.create_renewal(request.user, case, **data)
        return invitation_created(request, renewal, invitation, token)
    return form_page(request, case, RenewalForm(case, request.POST or None, initial={"checklist": case.checklist}),
        "Open the next renewal", "Keep the applicant and history link. Collect fresh evidence and business authorization for the new case.", "Create renewal and invitation", save)


@login_required
def history(request, case_id):
    case = get_case(request.user, case_id)
    rows = []
    candidate = case
    seen = set()
    while candidate and candidate.pk not in seen:
        seen.add(candidate.pk)
        try:
            accessible = get_case(request.user, candidate.pk)
            require_evidence_access(request.user, accessible)
            if insurer_role(request.user, accessible):
                rows.append({"case": accessible, "decisions": accessible.decisions.all()})
        except Http404:
            pass
        candidate = candidate.renewal_of
    return render(request, "underwriting/history.html", {"case": case, "rows": rows})
