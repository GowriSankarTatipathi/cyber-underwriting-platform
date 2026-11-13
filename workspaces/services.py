from datetime import timedelta
import hashlib
import secrets
import uuid
from allauth.account.models import EmailAddress
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from .models import AuditEvent, Case, EvidenceRequest, Invitation, Organization, SharingGrant
from .permissions import can_manage, is_owner, require_manager, require_owner


def audit(case, actor, action, detail=""):
    return AuditEvent.objects.create(case=case, actor=actor, action=action, detail=detail[:240])


def lock_case(case):
    """Call inside atomic transactions before evidence or workflow row locks."""
    return Case.objects.select_for_update().get(pk=case.pk)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def invitation_authorized(invitation):
    actor = invitation.invited_by
    case = invitation.case
    if not actor.is_active:
        return False
    if invitation.role == "owner":
        return can_manage(actor, case)
    return case.consent_active and is_owner(actor, case)


@transaction.atomic
def issue_invitation(case, actor, email, role, organization_name="", access_days=90):
    case = Case.objects.select_for_update().get(pk=case.pk)
    if role == "owner":
        require_manager(actor, case)
    elif role == "technician":
        require_owner(actor, case)
        if not case.consent_active:
            raise Http404
    else:
        raise Http404
    email = email.strip().lower()
    # A replacement invalidates outstanding links for the same recipient.
    Invitation.objects.filter(case=case, email=email, accepted_at__isnull=True, revoked_at__isnull=True).update(revoked_at=timezone.now())
    token = secrets.token_urlsafe(32)
    invitation = Invitation.objects.create(case=case, email=email, role=role, organization_name=organization_name, invited_by=actor, token_digest=digest(token), expires_at=timezone.now() + timedelta(days=7), access_expires_at=timezone.now() + timedelta(days=access_days))
    audit(case, actor, "Invitation created", f"{email} · {role}")
    return invitation, token


def find_invitation(token, lock=False):
    query = Invitation.objects.select_related("case", "case__applicant", "case__insurer", "invited_by")
    if lock:
        query = query.select_for_update(of=("self",))
    try:
        invitation = query.get(token_digest=digest(token))
    except Invitation.DoesNotExist:
        raise Http404
    if not invitation.usable or not invitation_authorized(invitation):
        raise Http404
    return invitation


@transaction.atomic
def accept_invitation(token, user):
    # Creation/revocation also lock the case first. Opposite ordering can deadlock
    # when an old invitation is accepted while its replacement is issued.
    invitation = find_invitation(token)
    case = Case.objects.select_for_update().get(pk=invitation.case_id)
    invitation = find_invitation(token, lock=True)
    user.refresh_from_db()
    if invitation.email.lower() != user.email.lower() or not user.is_active:
        raise Http404
    if not EmailAddress.objects.filter(user=user, email__iexact=invitation.email, verified=True).exists():
        raise Http404
    # Lock the case as well so consent changes cannot interleave acceptance.
    invitation.case = case
    if not invitation_authorized(invitation):
        raise Http404
    grant, _ = SharingGrant.objects.update_or_create(case=invitation.case, user=user, defaults={
        "role": invitation.role, "organization_name": invitation.organization_name,
        "granted_by": invitation.invited_by, "expires_at": invitation.access_expires_at, "revoked_at": None,
    })
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["accepted_at"])
    audit(invitation.case, user, "Invitation accepted", invitation.role)
    return grant


@transaction.atomic
def create_case(user, data):
    checklist = data["checklist"]
    applicant = Organization.objects.create(name=data["business_name"], kind="business")
    case = Case.objects.create(reference="CY-" + uuid.uuid4().hex[:10].upper(), insurer=checklist.insurer, applicant=applicant, checklist=checklist, kind=data["kind"], service=data["service"], due_date=data["due_date"], created_by=user, assignee=user)
    require_manager(user, case)
    from evidence.services import ensure_case_service
    ensure_case_service(case)
    for item in checklist.requirements:
        EvidenceRequest.objects.create(case=case, kind=item.get("kind", {"Supervised restore": "restore", "Backup coverage": "backup"}.get(item["name"], "control")), title=item["name"], description=item["evidence"], freshness_days=item["freshness_days"], due_date=case.due_date)
    audit(case, user, "Case created", f"Checklist {checklist.version}")
    invitation, token = issue_invitation(case, user, data["owner_email"], "owner", applicant.name)
    return case, invitation, token
