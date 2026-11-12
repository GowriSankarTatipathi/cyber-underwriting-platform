from django.http import Http404
from django.db.models import Q
from django.utils import timezone
from workspaces.models import User
from workspaces.permissions import active_grant, can_manage, insurer_role, is_owner, require_evidence_access, visible_requests


def require_decider(user, case):
    require_evidence_access(user, case)
    if not can_manage(user, case):
        raise Http404


def finding_owners(case, item):
    # Do not grant new case/request access as a side effect of assignment.
    staff = Q(membership__organization=case.insurer, membership__is_active=True) & (
        Q(membership__role="admin") | Q(membership__role__in=["underwriter", "reviewer"], pk=case.assignee_id)
        | Q(membership__role="underwriter", pk=case.created_by_id))
    grants = Q(sharinggrant__case=case, sharinggrant__revoked_at__isnull=True, sharinggrant__expires_at__gt=timezone.now())
    contributors = Q(sharinggrant__role="owner")
    if case.consent_active:
        contributors |= Q(sharinggrant__role="technician", pk=item.assigned_to_id)
    return User.objects.filter(staff | (grants & contributors), is_active=True).distinct()


def visible_findings(user, case):
    items = visible_requests(user, case)
    findings = case.findings.filter(request__in=items).select_related("request", "owner", "retest_of")
    if insurer_role(user, case) or is_owner(user, case):
        return findings
    return findings.filter(owner=user)


def require_responder(user, finding):
    if not visible_findings(user, finding.case).filter(pk=finding.pk).exists():
        raise Http404
    if finding.owner_id != user.pk and not insurer_role(user, finding.case):
        raise Http404


def require_shared_report(user, share):
    case = share.report.decision.case
    require_evidence_access(user, case)
    if share.recipient_id != user.pk or not share.active or not is_owner(user, case):
        raise Http404
    return case
