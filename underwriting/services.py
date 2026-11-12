import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from evidence.permissions import require_reviewer
from evidence.rules import current_restore_status, usable_version
from evidence.services import check_versions, ensure_case_service
from workspaces.models import Case, EvidenceRequest
from workspaces.permissions import get_case
from workspaces.services import audit, issue_invitation
from .models import Decision, ExceptionApproval, ExceptionRevocation, Finding, FindingEvent, FindingResponse, Report, ReportApproval, ReportShare, ReportWithdrawal, ReviewSnapshot, ShareRevocation
from .permissions import finding_owners, require_decider, require_responder
from .snapshots import case_snapshot, checksum, finding_data, normalize


def locked_case(user, case):
    case = Case.objects.select_for_update().get(pk=case.pk)
    user.refresh_from_db()
    get_case(user, case.pk)
    return case


def event(finding, actor, action, rationale):
    FindingEvent.objects.create(finding=finding, actor=actor, action=action, rationale=rationale, snapshot=finding_data(finding))
    audit(finding.case, actor, action, str(finding.pk))


@transaction.atomic
def save_finding(user, case, data, finding=None, revision=None):
    case = locked_case(user, case)
    require_reviewer(user, case)
    item = data["request"]
    if item.case_id != case.pk or not finding_owners(case, item).filter(pk=data["owner"].pk).exists():
        raise ValidationError("Assign an owner who can currently access this evidence request.")
    if data["due_date"] < timezone.localdate() and (not finding or data["due_date"] != finding.due_date):
        raise ValidationError("Choose today or a future deadline.")
    parent = data.get("retest_of")
    if data["kind"] == "retest":
        if not parent or parent.request_id != item.pk or item.kind != "restore":
            raise ValidationError("Select the original restore test for this request.")
    elif parent:
        raise ValidationError("Only a retest request can reference an original restore.")
    if finding:
        finding = Finding.objects.select_for_update().get(pk=finding.pk, case=case)
        if finding.revision != revision:
            raise ValidationError("This finding changed. Reload before editing it.")
        if finding.state == "closed":
            raise ValidationError("Reopen this finding before editing it.")
        if any(getattr(finding, name) != data.get(name) for name in ("request", "kind", "retest_of")):
            raise ValidationError("The evidence request and retest link are fixed. Create a separate finding for a different scope.")
        finding.revision += 1
        for key, value in data.items():
            setattr(finding, key, value)
        action = "Finding updated"
    else:
        finding = Finding(case=case, created_by=user, **data)
        action = "Finding created"
    finding.full_clean()
    finding.save()
    event(finding, user, action, finding.priority_reason)
    return finding


@transaction.atomic
def respond(user, finding, message, evidence_versions, restore_test=None):
    case = locked_case(user, finding.case)
    finding = Finding.objects.get(pk=finding.pk, case=case)
    require_responder(user, finding)
    if finding.state == "closed":
        raise ValidationError("This finding is closed. A reviewer must reopen it before another response.")
    versions = list(evidence_versions)
    if versions:
        check_versions(versions, finding.request)
    if not message.strip():
        raise ValidationError("Describe the response or fix.")
    if restore_test:
        if finding.kind != "retest" or restore_test.request_id != finding.request_id or restore_test.retest_of_id != finding.retest_of_id:
            raise ValidationError("Select a retest linked to this finding's original test.")
        if not set(restore_test.evidence_versions.values_list("pk", flat=True)).issubset({v.pk for v in versions}):
            raise ValidationError("Cite the evidence versions used by the retest.")
    response = FindingResponse.objects.create(finding=finding, message=message, restore_test=restore_test, submitted_by=user)
    response.evidence_versions.set(versions)
    finding.state = "submitted"
    finding.revision += 1
    finding.save(update_fields=["state", "revision"])
    event(finding, user, "Response submitted", message)
    return response


