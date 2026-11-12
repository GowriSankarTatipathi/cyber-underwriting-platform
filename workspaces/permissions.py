from django.db.models import Q
from django.http import Http404
from django.utils import timezone
from .models import Case, Membership, SharingGrant


def insurer_memberships(user):
    return Membership.objects.filter(user=user, is_active=True, organization__kind="insurer")


def insurer_role(user, case):
    role = insurer_memberships(user).filter(organization=case.insurer).values_list("role", flat=True).first()
    if role == "admin" or (role in ("underwriter", "reviewer") and case.assignee_id == user.pk) or (role == "underwriter" and case.created_by_id == user.pk):
        return role
    return None


def active_grant(user, case):
    if not user.is_active:
        return None
    return SharingGrant.objects.filter(case=case, user=user, revoked_at__isnull=True, expires_at__gt=timezone.now()).first()


def accessible_cases(user):
    if not user.is_authenticated or not user.is_active:
        return Case.objects.none()
    memberships = insurer_memberships(user)
    admin_orgs = memberships.filter(role="admin").values("organization_id")
    staff_orgs = memberships.filter(role__in=["underwriter", "reviewer"]).values("organization_id")
    underwriter_orgs = memberships.filter(role="underwriter").values("organization_id")
    grants = SharingGrant.objects.filter(user=user, revoked_at__isnull=True, expires_at__gt=timezone.now())
    owner_cases = grants.filter(role="owner").values("case_id")
    technician_cases = grants.filter(role="technician", case__consent_revoked_at__isnull=True, case__consent_expires_at__gt=timezone.now(), case__consent_granted_by__isnull=False).values("case_id")
    return Case.objects.filter(
        Q(insurer_id__in=admin_orgs)
        | Q(insurer_id__in=staff_orgs, assignee=user)
        | Q(insurer_id__in=underwriter_orgs, created_by=user)
        | Q(pk__in=owner_cases) | Q(pk__in=technician_cases)
    ).distinct()


def get_case(user, case_id):
    try:
        return accessible_cases(user).select_related("insurer", "applicant", "checklist", "assignee").get(pk=case_id)
    except Case.DoesNotExist:
        raise Http404


def can_manage(user, case):
    return insurer_role(user, case) in ("admin", "underwriter") and accessible_cases(user).filter(pk=case.pk).exists()


def is_owner(user, case):
    grant = active_grant(user, case)
    return bool(grant and grant.role == "owner")


def require_manager(user, case):
    if not can_manage(user, case):
        raise Http404


def require_owner(user, case):
    if not is_owner(user, case):
        raise Http404


def require_evidence_access(user, case):
    if not accessible_cases(user).filter(pk=case.pk).exists():
        raise Http404
    if not is_owner(user, case) and not case.consent_active:
        raise Http404


def visible_requests(user, case):
    require_evidence_access(user, case)
    requests = case.requests.select_related("assigned_to")
    if insurer_role(user, case) or is_owner(user, case):
        return requests
    return requests.filter(assigned_to=user)


def require_job_access(user, job):
    case = get_case(user, job.case_id)
    require_evidence_access(user, case)
    if user.pk != job.requested_by_id and not can_manage(user, case):
        raise Http404
    return case


def authorize_job_execution(job):
    """Recheck at execution and again before publication/external processing."""
    job.refresh_from_db()
    job.requested_by.refresh_from_db()
    if job.grant_id:
        job.grant.refresh_from_db()
        if not job.grant.active:
            raise Http404
    return require_job_access(job.requested_by, job)
