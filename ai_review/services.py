from functools import partial

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from evidence.permissions import require_reviewer, require_version
from evidence.rules import freshness
from underwriting.permissions import require_decider
from underwriting.services import save_finding
from underwriting.snapshots import checksum, finding_data, normalize
from workspaces.models import Case
from workspaces.permissions import active_grant, get_case, require_owner
from workspaces.services import audit
from .errors import ProcessingError
from .models import ConsentRevocation, DraftDisposition, FactReview, FollowUpDraft, ProcessingAttempt, ProcessingConsent, ReviewJob
from .permissions import authorize_job, require_current_result, require_result_access
from .rules import effective_facts, rule_preview
from .tasks import dispatch_job
from .validation import validate_fact


@transaction.atomic
def authorize_external(user, case, expires_at, confirm):
    case = Case.objects.select_for_update().get(pk=case.pk)
    require_owner(user, case)
    grant = active_grant(user, case)
    if not confirm or not case.consent_active or expires_at <= timezone.now() or expires_at > min(grant.expires_at, case.consent_expires_at):
        raise ValidationError("Confirm external processing and choose a future expiry within your case authorization.")
    for consent in case.ai_consents.filter(revocation__isnull=True):
        ConsentRevocation.objects.create(consent=consent, actor=user)
    consent = ProcessingConsent.objects.create(case=case, provider="openai", authorized_by=user, expires_at=expires_at)
    audit(case, user, "External evidence AI authorized", str(consent.pk))
    return consent


@transaction.atomic
def revoke_external(user, consent):
    case = Case.objects.select_for_update().get(pk=consent.case_id)
    require_owner(user, case)
    ConsentRevocation.objects.get_or_create(consent=consent, defaults={"actor": user})
    audit(case, user, "External evidence AI revoked", str(consent.pk))


def current_consent(case):
    return next((row for row in case.ai_consents.filter(provider="openai", revocation__isnull=True, expires_at__gt=timezone.now()).order_by("-created_at") if row.active), None)


@transaction.atomic
def queue_review(user, version):
    case = Case.objects.select_for_update().get(pk=version.document.request.case_id)
    require_reviewer(user, case)
    require_version(user, version)
    if version.document.latest_version.pk != version.pk:
        raise ValidationError("Select the current document version for a new extraction.")
    if not version.latest_scan or version.latest_scan.status != "clean":
        raise ValidationError("File checks must pass before processing.")
    provider = settings.AI_REVIEW_PROVIDER
    consent = None
    if provider == "demo" and settings.DEBUG:
        model = "demo-line-extractor-v1"
    elif provider == "openai" and settings.AI_ALLOW_EXTERNAL and settings.OPENAI_API_KEY and settings.OPENAI_MODEL:
        model = settings.OPENAI_MODEL
        consent = current_consent(case)
        if not consent:
            raise ValidationError("The business owner must authorize external AI processing for this case first.")
    else:
        raise ValidationError("AI processing is not configured. You can continue manual evidence review.")
    policy = version.document.service.current_policy
    key = checksum([str(version.pk), user.pk, provider, model, str(consent.pk) if consent else None,
        str(policy.pk) if policy else None, "evidence-extraction-v1", "evidence-facts-v1"])
    job, created = ReviewJob.objects.get_or_create(dedupe_key=key, defaults={"case": case, "version": version,
        "requested_by": user, "consent": consent, "policy": policy, "provider": provider, "model_name": model})
    if created:
        audit(case, user, "Evidence extraction queued", str(job.pk))
        transaction.on_commit(partial(dispatch_job, job.pk))
    return job, created