@transaction.atomic
def review_finding(user, finding, action, rationale, revision):
    case = locked_case(user, finding.case)
    require_reviewer(user, case)
    finding = Finding.objects.get(pk=finding.pk, case=case)
    if revision != finding.revision:
        raise ValidationError("A new response or edit arrived. Reload before reviewing.")
    if not rationale.strip() or action not in ("close", "reopen", "request_changes"):
        raise ValidationError("Choose a review action and explain it.")
    if action == "close":
        response = finding.responses.first()
        if finding.state != "submitted" or not response:
            raise ValidationError("Review a submitted response before closing the finding.")
        versions = list(response.evidence_versions.all())
        if not versions or any(not usable_version(v, finding.request.freshness_days) or v.document.latest_version.pk != v.pk for v in versions):
            raise ValidationError("Closing requires current, accepted, fresh evidence with confirmed scope.")
        if finding.kind == "retest":
            test = response.restore_test
            latest = finding.request.restore_tests.filter(service=finding.retest_of.service).first()
            if not test or test.retest_of_id != finding.retest_of_id or not latest or latest.pk != test.pk or current_restore_status(test) != "Passed":
                raise ValidationError("The latest linked retest must pass reviewer verification against current targets.")
        finding.state = "closed"
    else:
        if action == "reopen" and finding.state != "closed":
            raise ValidationError("Only a closed finding can be reopened.")
        if action == "request_changes" and finding.state != "submitted":
            raise ValidationError("A submitted response is required before requesting changes.")
        finding.state = "open"
    finding.revision += 1
    finding.save(update_fields=["state", "revision"])
    event(finding, user, {"close": "Finding closed", "reopen": "Finding reopened", "request_changes": "Changes requested"}[action], rationale)


@transaction.atomic
def approve_exception(user, finding, rationale, compensating_controls, expires_at):
    case = locked_case(user, finding.case)
    require_decider(user, case)
    finding.refresh_from_db()
    if finding.state == "closed" or expires_at <= timezone.now():
        raise ValidationError("An exception needs an open finding and a future expiry.")
    if any(row.active for row in finding.exceptions.all()):
        raise ValidationError("Revoke the current exception before approving a replacement.")
    exception = ExceptionApproval(finding=finding, rationale=rationale, compensating_controls=compensating_controls, expires_at=expires_at, approved_by=user)
    exception.full_clean()
    exception.save()
    event(finding, user, "Exception approved", rationale)
    return exception


@transaction.atomic
def revoke_exception(user, exception, rationale):
    case = locked_case(user, exception.finding.case)
    require_decider(user, case)
    if not rationale.strip():
        raise ValidationError("Give a reason for revocation.")
    revocation, _ = ExceptionRevocation.objects.get_or_create(exception=exception, defaults={"actor": user, "rationale": rationale})
    audit(case, user, "Exception revoked", str(exception.pk))
    return revocation


@transaction.atomic
def prepare_decision(user, case):
    case = locked_case(user, case)
    require_decider(user, case)
    payload = case_snapshot(case)
    latest = case.decisions.first()
    return ReviewSnapshot.objects.create(case=case, payload=payload, sha256=checksum(payload), created_by=user, base_decision_number=latest.number if latest else 0)


def snapshot_current(snapshot):
    return checksum(case_snapshot(Case.objects.get(pk=snapshot.case_id))) == snapshot.sha256


