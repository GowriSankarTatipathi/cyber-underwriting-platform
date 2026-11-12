from datetime import timedelta
from django.utils import timezone


def freshness(observed_at, days, at=None):
    at = at or timezone.now()
    if observed_at is None or observed_at > at:
        return "Unknown"
    return "Fresh" if at <= observed_at + timedelta(days=days) else "Stale"


def usable_version(version, days, at=None):
    scan = version.latest_scan
    review = version.latest_review
    return bool(scan and scan.status == "clean" and review and review.decision == "accepted"
                and review.scope_confirmed and freshness(version.observed_at, days, at) == "Fresh")


def evidence_snapshot(versions):
    return {str(version.pk): {"sha256": version.sha256,
        "review_id": version.latest_review.pk if version.latest_review else None,
        "scan_id": version.latest_scan.pk if version.latest_scan else None}
        for version in versions}


def evaluate_restore(test, scope_confirmed, at=None):
    at = at or timezone.now()
    reasons = []
    if test.functional_checks_passed is False:
        return "Failed", ["The recovered service failed its functional checks."]
    if scope_confirmed is False:
        return "Partial", ["The restore did not cover the required service scope."]
    if scope_confirmed is None:
        reasons.append("A reviewer has not confirmed the required scope.")
    if test.duration_minutes is None:
        reasons.append("Elapsed recovery time is missing.")
    if test.data_age_minutes is None:
        reasons.append("Recovered data age is missing.")
    if test.functional_checks_passed is None:
        reasons.append("The functional check result is missing.")
    if test.functional_checks_passed and not test.functional_checks.strip():
        reasons.append("Describe the functional checks that demonstrated the service worked.")
    if freshness(test.performed_at, test.request.freshness_days, at) != "Fresh":
        reasons.append("The test date is stale or invalid under the request's freshness rule.")
    versions = list(test.evidence_versions.all())
    if not versions or any(not usable_version(version, test.request.freshness_days, at) for version in versions):
        reasons.append("Every cited evidence version needs clean file checks, accepted scope, and fresh evidence.")
    if reasons:
        return "Inconclusive", reasons
    if test.duration_minutes > test.policy.duration_minutes:
        reasons.append(f"Recovery time {test.duration_minutes} min exceeds the {test.policy.duration_minutes} min target.")
    if test.data_age_minutes > test.policy.data_age_minutes:
        reasons.append(f"Recovered data age {test.data_age_minutes} min exceeds the {test.policy.data_age_minutes} min target.")
    return ("Target missed", reasons) if reasons else ("Passed", ["Scope, functional checks, evidence freshness, and both recovery targets are satisfied."])


def current_restore_status(test):
    review = test.latest_review
    if not review:
        return "Needs review"
    versions = list(test.evidence_versions.all())
    if evidence_snapshot(versions) != review.evidence_snapshot:
        return "Needs review"
    if freshness(test.performed_at, test.request.freshness_days) == "Stale" or any(freshness(v.observed_at, test.request.freshness_days) == "Stale" for v in versions):
        return "Stale"
    current_policy = test.service.current_policy
    if current_policy and current_policy.pk != test.policy_id:
        return "Prior target version"
    return review.result


def control_summary(item):
    versions = [document.latest_version for document in item.documents.all()]
    versions = [version for version in versions if version]
    assessment = item.assessments.order_by("-created_at", "-pk").first()
    status = "Missing"
    reason = "No evidence has been submitted."
    if versions:
        status, reason = "Needs review", "Review the current evidence versions and record a control result."
        if assessment:
            recorded = {str(version.pk) for version in assessment.evidence_versions.all()}
            if recorded == {str(version.pk) for version in versions} and evidence_snapshot(versions) == assessment.review_snapshot:
                status, reason = assessment.get_result_display(), assessment.rationale
                if status == "Verified" and any(not usable_version(v, item.freshness_days) for v in versions):
                    status, reason = "Stale", "The evidence is no longer fresh or accepted. Refresh and review it."
                if status == "Verified" and item.kind == "restore":
                    if not restore_control_tests(item):
                        status, reason = "Needs review", "Each critical service needs a passing latest restore against current targets."
    return {"status": status, "reason": reason, "assessment": assessment, "versions": versions}


def restore_control_tests(item):
    tests = []
    for service in item.case.services.all():
        latest = item.restore_tests.filter(service=service).order_by("-performed_at", "-created_at").first()
        if not latest or current_restore_status(latest) != "Passed":
            return []
        tests.append(latest)
    return tests