@transaction.atomic
def retry_job(user, job):
    case = Case.objects.select_for_update().get(pk=job.case_id)
    require_reviewer(user, case)
    job = ReviewJob.objects.select_for_update().get(pk=job.pk)
    authorize_job(job, execution=True)
    if job.status == "completed" or (job.status == "running" and not job.stalled):
        raise ValidationError("This job is complete or still running. Open its review or wait for processing.")
    if job.version.document.latest_version.pk != job.version_id:
        raise ValidationError("Start a new review for the latest evidence version.")
    if job.stalled:
        ProcessingAttempt.objects.filter(job=job, status="running").update(status="interrupted", error_code="worker_interrupted", finished_at=timezone.now())
    job.status, job.execution_token, job.lease_expires_at = "queued", None, None
    job.retry_base = job.attempts
    job.error_code = job.error_message = ""
    job.save(update_fields=["status", "execution_token", "lease_expires_at", "retry_base", "error_code", "error_message", "updated_at"])
    audit(case, user, "Evidence extraction retried", str(job.pk))
    transaction.on_commit(partial(dispatch_job, job.pk))
    return job


@transaction.atomic
def cancel_job(user, job):
    case = Case.objects.select_for_update().get(pk=job.case_id)
    require_reviewer(user, case)
    job = ReviewJob.objects.select_for_update().get(pk=job.pk)
    if job.status == "completed":
        raise ValidationError("The result is already saved. Reject its suggestions through human review.")
    job.status, job.execution_token, job.lease_expires_at = "cancelled", None, None
    job.save(update_fields=["status", "execution_token", "lease_expires_at", "updated_at"])
    ProcessingAttempt.objects.filter(job=job, status="running").update(status="cancelled", finished_at=timezone.now())
    audit(case, user, "Evidence extraction cancelled", str(job.pk))


@transaction.atomic
def review_fact(user, result, fact_key, action, rationale, previous_review_id=0, value="", passage_id="", quote="", confirm=False):
    case = Case.objects.select_for_update().get(pk=result.job.case_id)
    require_current_result(user, result)
    fact = next((row for row in result.facts if row["key"] == fact_key), None)
    if not fact:
        raise Http404
    latest = result.reviews.filter(fact_key=fact_key).order_by("-created_at", "-pk").first()
    if (latest.pk if latest else 0) != previous_review_id:
        raise ValidationError("This suggestion was reviewed in another tab. Reload before changing it.")
    if not confirm or not rationale.strip():
        raise ValidationError("Review the original source, confirm your action, and record a rationale.")
    if action == "reject":
        reviewed_value, citations = None, []
    elif action in ("accept", "correct"):
        candidate = fact if action == "accept" else {"field": fact["field"], "value": value,
            "citations": [{"passage_id": passage_id, "quote": quote}]}
        checked = validate_fact(candidate, result.passages)
        if not checked["valid"]:
            raise ValidationError(" ".join(checked["validation_errors"]))
        reviewed_value, citations = checked["normalized_value"], checked["citations"]
    else:
        raise ValidationError("Choose accept, correct, or reject.")
    review = FactReview.objects.create(result=result, fact_key=fact_key, action=action, value=reviewed_value,
        citations=citations, rationale=rationale, reviewer=user)
    audit(case, user, "Extraction suggestion reviewed", f"{result.pk} · {fact_key} · {action}")
    return review


def draft_context(result):
    job = result.job
    version = job.version
    facts, conflicts = effective_facts(result)
    baseline = version.restore_tests.filter(request=version.document.request, service=version.document.service).first()
    policy = version.document.service.current_policy
    return normalize({"result_id": result.pk, "source_version_id": version.pk, "source_sha256": version.sha256,
        "provider": job.provider, "model": result.actual_model, "prompt_version": job.prompt_version, "schema_version": job.schema_version,
        "policy_id": policy.pk if policy else None, "rule_version": "restore-v1", "baseline_id": baseline.pk if baseline else None,
        "latest_version_id": version.document.latest_version.pk, "scan_id": version.latest_scan.pk if version.latest_scan else None,
        "evidence_review_id": version.latest_review.pk if version.latest_review else None,
        "freshness": freshness(version.observed_at, version.document.request.freshness_days), "facts": facts, "conflicts": conflicts,
        "reviews": list(result.reviews.order_by("pk").values_list("pk", flat=True))})