@transaction.atomic
def record_decision(user, snapshot, outcome, rationale, conditions, confirm=False):
    case = locked_case(user, snapshot.case)
    require_decider(user, case)
    if snapshot.created_by_id != user.pk:
        raise Http404
    if hasattr(snapshot, "decision"):
        raise ValidationError("A decision already uses this review. Prepare a new review for another decision.")
    latest = case.decisions.first()
    if (latest.number if latest else 0) != snapshot.base_decision_number:
        raise ValidationError("Another decision was recorded. Prepare a new review before superseding it.")
    if not snapshot_current(snapshot):
        raise ValidationError("Evidence, rules, findings, or exception status changed. Prepare and review a new snapshot.")
    if not confirm or not rationale.strip():
        raise ValidationError("Confirm the reviewed snapshot and provide a decision rationale.")
    conditions = list(conditions)
    ids = {str(row.pk) for row in conditions}
    open_rows = [row for row in snapshot.payload["findings"] if row["state"] != "closed"]
    if not ids.issubset({row["id"] for row in open_rows}):
        raise ValidationError("Conditions must reference open findings in this case.")
    gaps = [row for row in snapshot.payload["requests"] if row["control_status"] != "Verified"]
    if outcome == "approved" and (gaps or open_rows or conditions or not snapshot.payload["requests"]):
        raise ValidationError("Approval without conditions requires verified controls and no open findings. Use a conditional decision, deferral, or decline.")
    if outcome == "conditional":
        if not conditions:
            raise ValidationError("Select at least one open condition with an owner and deadline.")
        if any(not finding_owners(case, row.request).filter(pk=row.owner_id).exists() or row.due_date < timezone.localdate() for row in conditions):
            raise ValidationError("Every condition needs an owner with current access and a deadline today or later.")
        exceptions = {row["id"] for row in open_rows if any(e["active"] for e in row["exceptions"])}
        if any(row["id"] not in ids | exceptions for row in open_rows):
            raise ValidationError("Every open finding needs an explicit condition or active exception.")
        covered = {row["request_id"] for row in open_rows if row["id"] in ids | exceptions}
        if any(row["id"] not in covered for row in gaps):
            raise ValidationError("Create an owned finding and condition or exception for every unresolved control.")
    elif conditions:
        raise ValidationError("Open conditions belong to an approval with conditions.")
    decision = Decision(case=case, snapshot=snapshot, number=latest.number + 1 if latest else 1,
        outcome=outcome, rationale=rationale, conditions=[row for row in open_rows if row["id"] in ids], decided_by=user)
    decision.full_clean()
    decision.save()
    audit(case, user, "Underwriting decision recorded", f"Decision {decision.number} · {outcome}")
    return decision


@transaction.atomic
def draft_report(user, decision, title, summary):
    case = locked_case(user, decision.case)
    require_reviewer(user, case)
    source = decision.snapshot.payload
    # Explicit allowlist: insurer notes, private decision rationale, review comments,
    # object keys, and raw files are never included in a business report.
    payload = normalize({"schema_version": 1, "case_reference": case.reference, "applicant": source["case"]["applicant"],
        "insurer": source["case"]["insurer"], "product": source["checklist"]["product"],
        "title": title, "summary": summary, "decision_id": decision.pk, "decision_number": decision.number,
        "outcome": decision.get_outcome_display(), "decided_at": decision.created_at,
        "snapshot_sha256": decision.snapshot.sha256, "checklist_version": source["checklist"]["version"],
        "rule_versions": source["rule_versions"],
        "controls": [{"title": item["title"], "status": item["control_status"], "freshness_days": item["freshness_days"],
            "evidence": [{"id": v["id"], "number": v["number"], "sha256": v["sha256"], "observed_at": v["observed_at"]} for v in item["versions"]],
            "restore_tests": item["restore_tests"]} for item in source["requests"]],
        "conditions": [{key: row[key] for key in ("id", "title", "description", "owner", "due_date", "state")} for row in decision.conditions],
        "exceptions": [{"finding": row["title"], "expires_at": e["expires_at"], "compensating_controls": e["compensating_controls"]}
            for row in source["findings"] for e in row["exceptions"] if e["active"]]})
    report = Report(decision=decision, title=title, summary=summary, payload=payload, sha256=checksum(payload), created_by=user)
    report.full_clean()
    report.save()
    audit(case, user, "Report drafted", str(report.pk))
    return report


@transaction.atomic
def approve_report(user, report, rationale):
    case = locked_case(user, report.decision.case)
    require_decider(user, case)
    report.refresh_from_db()
    if not rationale.strip() or hasattr(report, "withdrawal"):
        raise ValidationError("Provide approval rationale for a report that has not been withdrawn.")
    if case.decisions.first().pk != report.decision_id or not snapshot_current(report.decision.snapshot):
        raise ValidationError("This decision has been superseded or its evidence changed. Record an updated decision before approving a report.")
    approval, _ = ReportApproval.objects.get_or_create(report=report, defaults={"rationale": rationale, "actor": user})
    audit(case, user, "Report approved", str(report.pk))
    return approval


