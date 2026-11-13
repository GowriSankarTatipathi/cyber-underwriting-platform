from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import AssignmentForm, CaseForm, ConsentForm, InvitationSignupForm, InviteForm, NotesForm, RequestAssignmentForm
from .models import Case, CaseFile, CaseJob, Invitation, SharingGrant, User
from .permissions import active_grant, accessible_cases, can_manage, get_case, insurer_memberships, insurer_role, is_owner, require_evidence_access, require_job_access, require_manager, require_owner, visible_requests
from .services import accept_invitation, audit, create_case, find_invitation, issue_invitation, lock_case


@login_required
def home(request):
    destination = request.session.pop("workspace_return_to", "")
    if destination.startswith("/invitations/"):
        return redirect(destination)
    if insurer_memberships(request.user).exists():
        return redirect("underwriter")
    if request.user.capacity_relationships.exists() and not accessible_cases(request.user).exists():
        return redirect("capacity_inbox")
    return redirect("portal")


@login_required
def walkthrough(request):
    if not settings.DEBUG:
        raise Http404
    from .demo_data import DEMO_INSURER, SCENARIOS
    cases = {case.reference: case for case in accessible_cases(request.user).filter(insurer__name=DEMO_INSURER)}
    scenarios = [{"case": cases[reference], "description": description} for reference, _, description in SCENARIOS if reference in cases]
    return render(request, "workspaces/walkthrough.html", {"scenarios": scenarios})


@login_required
def dashboard(request, workspace):
    cases = accessible_cases(request.user).select_related("applicant", "insurer", "assignee", "checklist")
    if workspace == "underwriter":
        memberships = insurer_memberships(request.user)
        if not memberships.exists():
            raise Http404
        cases = cases.filter(insurer_id__in=memberships.values("organization_id"))
        can_create = memberships.filter(role__in=["admin", "underwriter"]).exists()
    else:
        cases = cases.filter(grants__user=request.user, grants__revoked_at__isnull=True, grants__expires_at__gt=timezone.now()).distinct()
        can_create = False
    query = request.GET.get("q", "").strip()[:160]
    if query:
        cases = cases.filter(applicant__name__icontains=query)
    cases = list(cases.order_by("-created_at"))
    for case in cases:
        latest = case.decisions.first() if case.consent_active and insurer_role(request.user, case) else None
        case.workspace_status = latest.get_outcome_display() if latest else case.status_label
    return render(request, "workspaces/dashboard.html", {"cases": cases, "workspace": workspace, "can_create": can_create, "query": query})


@login_required
def case_create(request):
    if not insurer_memberships(request.user).filter(role__in=["admin", "underwriter"]).exists():
        raise Http404
    form = CaseForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        case, invitation, token = create_case(request.user, form.cleaned_data)
        return invitation_created(request, case, invitation, token)
    return render(request, "workspaces/form.html", {"form": form, "title": "Open an underwriting case", "intro": "Choose the insurer's checklist and invite the business owner to authorize access.", "button": "Create case and invitation"})


def invitation_created(request, case, invitation, token):
    url = request.build_absolute_uri(reverse("invitation", args=[token]))
    return render(request, "workspaces/invitation_created.html", {"case": case, "invitation": invitation, "invitation_url": url})


@login_required
def case_detail(request, case_id):
    case = get_case(request.user, case_id)
    role = insurer_role(request.user, case)
    owner = is_owner(request.user, case)
    manager = can_manage(request.user, case)
    evidence_access = owner or case.consent_active
    requests = list(visible_requests(request.user, case).prefetch_related("files")) if evidence_access else []
    from evidence.rules import control_summary
    for item in requests:
        item.evidence_summary = control_summary(item)
    grants = case.grants.select_related("user") if owner or manager else []
    invitations = case.invitations.order_by("-created_at") if owner or manager else []
    return render(request, "workspaces/case.html", {
        "case": case, "insurer_role": role, "owner": owner, "manager": manager,
        "latest_decision": case.decisions.first() if role and case.consent_active else None,
        "requests": requests, "grants": grants, "invitations": invitations,
        "evidence_access": evidence_access, "can_inventory": evidence_access and (owner or role or bool(requests)), "consent_form": ConsentForm(),
        "assignment_form": AssignmentForm(case, initial={"assignee": case.assignee}),
        "notes_form": NotesForm(instance=case),
        "events": case.audit_events.select_related("actor")[:20] if role else [],
    })


