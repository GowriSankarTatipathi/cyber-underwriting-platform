from celery import shared_task
from celery.exceptions import Retry
from django.db import close_old_connections
from .models import ReviewJob
from .processing import run_job


@shared_task(bind=True, ignore_result=True, max_retries=2)
def process_evidence(self, job_id):
    close_old_connections()
    try:
        retry = run_job(job_id)
        if retry:
            try:
                raise self.retry(countdown=10 * (2 ** self.request.retries))
            except self.MaxRetriesExceededError:
                ReviewJob.objects.filter(pk=job_id, status="retry_wait").update(status="failed", error_code="attempt_limit", error_message="Automatic retries ended. Retry explicitly or continue manual review.")
            except Retry:
                raise
            except Exception:
                ReviewJob.objects.filter(pk=job_id, status="retry_wait").update(status="failed", error_code="queue_unavailable", error_message="The queue could not schedule a retry. Retry explicitly when the queue is available, or continue manual review.")
    finally:
        close_old_connections()


def dispatch_job(job_id):
    try:
        process_evidence.apply_async(args=[str(job_id)], argsrepr="(<private job ID>,)", retry=False)
    except Exception:
        ReviewJob.objects.filter(pk=job_id, status__in=["queued", "retry_wait"]).update(status="failed", error_code="queue_unavailable",
            error_message="The processing queue is unavailable. Start Redis and a worker, then retry. Manual review remains available.")
