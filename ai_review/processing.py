from datetime import timedelta
import uuid

from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from pydantic import ValidationError as SchemaError

from evidence.storage import StorageUnavailable, read_private
from workspaces.models import Case
from workspaces.services import audit
from .errors import ProcessingError
from .models import ExtractionResult, ProcessingAttempt, ReviewJob
from .permissions import authorize_job
from .preparation import prepare_document
from .providers import extract
from .validation import validate_extraction


def finish_error(job_id, token, code, message, retryable=False, blocked=False):
    with transaction.atomic():
        job = ReviewJob.objects.select_for_update().get(pk=job_id)
        if job.status != "running" or job.execution_token != token:
            return False
        again = retryable and job.attempts - job.retry_base < settings.AI_MAX_ATTEMPTS
        job.status = "blocked" if blocked else ("retry_wait" if again else "failed")
        job.error_code, job.error_message = code, message
        job.lease_expires_at = None
        job.save(update_fields=["status", "error_code", "error_message", "lease_expires_at", "updated_at"])
        ProcessingAttempt.objects.filter(execution_token=token, status="running").update(status=job.status, error_code=code, finished_at=timezone.now())
        return again


def run_job(job_id):
    try:
        with transaction.atomic():
            job = ReviewJob.objects.select_for_update().get(pk=job_id)
            if job.status not in ("queued", "retry_wait"):
                return False
            if job.attempts - job.retry_base >= settings.AI_MAX_ATTEMPTS:
                job.status, job.error_code = "failed", "attempt_limit"
                job.error_message = "The automatic retry limit was reached. Review manually or explicitly retry."
                job.save(update_fields=["status", "error_code", "error_message", "updated_at"])
                return False
            token = uuid.uuid4()
            job.status, job.execution_token = "running", token
            job.attempts += 1
            job.lease_expires_at = timezone.now() + timedelta(seconds=settings.AI_LEASE_SECONDS)
            job.error_code = job.error_message = ""
            job.save(update_fields=["status", "execution_token", "attempts", "lease_expires_at", "error_code", "error_message", "updated_at"])
            ProcessingAttempt.objects.create(job=job, number=job.attempts, execution_token=token)
    except ReviewJob.DoesNotExist:
        return False
    try:
        authorize_job(job, execution=True)
        data = read_private(job.version)
        authorize_job(job, execution=True)
        prepared = prepare_document(data, job.version.original_name)
        authorize_job(job, execution=True)
        if job.status != "running" or job.execution_token != token:
            return False
        payload, response_id, model = extract(job, prepared["passages"])
        authorize_job(job, execution=True)
        facts, warnings = validate_extraction(payload, prepared["passages"])
        with transaction.atomic():
            # Match the case-first lock order used by Phase 3/4 mutations and cancellation.
            Case.objects.select_for_update().get(pk=job.case_id)
            job = ReviewJob.objects.select_for_update().get(pk=job_id)
            authorize_job(job, execution=True)
            if job.status != "running" or job.execution_token != token:
                return False
            ExtractionResult.objects.create(job=job, source_sha256=job.version.sha256, passages=prepared["passages"],
                facts=facts, warnings=prepared["warnings"] + warnings,
                provider_response_id=response_id[:200], actual_model=model[:100])
            job.status, job.lease_expires_at = "completed", None
            job.save(update_fields=["status", "lease_expires_at", "updated_at"])
            ProcessingAttempt.objects.filter(execution_token=token, status="running").update(status="completed", finished_at=timezone.now())
            audit(job.case, job.requested_by, "Evidence extraction completed", str(job.pk))
        return False
    except Http404:
        return finish_error(job_id, token, "access_revoked", "Processing stopped because authorization or file checks changed. Manual case review remains available to authorized users.", blocked=True)
    except ProcessingError as error:
        return finish_error(job_id, token, error.code, error.message, error.retryable)
    except StorageUnavailable:
        return finish_error(job_id, token, "storage_unavailable", "The exact evidence version could not be read or verified. Retry storage access or continue manual review.", True)
    except SchemaError:
        return finish_error(job_id, token, "invalid_response", "The extracted fields did not match the required schema. Continue manual review.")
    except Exception:
        # Never log source text, provider bodies, credentials, or unvalidated exception strings.
        return finish_error(job_id, token, "processing_failed", "Processing failed. Retry or continue the existing manual evidence workflow.")