@login_required
@require_POST
@transaction.atomic
def assign_case(request, case_id):
    case = lock_case(get_case(request.user, case_id))
    require_manager(request.user, case)
    form = AssignmentForm(case, request.POST)
    if form.is_valid():
        Case.objects.filter(pk=case.pk).update(assignee=form.cleaned_data["assignee"])
        audit(case, request.user, "Reviewer assigned", form.cleaned_data["assignee"].email)
        messages.success(request, "Case assignment saved.")
    else:
        messages.error(request, "Choose an active reviewer from this insurer.")
    return redirect("case", case.pk)


@login_required
@require_POST
@transaction.atomic
def save_notes(request, case_id):
    case = lock_case(get_case(request.user, case_id))
    require_manager(request.user, case)
    form = NotesForm(request.POST, instance=case)
    if form.is_valid():
        # Only update the intended column; do not overwrite concurrent consent.
        Case.objects.filter(pk=case.pk).update(internal_notes=form.cleaned_data["internal_notes"])
        audit(case, request.user, "Internal notes updated")
        messages.success(request, "Internal notes saved.")
    return redirect("case", case.pk)


@login_required
def invite(request, case_id):
    case = get_case(request.user, case_id)
    if is_owner(request.user, case):
        if not case.consent_active:
            raise Http404
        role = "technician"
    else:
        require_manager(request.user, case)
        role = "owner"
    form = InviteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        invitation, token = issue_invitation(case, request.user, role=role, **form.cleaned_data)
        return invitation_created(request, case, invitation, token)
    return render(request, "workspaces/form.html", {"form": form, "case": case, "title": "Invite an IT provider" if role == "technician" else "Invite a business owner", "intro": "This invitation grants access to this case only. The invitation link expires in seven days.", "button": "Create invitation"})


def invitation(request, token):
    invitation = find_invitation(token)
    existing = User.objects.filter(email__iexact=invitation.email).exists()
    signup = InvitationSignupForm(invitation, request.POST or None) if not existing else None
    if request.method == "POST":
        if request.user.is_authenticated:
            grant = accept_invitation(token, request.user)
            messages.success(request, "Invitation accepted. Review this case's authorization and request list.")
            return redirect("case", grant.case_id)
        if signup and signup.is_valid():
            try:
                with transaction.atomic():
                    invitation = find_invitation(token, lock=True)
                    user = signup.save()
                    # Possession of the addressed invitation establishes the email.
                    EmailAddress.objects.create(user=user, email=invitation.email, verified=True, primary=True)
            except IntegrityError:
                messages.error(request, "An account already exists. Sign in to accept this invitation.")
                return redirect(reverse("account_login") + "?" + urlencode({"next": request.path}))
            messages.success(request, "Account created. Sign in and set up your authenticator to accept the invitation.")
            return redirect(reverse("account_login") + "?" + urlencode({"next": request.path}))
    return render(request, "workspaces/invitation.html", {"invitation": invitation, "signup_form": signup, "existing": existing, "matches": request.user.is_authenticated and request.user.email.lower() == invitation.email.lower(), "login_url": reverse("account_login") + "?" + urlencode({"next": request.path})})


@login_required
@require_POST
@transaction.atomic
def consent(request, case_id):
    case = lock_case(get_case(request.user, case_id))
    request.user.refresh_from_db()
    require_owner(request.user, case)
    if request.POST.get("action") == "revoke":
        case.consent_revoked_at = timezone.now()
        case.save(update_fields=["consent_revoked_at"])
        audit(case, request.user, "Insurer access revoked")
        messages.success(request, "Insurer evidence access and provider access have been revoked.")
    else:
        form = ConsentForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Confirm authorization and choose an expiry between 1 and 365 days.")
            return redirect("case", case.pk)
        case.consent_granted_by = request.user
        case.consent_revoked_at = None
        case.consent_expires_at = min(timezone.now() + timedelta(days=form.cleaned_data["days"]), active_grant(request.user, case).expires_at)
        case.save(update_fields=["consent_granted_by", "consent_revoked_at", "consent_expires_at"])
        audit(case, request.user, "Insurer access authorized", str(case.consent_expires_at.date()))
        messages.success(request, "Case access authorized. You can now invite your IT provider.")
    return redirect("case", case.pk)


