from datetime import timedelta
import re
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from evidence.permissions import require_reviewer
from evidence.services import check_date
from underwriting.permissions import require_decider
from underwriting.services import snapshot_current
from underwriting.snapshots import checksum, normalize
from workspaces.models import Case, Membership, Organization
from workspaces.permissions import accessible_cases, active_grant, get_case, insurer_role, is_owner, require_owner
from workspaces.services import audit
from .analytics import validate_dataset
from .models import Dependency, DependencyRetirement, Outcome, PartnerResponse, Permission, PermissionRevocation, Provider, Relationship, RelationshipRevocation, Submission, SubmissionConsent, SubmissionRelease, SubmissionRevocation, ValidationRun


def permission_active(permission):
    permission = Permission.objects.select_related("case", "owner").get(pk=permission.pk)
    return permission.expires_at > timezone.now() and not hasattr(permission, "revocation") and permission.case.consent_active and is_owner(permission.owner, permission.case)


def permission_for(case):
    return next((p for p in case.portfolio_permissions.order_by("-pk") if permission_active(p)), None)


def lock_case(user, case):
    case = Case.objects.select_for_update().get(pk=case.pk)
    user.refresh_from_db()
    get_case(user, case.pk)
    return case


@transaction.atomic
def authorize(user, case, expires_at, confirm):
    case = lock_case(user, case)
    require_owner(user, case)
    grant = active_grant(user, case)
    if not confirm or not case.consent_active or not timezone.now() < expires_at <= min(grant.expires_at, case.consent_expires_at):
        raise ValidationError("Confirm the permitted research purpose and an expiry within case authorization.")
    for old in case.portfolio_permissions.filter(revocation__isnull=True):
        PermissionRevocation.objects.create(permission=old, actor=user)
    row = Permission.objects.create(case=case, owner=user, expires_at=expires_at)
    audit(case, user, "Portfolio use authorized", str(row.pk))
    return row


@transaction.atomic
def revoke_permission(user, permission):
    case = lock_case(user, permission.case)
    require_owner(user, case)
    PermissionRevocation.objects.get_or_create(permission=permission, defaults={"actor": user})
    audit(case, user, "Portfolio use revoked", str(permission.pk))


def require_portfolio(user, case):
    require_reviewer(user, case)
    permission = permission_for(case)
    if not permission:
        raise ValidationError("The business owner must authorize internal portfolio use first.")
    return permission


@transaction.atomic
def record_outcome(user, case, data):
    case = lock_case(user, case)
    permission = require_portfolio(user, case)
    # Serialize overlapping-period checks across this insurer's renewal cases.
    Organization.objects.select_for_update().get(pk=case.insurer_id)
    decision, start, end = data["decision"], data["period_start"], data["period_end"]
    if decision.case_id != case.pk or decision.created_at.date() >= start or not start < end <= timezone.localdate():
        raise ValidationError("Use a completed observation period strictly after the linked underwriting decision. Future or retrospective decision features are not allowed.")
    loss, count = data["incurred_loss"], data["incidents"]
    if not loss.is_finite() or loss < 0 or count < 0 or (not count and loss != 0):
        raise ValidationError("Use nonnegative incident/loss values; a zero-incident period must have zero incident loss.")
    data = {**data, "currency": data["currency"].upper()}
    if not re.fullmatch(r"[A-Z]{3}", data["currency"]):
        raise ValidationError("Use a three-letter currency code; currencies are never converted or combined.")
    old = data.get("supersedes")
    if old:
        old = Outcome.objects.get(pk=old.pk)
        data["supersedes"] = old
    if old and (old.case_id != case.pk or hasattr(old, "replacement") or old.period_start != start or old.period_end != end):
        raise ValidationError("A correction must replace the latest record for exactly the same case and period.")
    overlaps = Outcome.objects.filter(case__applicant=case.applicant, case__insurer=case.insurer, replacement__isnull=True,
        period_start__lt=end, period_end__gt=start)
    if old:
        overlaps = overlaps.exclude(pk=old.pk)
    if overlaps.exists():
        raise ValidationError("This applicant already has an overlapping outcome period. Correct its record instead of double-counting exposure.")
    row = Outcome(case=case, permission=permission, recorded_by=user, **data)
    row.full_clean()
    row.save()
    audit(case, user, "Portfolio outcome recorded", str(row.pk))
    return row