@transaction.atomic
def withdraw_report(user, report, rationale):
    case = locked_case(user, report.decision.case)
    require_decider(user, case)
    report.refresh_from_db()
    if not rationale.strip():
        raise ValidationError("Explain why the report is withdrawn.")
    row, _ = ReportWithdrawal.objects.get_or_create(report=report, defaults={"rationale": rationale, "actor": user})
    audit(case, user, "Report withdrawn", str(report.pk))
    return row


@transaction.atomic
def share_report(user, report, recipient, expires_at):
    from workspaces.permissions import active_grant
    case = locked_case(user, report.decision.case)
    require_decider(user, case)
    report.refresh_from_db()
    grant = active_grant(recipient, case)
    if not report.approved or not grant or grant.role != "owner":
        raise ValidationError("Share an approved report with a currently authorized business owner.")
    if expires_at <= timezone.now() or expires_at > grant.expires_at:
        raise ValidationError("Sharing must expire in the future and no later than the recipient's case access.")
    share = ReportShare.objects.create(report=report, recipient=recipient, expires_at=expires_at, created_by=user)
    audit(case, user, "Report shared", str(share.pk))
    return share


@transaction.atomic
def revoke_share(user, share, rationale):
    case = locked_case(user, share.report.decision.case)
    require_decider(user, case)
    if not rationale.strip():
        raise ValidationError("Explain why sharing is revoked.")
    row, _ = ShareRevocation.objects.get_or_create(share=share, defaults={"rationale": rationale, "actor": user})
    audit(case, user, "Report sharing revoked", str(share.pk))
    return row


@transaction.atomic
def create_renewal(user, previous, checklist, due_date, owner_email):
    previous = locked_case(user, previous)
    require_decider(user, previous)
    if not previous.decisions.exists():
        raise ValidationError("Record the prior case's decision before opening its renewal.")
    if hasattr(previous, "renewal_case"):
        raise ValidationError("A renewal already exists for this case.")
    if checklist.insurer_id != previous.insurer_id or checklist.product != previous.checklist.product:
        raise ValidationError("Choose a checklist for the same insurer and product.")
    if due_date and due_date < timezone.localdate():
        raise ValidationError("Choose today or a future renewal deadline.")
    case = Case.objects.create(renewal_of=previous, reference="CY-" + uuid.uuid4().hex[:10].upper(),
        insurer=previous.insurer, applicant=previous.applicant, checklist=checklist, kind="renewal",
        service=previous.service, assignee=user, created_by=user, due_date=due_date)
    # Fresh requirements and authorization; no evidence, decisions, exceptions, or grants are copied.
    primary = ensure_case_service(case)
    for service in previous.services.all():
        data = {key: getattr(service, key) for key in ("name", "owner_name", "dependencies", "description")}
        if service.name == primary.name:
            for key, value in data.items():
                setattr(primary, key, value)
            primary.save()
        else:
            # New case has no consent yet; initialize inventory without a contributor operation.
            from evidence.models import Service, RecoveryPolicy
            new_service = Service.objects.create(case=case, created_by=user, **data)
            targets = checklist.recovery_targets
            if targets.get("duration_minutes") is not None and targets.get("data_age_minutes") is not None:
                RecoveryPolicy.objects.create(service=new_service, checklist=checklist, number=1, created_by=user,
                    duration_minutes=targets["duration_minutes"], data_age_minutes=targets["data_age_minutes"],
                    required_scope=f"Restore {new_service.name} and required dependencies; verify service usability.")
    for item in checklist.requirements:
        EvidenceRequest.objects.create(case=case, kind=item.get("kind", {"Supervised restore": "restore", "Backup coverage": "backup"}.get(item["name"], "control")),
            title=item["name"], description=item["evidence"], freshness_days=item["freshness_days"], due_date=due_date)
    audit(previous, user, "Renewal opened", case.reference)
    audit(case, user, "Renewal linked", previous.reference)
    invitation, token = issue_invitation(case, user, owner_email, "owner", case.applicant.name)
    return case, invitation, token
