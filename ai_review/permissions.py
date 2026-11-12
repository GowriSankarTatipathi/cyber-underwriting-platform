from django.conf import settings
from django.http import Http404
from evidence.permissions import require_reviewer
from workspaces.models import Case
from .errors import ProcessingError


def authorize_job(job, execution=False):
    """No cached permission decisions survive a storage or provider round trip."""
    job.refresh_from_db()
    job.requested_by.refresh_from_db()
    require_reviewer(job.requested_by, job.case)
    if job.version.document.request.case_id != job.case_id:
        raise Http404
    scan = job.version.latest_scan
    if not scan or scan.status != "clean":
        raise Http404
    if job.provider == "openai":
        if not job.consent or job.consent.case_id != job.case_id or not job.consent.active:
            raise Http404
        if execution and (not settings.AI_ALLOW_EXTERNAL or settings.AI_REVIEW_PROVIDER != "openai"):
            raise Http404
    elif job.provider != "demo" or not settings.DEBUG:
        raise Http404
    return job


def require_result_access(user, job):
    case = Case.objects.get(pk=job.case_id)
    require_reviewer(user, case)
    scan = job.version.latest_scan
    if not scan or scan.status != "clean":
        raise Http404
    return case


def require_current_result(user, result):
    job = result.job
    require_result_access(user, job)
    if job.version.document.latest_version.pk != job.version_id:
        raise ProcessingError("superseded", "A newer evidence version exists. Process and review the current version.")
    current = job.version.document.service.current_policy
    if (current.pk if current else None) != job.policy_id:
        raise ProcessingError("targets_changed", "Recovery targets changed. Start a new review against the current targets.")
    return job.case