@transaction.atomic
def record_dependency(user, case, service, provider_name, provider_key, critical, source, observed_at):
    case = lock_case(user, case)
    permission = require_portfolio(user, case)
    if service.case_id != case.pk:
        raise Http404
    check_date(observed_at)
    key = provider_key.strip().casefold()
    if not key or not provider_name.strip() or not source.strip():
        raise ValidationError("Record the provider identity, canonical key, and evidence source.")
    provider, _ = Provider.objects.get_or_create(insurer=case.insurer, canonical_key=key, defaults={"name": provider_name.strip(), "created_by": user})
    if case.provider_dependencies.filter(service=service, provider=provider, retirement__isnull=True).exists():
        raise ValidationError("An active dependency exists. Retire it before recording revised information.")
    row = Dependency.objects.create(case=case, service=service, provider=provider, permission=permission, critical=critical, source=source, observed_at=observed_at, recorded_by=user)
    audit(case, user, "Provider dependency recorded", str(row.pk))
    return row


@transaction.atomic
def retire_dependency(user, dependency, rationale):
    case = lock_case(user, dependency.case)
    require_reviewer(user, case)
    if not rationale.strip():
        raise ValidationError("Explain the dependency retirement.")
    row = DependencyRetirement.objects.get_or_create(dependency=dependency, defaults={"rationale": rationale, "actor": user})[0]
    audit(case, user, "Provider dependency retired", str(dependency.pk))
    return row


def eligible_cases(user, insurer):
    return [case for case in accessible_cases(user).filter(insurer=insurer) if insurer_role(user, case) and case.consent_active and permission_for(case)]


def outcomes(user, insurer):
    rows = Outcome.objects.filter(case__in=eligible_cases(user, insurer), replacement__isnull=True).select_related("case", "decision__snapshot", "permission")
    return [row for row in rows if permission_active(row.permission)]


def concentration(user, insurer):
    groups = {}
    for row in Dependency.objects.filter(case__in=eligible_cases(user, insurer), retirement__isnull=True).select_related("case", "service", "provider", "permission"):
        if not permission_active(row.permission):
            continue
        group = groups.setdefault(row.provider_id, {"name": row.provider.name, "key": row.provider.canonical_key, "cases": {}, "critical_cases": set(), "stale": 0})
        group["cases"][str(row.case_id)] = row.case.reference
        if row.critical:
            group["critical_cases"].add(str(row.case_id))
        group["stale"] += int(row.observed_at < timezone.now() - timedelta(days=90))
    return [{**g, "case_count": len(g["cases"]), "critical_count": len(g["critical_cases"]), "critical_cases": sorted(g["critical_cases"])} for g in groups.values()]


@transaction.atomic
def validate_model(user, case, cutoff, currency):
    case = lock_case(user, case)
    require_decider(user, case)
    currency = currency.upper()
    if not re.fullmatch(r"[A-Z]{3}", currency) or cutoff >= timezone.localdate():
        raise ValidationError("Choose a past holdout cutoff and a three-letter currency code.")
    rows = [row for row in outcomes(user, case.insurer) if row.maturity == "mature" and row.currency == currency]
    if len(rows) > 5000:
        raise ValidationError("This pilot validation tool supports up to 5,000 periods per run.")
    dataset = []
    for row in rows:
        findings = row.decision.snapshot.payload.get("findings", [])
        band = "higher" if any(f["state"] != "closed" and f["priority"] in ("high", "critical") for f in findings) else "lower"
        dataset.append({"id": str(row.pk), "case_id": str(row.case_id), "permission_id": row.permission_id, "applicant": str(row.case.applicant_id),
            "decision_id": str(row.decision_id), "snapshot_sha256": row.decision.snapshot.sha256, "band": band,
            "start": row.period_start.isoformat(), "end": row.period_end.isoformat(), "years": (row.period_end - row.period_start).days / 365.25,
            "incidents": row.incidents, "loss": float(row.incurred_loss), "currency": currency,
            "source": row.source, "recorded_at": row.created_at.isoformat(), "maturity": row.maturity})
    dataset.sort(key=lambda row: row["id"])
    run = ValidationRun.objects.create(insurer=case.insurer, cutoff=cutoff, currency=currency, dataset=dataset, dataset_sha256=checksum(dataset),
        metrics=validate_dataset(dataset, cutoff.isoformat()), created_by=user)
    audit(case, user, "Portfolio validation recorded", str(run.pk))
    return run


