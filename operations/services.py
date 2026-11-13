from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from ai_review.permissions import require_result_access
from evidence.permissions import require_reviewer
from underwriting.snapshots import normalize
from workspaces.models import Case
from workspaces.permissions import get_case
from workspaces.services import audit
from .models import ExtractionEvaluation, FollowupEvaluation, ReviewSession


@transaction.atomic
def timer(user, case, action, notes=""):
    case = Case.objects.select_for_update().get(pk=case.pk)
    require_reviewer(user, case)
    type(user).objects.select_for_update().get(pk=user.pk)
    active = ReviewSession.objects.filter(reviewer=user, ended_at__isnull=True).first()
    if action == "start":
        if active:
            raise ValidationError("Stop your existing review timer before starting another.")
        return ReviewSession.objects.create(case=case, reviewer=user)
    if action != "stop" or not active or active.case_id != case.pk:
        raise ValidationError("There is no active timer for you on this case.")
    active.ended_at, active.notes = timezone.now(), notes[:300]
    active.save(update_fields=["ended_at", "notes"])
    return active


@transaction.atomic
def evaluate_extraction(user, result, expected_facts, correct_facts, correct_citations, rationale):
    case = Case.objects.select_for_update().get(pk=result.job.case_id)
    require_result_access(user, result.job)
    total = len(result.facts)
    # Count all model citations, including invalid ones, rather than only validated citations.
    citations = sum(row["submitted_citation_count"] for row in result.facts) if all("submitted_citation_count" in row for row in result.facts) else None
    if not rationale.strip() or min(expected_facts, correct_facts, correct_citations) < 0 or correct_facts > min(total, expected_facts) or citations is not None and correct_citations > citations:
        raise ValidationError("Check the independently labelled fact/citation counts and explain the assessment.")
    assessment = ExtractionEvaluation.objects.create(result=result, expected_facts=expected_facts, correct_facts=correct_facts,
        correct_citations=correct_citations if citations is not None else None, extracted_facts=total, extracted_citations=citations, rationale=rationale, evaluator=user)
    audit(case, user, "Pilot extraction assessed", str(assessment.pk))
    return assessment


@transaction.atomic
def evaluate_followup(user, finding, sufficient, rationale):
    case = Case.objects.select_for_update().get(pk=finding.case_id)
    require_reviewer(user, case)
    response = finding.responses.order_by("created_at", "pk").first()
    if not response or not rationale.strip():
        raise ValidationError("A first response and an assessment rationale are required.")
    result = FollowupEvaluation.objects.create(finding=finding, first_response=response, sufficient=sufficient, rationale=rationale, evaluator=user)
    audit(case, user, "Pilot first response assessed", str(result.pk))
    return result


def metrics(user, case):
    case = get_case(user, case.pk)
    require_reviewer(user, case)
    sessions = case.pilot_sessions.filter(ended_at__isnull=False)
    seconds = sum((row.ended_at - row.started_at).total_seconds() for row in sessions)
    evaluations = {}
    for row in ExtractionEvaluation.objects.filter(result__job__case=case).order_by("created_at", "pk"):
        evaluations[row.result_id] = row
    follows = {}
    for row in FollowupEvaluation.objects.filter(finding__case=case).order_by("created_at", "pk"):
        follows[row.finding_id] = row
    totals = {field: sum(getattr(row, field) or 0 for row in evaluations.values()) for field in ("expected_facts", "correct_facts", "extracted_facts", "correct_citations", "extracted_citations")}
    def ratio(n, d):
        return round(n / d, 4) if d else None
    return normalize({"case_reference": case.reference, "review_minutes": round(seconds / 60, 2), "completed_review_sessions": sessions.count(),
        "active_sessions_excluded": case.pilot_sessions.filter(ended_at__isnull=True).count(), "evaluated_extractions": len(evaluations), **totals,
        "fact_precision": ratio(totals["correct_facts"], totals["extracted_facts"]), "fact_recall": ratio(totals["correct_facts"], totals["expected_facts"]),
        "citation_accuracy": ratio(totals["correct_citations"], totals["extracted_citations"]), "assessed_first_responses": len(follows),
        "sufficient_first_responses": sum(row.sufficient for row in follows.values()),
        "first_response_success_rate": ratio(sum(row.sufficient for row in follows.values()), len(follows)),
        "method": "Explicit review timers and independent human evaluations; ratios use assessed items only. Null means no denominator. Older extractions without original citation counts are excluded from citation accuracy. Evaluations do not approve evidence or close findings."})
