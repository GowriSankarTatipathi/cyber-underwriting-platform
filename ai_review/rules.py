from decimal import Decimal

from evidence.rules import freshness, recovery_target_reasons
from .validation import LABELS


def fact_rows(result):
    reviews = {}
    for review in result.reviews.order_by("created_at", "pk"):
        reviews[review.fact_key] = review
    return [{**fact, "label": LABELS[fact["field"]], "review": reviews.get(fact["key"])} for fact in result.facts]


def effective_facts(result, reviewed_only=True):
    grouped = {}
    for fact in fact_rows(result):
        review = fact["review"]
        if review:
            if review.action == "reject":
                continue
            value, citations = review.value, review.citations
        elif not reviewed_only and fact["valid"]:
            value, citations = fact["normalized_value"], fact["citations"]
        else:
            continue
        grouped.setdefault(fact["field"], []).append({"key": fact["key"], "value": value, "citations": citations,
            "review_id": review.pk if review else None})
    facts, conflicts = {}, []
    for field, rows in grouped.items():
        if len({str(row["value"]) for row in rows}) > 1:
            conflicts.append(f"Conflicting values for {LABELS[field]}; reject or correct the conflicting suggestions.")
        else:
            facts[field] = rows[0]
    return facts, conflicts


def rule_preview(result, reviewed_only=True):
    job = result.job
    facts, conflicts = effective_facts(result, reviewed_only)
    reasons = list(conflicts)
    policy = job.policy
    values = {field: row["value"] for field, row in facts.items()}
    duration = Decimal(values["restore_duration_minutes"]) if "restore_duration_minutes" in values else None
    age = Decimal(values["recovered_data_age_minutes"]) if "recovered_data_age_minutes" in values else None
    functional = values.get("functional_checks_passed")
    missed = recovery_target_reasons(duration, age, policy) if policy else []
    reasons.extend(missed)
    if functional is False:
        reasons.append("The cited functional checks failed.")
    missing = [LABELS[key] for key in ("restore_duration_minutes", "recovered_data_age_minutes", "functional_checks_passed") if key not in values]
    if job.version.document.request.kind == "restore" and missing:
        reasons.append("Missing or unreviewed: " + ", ".join(missing) + ".")
    if freshness(job.version.observed_at, job.version.document.request.freshness_days) != "Fresh":
        reasons.append("The evidence is stale under the insurer's freshness rule.")
    if not policy:
        reasons.append("No recovery policy is configured for this service.")
    if functional is False:
        status = "Functional checks failed"
    elif missed:
        status = "Target missed"
    elif job.version.document.request.kind != "restore":
        status = "Facts available for control review"
    elif missing or conflicts or not policy:
        status = "Inconclusive"
    else:
        status = "Measurements meet targets; restore verification still required"
    return {"status": status, "reasons": reasons, "facts": facts, "conflicts": conflicts,
        "missing": missing, "target_missed": bool(missed), "functional_failed": functional is False,
        "rule_version": "restore-v1", "reviewed_only": reviewed_only}


def discrepancies(result):
    job = result.job
    values, conflicts = effective_facts(result, reviewed_only=False)
    issues = list(conflicts)
    if "service" in values and str(values["service"]["value"]).casefold() != job.version.document.service.name.casefold():
        issues.append("The document's service name differs from the selected service inventory entry.")
    for test in job.version.restore_tests.filter(request=job.version.document.request):
        for field, attr in (("restore_duration_minutes", "duration_minutes"), ("recovered_data_age_minutes", "data_age_minutes"), ("functional_checks_passed", "functional_checks_passed")):
            if field in values and getattr(test, attr) is not None:
                expected = Decimal(values[field]["value"]) if field.endswith("_minutes") else values[field]["value"]
                if expected != getattr(test, attr):
                    issues.append(f"{LABELS[field]} differs from restore record {test.pk}. Review the source and record separately.")
    # Compare only the same request/service and the same observation time. Later retests
    # are history, not automatically a discrepancy with the initial failed test.
    from .models import ExtractionResult
    others = ExtractionResult.objects.filter(job__version__document__request=job.version.document.request,
        job__version__document__service=job.version.document.service,
        job__version__observed_at=job.version.observed_at).exclude(pk=result.pk).select_related("job__version__document")
    for other in others:
        if other.job.version.document.latest_version.pk != other.job.version_id:
            continue
        other_values, _ = effective_facts(other, reviewed_only=True)
        for field in values.keys() & other_values.keys():
            if values[field]["value"] != other_values[field]["value"]:
                issues.append(f"{LABELS[field]} differs from reviewed document {other.job.version.document.title} at the same observation time.")
    return list(dict.fromkeys(issues))
