from .rules import control_summary, current_restore_status, freshness


def request_payload(item):
    summary = control_summary(item)
    assessment = summary["assessment"]
    return {
        "id": str(item.pk), "title": item.title, "description": item.description,
        "kind": item.kind, "freshness_days": item.freshness_days,
        "assignee": item.assigned_to.email if item.assigned_to else None,
        "control_status": summary["status"],
        "assessment": {"id": assessment.pk, "result": assessment.result, "rationale": assessment.rationale,
            "rule_version": assessment.rule_version, "checklist_version": assessment.checklist_version,
            "reviewer": assessment.reviewer.email, "reviewed_at": assessment.created_at,
            "evidence_snapshot": assessment.review_snapshot} if assessment else None,
        "documents": [{"id": str(document.pk), "title": document.title, "service": document.service.name,
            "versions": [{"id": str(version.pk), "number": version.number, "sha256": version.sha256,
                "source": version.source, "scope": version.scope, "observed_at": version.observed_at,
                "freshness": freshness(version.observed_at, item.freshness_days),
                "file_checks": version.latest_scan.status if version.latest_scan else "quarantined",
                "review": version.latest_review.decision if version.latest_review else "needs_review"}
                for version in document.versions.all()]} for document in item.documents.all()],
        "restore_tests": [{"id": str(test.pk), "retest_of": str(test.retest_of_id) if test.retest_of_id else None,
            "performed_at": test.performed_at, "scope": test.scope, "service_snapshot": test.service_snapshot,
            "duration_minutes": test.duration_minutes, "data_age_minutes": test.data_age_minutes,
            "functional_checks_passed": test.functional_checks_passed,
            "targets": {"policy_id": str(test.policy_id), "version": test.policy.number, "duration_minutes": test.policy.duration_minutes, "data_age_minutes": test.policy.data_age_minutes},
            "current_status": current_restore_status(test),
            "reviewed_result": test.latest_review.result if test.latest_review else None,
            "evidence_version_ids": [str(pk) for pk in test.evidence_versions.values_list("pk", flat=True)]}
            for test in item.restore_tests.all()],
        "backup_jobs": [{"id": str(job.pk), "service": job.service.name, "observed_at": job.observed_at,
            "status": job.status, "coverage": job.coverage, "evidence_version_id": str(job.source_version_id)}
            for job in item.backup_jobs.all()],
    }