def require_validation(user, run):
    if not user.is_active or not Membership.objects.filter(user=user, organization=run.insurer, is_active=True, role__in=["admin", "underwriter", "reviewer"]).exists():
        raise Http404
    if checksum(run.dataset) != run.dataset_sha256:
        raise Http404
    allowed = {str(c.pk) for c in eligible_cases(user, run.insurer)}
    for row in run.dataset:
        if row["case_id"] not in allowed or not permission_active(Permission.objects.get(pk=row["permission_id"])):
            raise Http404
    return run


@transaction.atomic
def create_relationship(user, case, partner_name, recipient, contract_reference, expires_at, confirm):
    case = lock_case(user, case)
    require_decider(user, case)
    if not confirm or not recipient.is_active or expires_at <= timezone.now() or not contract_reference.strip():
        raise ValidationError("Confirm the existing commercial relationship, active named recipient and future expiry.")
    row = Relationship.objects.create(insurer=case.insurer, partner_name=partner_name, recipient=recipient,
        contract_reference=contract_reference, expires_at=expires_at, created_by=user)
    audit(case, user, "Capacity relationship recorded", str(row.pk))
    return row


def relationship_active(relationship):
    relationship.refresh_from_db()
    return relationship.expires_at > timezone.now() and not hasattr(relationship, "revocation") and relationship.recipient.is_active


def can_revoke_relationship(user, case, relationship):
    return relationship.insurer_id == case.insurer_id and (relationship.created_by_id == user.pk or insurer_role(user, case) == "admin")


@transaction.atomic
def revoke_relationship(user, case, relationship):
    case = lock_case(user, case)
    require_decider(user, case)
    if not can_revoke_relationship(user, case, relationship):
        raise Http404
    row = RelationshipRevocation.objects.get_or_create(relationship=relationship, defaults={"actor": user})[0]
    audit(case, user, "Capacity relationship revoked", str(relationship.pk))
    return row


@transaction.atomic
def prepare_submission(user, case, relationship, report, expires_at):
    case = lock_case(user, case)
    require_decider(user, case)
    report.refresh_from_db()
    if relationship.insurer_id != case.insurer_id or report.decision.case_id != case.pk:
        raise Http404
    if checksum(report.payload) != report.sha256:
        raise ValidationError("The report fingerprint does not match. Prepare and approve a new report.")
    if not relationship_active(relationship) or not report.approved or not timezone.now() < expires_at <= relationship.expires_at:
        raise ValidationError("Use an active commercial relationship, approved report and expiry within the relationship.")
    if case.decisions.first().pk != report.decision_id or not snapshot_current(report.decision.snapshot):
        raise ValidationError("Prepare an approved report from the latest current decision first.")
    # Further restrict the already-approved business report: no raw evidence, notes,
    # losses, account identifiers, private storage references or condition-owner emails.
    p = report.payload
    payload = {key: p[key] for key in ("case_reference", "applicant", "insurer", "product", "title", "summary", "outcome", "decision_number", "decided_at", "checklist_version", "snapshot_sha256")}
    payload.update(schema_version=1, report_id=str(report.pk), report_sha256=report.sha256)
    payload["controls"] = [{"title": c["title"], "status": c["status"]} for c in p["controls"]]
    payload["conditions"] = [{k: c[k] for k in ("title", "description", "due_date", "state")} for c in p["conditions"]]
    payload["exceptions"] = [{k: e[k] for k in ("finding", "expires_at", "compensating_controls")} for e in p["exceptions"]]
    payload["partner"] = relationship.partner_name
    row = Submission.objects.create(case=case, relationship=relationship, report=report, payload=normalize(payload), sha256=checksum(payload), expires_at=expires_at, created_by=user)
    audit(case, user, "Capacity submission prepared", str(row.pk))
    return row