@login_required
@require_POST
@transaction.atomic
def revoke_grant(request, case_id, grant_id):
    case = lock_case(get_case(request.user, case_id))
    request.user.refresh_from_db()
    require_owner(request.user, case)
    grant = get_object_or_404(SharingGrant, pk=grant_id, case=case, role="technician")
    grant.revoked_at = timezone.now()
    grant.save(update_fields=["revoked_at"])
    audit(case, request.user, "Provider access revoked", grant.user.email)
    messages.success(request, "Provider access revoked.")
    return redirect("case", case.pk)


@login_required
@require_POST
@transaction.atomic
def revoke_invitation(request, case_id, invitation_id):
    case = lock_case(get_case(request.user, case_id))
    request.user.refresh_from_db()
    invitation = get_object_or_404(Invitation, pk=invitation_id, case=case, accepted_at__isnull=True)
    if invitation.role == "owner":
        require_manager(request.user, case)
    else:
        require_owner(request.user, case)
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=["revoked_at"])
    audit(case, request.user, "Invitation revoked", invitation.email)
    return redirect("case", case.pk)


@login_required
def assign_request(request, case_id, request_id):
    case = get_case(request.user, case_id)
    if not can_manage(request.user, case):
        require_owner(request.user, case)
    require_evidence_access(request.user, case)
    item = get_object_or_404(case.requests, pk=request_id)
    form = RequestAssignmentForm(case, request.POST or None, initial={"assigned_to": item.assigned_to})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            case = lock_case(case)
            request.user.refresh_from_db()
            if not can_manage(request.user, case):
                require_owner(request.user, case)
            require_evidence_access(request.user, case)
            # Rebuild choices after acquiring the case lock: a grant may have
            # expired or been revoked since the first form validation.
            form = RequestAssignmentForm(case, request.POST)
            if not form.is_valid():
                messages.error(request, "That provider no longer has active case access. Choose an active provider.")
                return redirect("assign_request", case.pk, item.pk)
            item.assigned_to = form.cleaned_data["assigned_to"]
            item.save(update_fields=["assigned_to"])
            audit(case, request.user, "Evidence request assigned", item.title)
        messages.success(request, "Request assignment saved.")
        return redirect("case", case.pk)
    return render(request, "workspaces/form.html", {"title": "Assign evidence request", "intro": item.title, "form": form, "case": case, "button": "Save assignment"})


@login_required
def download_file(request, case_id, file_id):
    case = get_case(request.user, case_id)
    item = get_object_or_404(CaseFile, pk=file_id, request__in=visible_requests(request.user, case))
    root = Path(settings.PRIVATE_FILE_ROOT).resolve()
    path = (root / item.storage_key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise Http404
    audit(case, request.user, "Sample file downloaded", str(item.pk))
    return FileResponse(path.open("rb"), as_attachment=True, filename=item.name, content_type="text/plain")


@login_required
def export_case(request, case_id):
    case = get_case(request.user, case_id)
    items = visible_requests(request.user, case)
    from evidence.exports import request_payload
    payload = {
        "schema_version": 2, "reference": case.reference, "applicant": case.applicant.name,
        "case_type": case.kind, "service": case.service, "checklist_version": case.checklist.version,
        "recovery_targets": case.checklist.recovery_targets,
        "requests": [request_payload(item) for item in items],
    }
    if insurer_role(request.user, case):
        payload["internal_notes"] = case.internal_notes
    audit(case, request.user, "Case request list exported")
    response = JsonResponse(payload, json_dumps_params={"indent": 2})
    response["Content-Disposition"] = f'attachment; filename="{case.reference}-requests.json"'
    return response


@login_required
def job_detail(request, case_id, job_id):
    job = get_object_or_404(CaseJob, pk=job_id, case_id=case_id)
    require_job_access(request.user, job)
    return JsonResponse({"id": str(job.pk), "status": job.status, "created_at": job.created_at})
