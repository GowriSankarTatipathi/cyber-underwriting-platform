from datetime import timedelta
import uuid
from celery import shared_task
from celery.exceptions import Retry
from django.db import close_old_connections, transaction
from django.http import Http404
from django.utils import timezone
from .connectors import ConnectorError, collect
from .models import Connection, SyncAttempt, SyncRun
from .services import authorize_run, ingest, queue_sync, refresh_reminders


def fail(run_id, token, code, retryable=False, blocked=False):
    with transaction.atomic():
        run = SyncRun.objects.select_for_update().get(pk=run_id)
        if run.status != "running" or run.token != token:
            return False
        again = retryable and run.attempts - run.retry_base < 3
        run.status = "blocked" if blocked else "retry_wait" if again else "failed"
        run.error_code, run.lease_expires_at = code, None
        run.finished_at = timezone.now()
        run.save()
        SyncAttempt.objects.filter(run=run, number=run.attempts).update(status=run.status, error_code=code, finished_at=run.finished_at)
        return again


def run_sync(run_id):
    with transaction.atomic():
        run = SyncRun.objects.select_for_update().filter(pk=run_id).first()
        if not run or run.status not in ("queued", "retry_wait"):
            return False
        if run.attempts - run.retry_base >= 3:
            run.status, run.error_code = "failed", "attempt_limit"
            run.save(update_fields=["status", "error_code"])
            return False
        run.status, run.token = "running", uuid.uuid4()
        run.attempts += 1
        run.lease_expires_at = timezone.now() + timedelta(minutes=5)
        run.error_code = ""
        run.save()
        SyncAttempt.objects.create(run=run, number=run.attempts, status="running")
        token = run.token
    try:
        def guard():
            authorize_run(run)
            if run.status != "running" or run.token != token:
                raise Http404
        guard()
        rows = collect(run.connection, list(run.connection.assets.all()), guard)
        guard()
        ingest(run.pk, token, rows)
        return False
    except Http404:
        return fail(run_id, token, "authorization_or_rules_changed", blocked=True)
    except ConnectorError as error:
        return fail(run_id, token, error.code, error.retryable)
    except Exception:
        return fail(run_id, token, "invalid_or_failed_processing")


@shared_task(bind=True, ignore_result=True, max_retries=2)
def poll_connection(self, run_id):
    close_old_connections()
    try:
        if run_sync(run_id):
            try:
                raise self.retry(countdown=30 * (2 ** self.request.retries))
            except Retry:
                raise
            except Exception:
                SyncRun.objects.filter(pk=run_id, status="retry_wait").update(status="failed", error_code="retry_queue_unavailable")
    finally:
        close_old_connections()


def dispatch(run_id):
    try:
        poll_connection.apply_async(args=[str(run_id)], argsrepr="(<private monitoring run>,)", retry=False)
    except Exception:
        SyncRun.objects.filter(pk=run_id, status="queued").update(status="failed", error_code="queue_unavailable")


@shared_task(ignore_result=True)
def schedule_monitoring():
    close_old_connections()
    try:
        for connection in Connection.objects.filter(authorizations__revocation__isnull=True, authorizations__expires_at__gt=timezone.now()).distinct().iterator():
            try:
                queue_sync(connection.created_by, connection)
            except Exception:
                # Per-connection authorization/configuration failures must not stop other cases.
                continue
    finally:
        close_old_connections()


@shared_task(ignore_result=True)
def schedule_reminders():
    from workspaces.models import Case
    close_old_connections()
    try:
        for case in Case.objects.filter(consent_expires_at__gt=timezone.now(), consent_revoked_at__isnull=True).iterator():
            try:
                refresh_reminders(case)
            except Http404:
                continue
    finally:
        close_old_connections()