@transaction.atomic
def prepare_followup(user, result):
    case = Case.objects.select_for_update().get(pk=result.job.case_id)
    require_current_result(user, result)
    context = draft_context(result)
    preview = rule_preview(result)
    if not context["facts"]:
        raise ValidationError("Accept or correct at least one cited fact before preparing a follow-up.")
    if context["conflicts"]:
        raise ValidationError("Resolve conflicting accepted facts before preparing a follow-up.")
    item = result.job.version.document.request
    baseline = context["baseline_id"]
    is_retest = item.kind == "restore" and (preview["target_missed"] or preview["functional_failed"]) and baseline
    reasons = preview["reasons"] or ["Confirm the remaining scope and supporting evidence before underwriting review."]
    body = ["Please address the following evidence observations:", *[f"- {reason}" for reason in reasons]]
    if is_retest:
        body.append("Provide a dated supervised retest linked to the original test, covering the required service scope, elapsed recovery time, recovered data age, supervisor, and functional checks.")
    else:
        body.append("Provide the missing or clarified evidence with its source, observation date, service scope, and any relevant test measurements. A reviewer will verify the response.")
    body.append("Reviewed source passages:")
    for field, fact in context["facts"].items():
        for citation in fact["citations"]:
            body.append(f'{citation["locator"]}: "{citation["quote"]}"')
    body.append(f"Source evidence version: {result.job.version_id}; SHA-256: {result.source_sha256}.")
    content = {"title": f"{'Supervised retest' if is_retest else 'Evidence clarification'}: {item.title}"[:180],
        "request": str(item.pk), "kind": "retest" if is_retest else "evidence", "description": "\n".join(body),
        "priority": "high" if preview["target_missed"] or preview["functional_failed"] else "medium",
        "priority_reason": " ".join(reasons), "retest_of": baseline if is_retest else None}
    draft = FollowUpDraft.objects.create(result=result, context_sha256=checksum(context), review_snapshot=context, content=content, created_by=user)
    audit(case, user, "Evidence follow-up drafted", str(draft.pk))
    return draft


@transaction.atomic
def publish_followup(user, draft, finding_data_input, rationale, confirm=False):
    case = Case.objects.select_for_update().get(pk=draft.result.job.case_id)
    require_decider(user, case)
    draft = FollowUpDraft.objects.select_for_update().get(pk=draft.pk)
    require_current_result(user, draft.result)
    if hasattr(draft, "disposition"):
        raise ValidationError("This draft has already been sent or rejected.")
    if checksum(draft_context(draft.result)) != draft.context_sha256:
        raise ValidationError("Source reviews, targets, or evidence changed. Prepare a fresh follow-up draft.")
    if not confirm or not rationale.strip():
        raise ValidationError("Confirm the final request and explain why you are sending it.")
    data = dict(finding_data_input)
    data.pop("revision", None)
    if str(data["request"].pk) != draft.content["request"]:
        raise ValidationError("Keep this follow-up linked to its source evidence request.")
    if data["kind"] != draft.content["kind"] or (str(data["retest_of"].pk) if data.get("retest_of") else None) != draft.content["retest_of"]:
        raise ValidationError("Keep the reviewed request type and retest link. Create a separate manual finding for a different scope.")
    finding = save_finding(user, case, data)
    disposition = DraftDisposition.objects.create(draft=draft, action="publish", finding=finding, rationale=rationale,
        published_content=finding_data(finding), actor=user)
    audit(case, user, "Reviewed AI follow-up sent", str(finding.pk))
    return disposition


@transaction.atomic
def reject_followup(user, draft, rationale):
    case = Case.objects.select_for_update().get(pk=draft.result.job.case_id)
    require_reviewer(user, case)
    draft = FollowUpDraft.objects.select_for_update().get(pk=draft.pk)
    require_result_access(user, draft.result.job)
    if hasattr(draft, "disposition") or not rationale.strip():
        raise ValidationError("Choose an undecided draft and explain the rejection.")
    disposition = DraftDisposition.objects.create(draft=draft, action="reject", rationale=rationale, actor=user)
    audit(case, user, "AI follow-up draft rejected", str(draft.pk))
    return disposition