def current_submission(submission):
    submission = Submission.objects.select_related("case", "relationship__recipient", "report__decision__snapshot").get(pk=submission.pk)
    if hasattr(submission, "revocation") or not submission.case.consent_active or submission.expires_at <= timezone.now() or not relationship_active(submission.relationship) or not submission.report.approved:
        raise Http404
    if checksum(submission.payload) != submission.sha256:
        raise Http404
    return submission


def submission_status(submission):
    """Human-readable status for already-authorized case viewers, never an access grant."""
    if hasattr(submission, "revocation"):
        return "Revoked", False
    if submission.expires_at <= timezone.now():
        return "Expired", False
    try:
        current_submission(submission)
    except Http404:
        return "Access paused — check case authorization, report and relationship", False
    if not consent_active(submission):
        if hasattr(submission, "consent"):
            return "Business approval no longer active — prepare a new submission", False
        return "Awaiting business approval", True
    return ("Released to partner" if hasattr(submission, "release") else "Awaiting underwriter release"), True


@transaction.atomic
def consent_submission(user, submission, confirm):
    case = lock_case(user, submission.case)
    require_owner(user, case)
    submission = current_submission(submission)
    if not confirm or submission.expires_at > active_grant(user, case).expires_at or submission.expires_at > case.consent_expires_at:
        raise ValidationError("Confirm this exact report, named recipient and expiry within your case authorization.")
    result = SubmissionConsent.objects.get_or_create(submission=submission, defaults={"owner": user, "payload_sha256": submission.sha256})[0]
    audit(case, user, "Capacity submission approved by business", str(submission.pk))
    return result


def consent_active(submission):
    consent = getattr(submission, "consent", None)
    return consent and consent.payload_sha256 == submission.sha256 and is_owner(consent.owner, submission.case)


@transaction.atomic
def release_submission(user, submission, rationale):
    case = lock_case(user, submission.case)
    require_decider(user, case)
    submission = current_submission(submission)
    if not rationale.strip() or not consent_active(submission):
        raise ValidationError("Business approval of this exact submission and underwriter release rationale are required.")
    if case.decisions.first().pk != submission.report.decision_id or not snapshot_current(submission.report.decision.snapshot):
        raise ValidationError("The reviewed decision changed. Prepare a new submission.")
    result = SubmissionRelease.objects.get_or_create(submission=submission, defaults={"actor": user, "rationale": rationale})[0]
    audit(case, user, "Capacity submission released", str(submission.pk))
    return result


def require_partner(user, submission):
    submission = current_submission(submission)
    if not user.is_active or submission.relationship.recipient_id != user.pk or not hasattr(submission, "release") or not consent_active(submission):
        raise Http404
    return submission


@transaction.atomic
def revoke_submission(user, submission):
    case = lock_case(user, submission.case)
    if not is_owner(user, case):
        require_decider(user, case)
    result = SubmissionRevocation.objects.get_or_create(submission=submission, defaults={"actor": user})[0]
    audit(case, user, "Capacity submission revoked", str(submission.pk))
    return result


@transaction.atomic
def partner_response(user, submission, status, message):
    Case.objects.select_for_update().get(pk=submission.case_id)
    user.refresh_from_db()
    submission = require_partner(user, submission)
    if status not in dict(PartnerResponse._meta.get_field("status").choices) or not message.strip():
        raise ValidationError("Select a response and add a message. This workflow does not bind coverage.")
    result = PartnerResponse.objects.create(submission=submission, status=status, message=message, actor=user)
    audit(submission.case, user, "Capacity partner responded", str(result.pk))
    return result
