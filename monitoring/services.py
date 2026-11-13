from datetime import datetime, time, timedelta, timezone as dt_timezone
from functools import partial
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from evidence.permissions import require_reviewer
from evidence.services import check_date
from underwriting.permissions import require_decider
from underwriting.services import save_finding
from underwriting.snapshots import checksum, normalize
from workspaces.models import Case
from workspaces.permissions import active_grant, get_case, require_owner
from workspaces.services import audit
from .models import Alert, Asset, AssetInventory, Authorization, AuthorizationRevocation, Connection, Observation, ObservationGroup, Policy, Receipt, Reconciliation, Reminder, ReminderAcknowledgment, SyncAttempt, SyncRun
from .rules import evaluate, latest_observations, reconciliation_context
from .validation import CVE_RE, identifier, validate_config, validate_observation


def lock_case(user, case):
    case = Case.objects.select_for_update().get(pk=case.pk)
    user.refresh_from_db()
    get_case(user, case.pk)
    return case


@transaction.atomic
def create_asset(user, case, data):
    case = lock_case(user, case)
    require_reviewer(user, case)
    if data["request"].case_id != case.pk or data["service"].case_id != case.pk:
        raise Http404
    data = {**data, "identifier": identifier(data["kind"], data["identifier"])}
    asset = Asset(case=case, created_by=user, **data)
    asset.full_clean()
    asset.save()
    audit(case, user, "Monitoring asset recorded", str(asset.pk))
    return asset


@transaction.atomic
def record_inventory(user, asset, cves, source, observed_at):
    case = lock_case(user, asset.case)
    require_reviewer(user, case)
    check_date(observed_at)
    cves = sorted(set(cves))
    if len(cves) > 1000 or any(not CVE_RE.fullmatch(c) for c in cves) or not source.strip():
        raise ValidationError("Record valid CVE identifiers and the inventory source.")
    return AssetInventory.objects.create(asset=asset, cves=cves, source=source, observed_at=observed_at, recorded_by=user)


@transaction.atomic
def create_connection(user, case, name, provider, assets, config, credential_ref=""):
    case = lock_case(user, case)
    require_decider(user, case)
    assets = list(assets)
    if any(a.case_id != case.pk for a in assets):
        raise Http404
    if provider == "demo" and not settings.DEBUG:
        raise ValidationError("Fictional monitoring is limited to development.")
    validate_config(provider, config, assets, credential_ref)
    connection = Connection(case=case, name=name, provider=provider, config=config, credential_ref=credential_ref, created_by=user)
    connection.full_clean()
    connection.save()
    connection.assets.set(assets)
    audit(case, user, "Monitoring connection prepared", str(connection.pk))
    return connection


def connection_scope(connection):
    return normalize({"provider": connection.provider, "config": connection.config, "credential_ref": connection.credential_ref,
        "assets": list(connection.assets.order_by("pk").values("id", "identifier", "kind"))})


@transaction.atomic
def authorize(user, connection, expires_at, rights_confirmed):
    case = lock_case(user, connection.case)
    require_owner(user, case)
    grant = active_grant(user, case)
    if not rights_confirmed or not case.consent_active or not timezone.now() < expires_at <= min(grant.expires_at, case.consent_expires_at):
        raise ValidationError("Confirm asset authority and provider license rights, with an expiry within current case access.")
    for old in connection.authorizations.filter(revocation__isnull=True):
        AuthorizationRevocation.objects.create(authorization=old, actor=user)
    result = Authorization.objects.create(connection=connection, authorized_by=user, expires_at=expires_at, rights_confirmed=True, scope=connection_scope(connection))
    audit(case, user, "Monitoring authorized", str(connection.pk))
    return result


@transaction.atomic
def revoke(user, authorization):
    case = lock_case(user, authorization.connection.case)
    require_owner(user, case)
    AuthorizationRevocation.objects.get_or_create(authorization=authorization, defaults={"actor": user})
    audit(case, user, "Monitoring authorization revoked", str(authorization.pk))


@transaction.atomic
def publish_policy(user, case, data):
    case = lock_case(user, case)
    require_decider(user, case)
    previous = case.monitoring_policies.order_by("-number").first()
    policy = Policy(case=case, number=previous.number + 1 if previous else 1, created_by=user, **data)
    if not 0 <= policy.min_mfa_percent <= 100 or not 15 <= policy.poll_minutes <= 10080 or not 1 <= policy.freshness_hours <= 8760 or not 1 <= policy.max_backup_age_hours <= 8760 or not 1 <= policy.due_days <= 365:
        raise ValidationError("Check percentage, poll interval (15–10080 minutes), freshness/backup age (1–8760 hours), and deadline (1–365 days).")
    if not isinstance(policy.blocked_ports, list) or any(type(p) is not int or not 1 <= p <= 65535 for p in policy.blocked_ports):
        raise ValidationError("Restricted ports must be a list of port numbers.")
    policy.full_clean()
    policy.save()
    audit(case, user, "Monitoring rules published", str(policy.pk))
    return policy


def authorize_run(run):
    run.refresh_from_db()
    run.requested_by.refresh_from_db()
    case = get_case(run.requested_by, run.connection.case_id)
    require_reviewer(run.requested_by, case)
    if not run.authorization.active or run.authorization.connection_id != run.connection_id or checksum(run.authorization.scope) != checksum(connection_scope(run.connection)):
        raise Http404
    if case.monitoring_policies.order_by("-number").first().pk != run.policy_id:
        raise Http404
    if run.connection.provider == "demo" and not settings.DEBUG:
        raise Http404
    return case


@transaction.atomic
def queue_sync(user, connection):
    case = lock_case(user, connection.case)
    require_reviewer(user, case)
    authorization = next((a for a in connection.authorizations.filter(revocation__isnull=True).order_by("-pk") if a.active), None)
    policy = case.monitoring_policies.order_by("-number").first()
    if not policy or not authorization:
        raise ValidationError("Publish monitoring rules and obtain business authorization before polling.")
    if connection.provider != "demo" and not settings.MONITORING_LIVE_ENABLED:
        raise ValidationError("Live monitoring is disabled on this server. Configure it before polling.")
    slot = f"{int(timezone.now().timestamp()) // (policy.poll_minutes * 60)}:{policy.pk}:{authorization.pk}"
    run, created = SyncRun.objects.get_or_create(connection=connection, slot=slot, defaults={"authorization": authorization, "policy": policy, "requested_by": user})
    if created:
        from .tasks import dispatch
        transaction.on_commit(partial(dispatch, run.pk))
        audit(case, user, "Monitoring poll queued", str(run.pk))
    return run


@transaction.atomic
def retry_sync(user, run):
    case = lock_case(user, run.connection.case)
    require_reviewer(user, case)
    run = SyncRun.objects.select_for_update().get(pk=run.pk)
    authorize_run(run)
    if run.status == "completed" or run.status == "running" and run.lease_expires_at > timezone.now():
        raise ValidationError("The poll is completed or its worker is still running.")
    SyncAttempt.objects.filter(run=run, status="running").update(status="interrupted", finished_at=timezone.now())
    run.status, run.token, run.lease_expires_at, run.error_code = "queued", None, None, ""
    run.retry_base = run.attempts
    run.save()
    from .tasks import dispatch
    transaction.on_commit(partial(dispatch, run.pk))


@transaction.atomic
def ingest(run_id, token, rows):
    run = SyncRun.objects.get(pk=run_id)
    Case.objects.select_for_update().get(pk=run.connection.case_id)
    run = SyncRun.objects.select_for_update().get(pk=run_id)
    authorize_run(run)
    if run.status != "running" or run.token != token:
        return
    assets = list(run.connection.assets.all())
    if len(rows) > 1000:
        raise ValidationError("Too many observations in one poll.")
    validated = [validate_observation(row, assets) for row in rows]
    for row, asset in validated:
        prior = next((o for o in latest_observations(asset, row.kind) if o.group.connection_id == run.connection_id), None)
        now = timezone.now()
        group, _ = ObservationGroup.objects.get_or_create(connection=run.connection, asset=asset, kind=row.kind, fingerprint=checksum(row.data),
            defaults={"first_seen": now, "last_seen": now})
        ObservationGroup.objects.filter(pk=group.pk).update(last_seen=now)
        event_key = checksum([row.source_event_id, row.observed_at.isoformat(), row.source_time_known, row.data])
        observation, _ = Observation.objects.get_or_create(group=group, event_key=event_key, defaults={"source_event_id": row.source_event_id,
            "observed_at": row.observed_at, "source_time_known": row.source_time_known, "data": row.data, "provenance": row.provenance})
        Receipt.objects.get_or_create(run=run, observation=observation)
        # Late arrivals remain history and never replace current observations or trigger new work.
        if prior and prior.observed_at > observation.observed_at:
            continue
        outcome = evaluate(observation, run.policy)
        if run.policy.auto_followup and outcome["reasons"]:
            last = group.alerts.filter(policy=run.policy).order_by("-episode").first()
            recurring = last and last.finding.state == "closed" and prior and prior.group_id != group.pk
            if not last or recurring:
                owner = run.policy.created_by
                owner.refresh_from_db()
                require_reviewer(owner, run.connection.case)
                description = "\n".join(outcome["reasons"] + outcome["caveats"] + ["Source: " + row.provenance.get("source", run.connection.provider),
                    "Observed: " + row.observed_at.isoformat(), "This observation requires evidence review; it is not a verified control result."])
                finding = save_finding(run.requested_by, run.connection.case, {"request": asset.request, "title": f"Monitoring review: {asset.name}"[:180],
                    "description": description, "kind": "evidence", "priority": "high", "priority_reason": " ".join(outcome["reasons"]),
                    "owner": owner, "due_date": timezone.localdate() + timedelta(days=run.policy.due_days), "retest_of": None})
                Alert.objects.create(group=group, policy=run.policy, episode=last.episode + 1 if last else 1, observation=observation, finding=finding,
                    snapshot=normalize({"observation_id": observation.pk, "data": row.data, "source": row.provenance, "observed_at": row.observed_at,
                        "rule": outcome, "authorization_id": run.authorization_id}))
    run.status, run.finished_at, run.lease_expires_at = "completed", timezone.now(), None
    run.save(update_fields=["status", "finished_at", "lease_expires_at"])
    SyncAttempt.objects.filter(run=run, number=run.attempts).update(status="completed", finished_at=timezone.now())
    audit(run.connection.case, run.requested_by, "Monitoring poll completed", str(run.pk))


@transaction.atomic
def reconcile(user, asset, kind, selected, rationale, context_sha256):
    case = lock_case(user, asset.case)
    require_reviewer(user, case)
    context = reconciliation_context(asset, kind)
    if not rationale.strip() or context_sha256 != checksum(context) or selected and str(selected.pk) not in {row["id"] for row in context}:
        raise ValidationError("Observations changed or the selection is invalid. Reload and record a rationale.")
    result = Reconciliation.objects.create(asset=asset, kind=kind, observation=selected, context_sha256=context_sha256, context=context, rationale=rationale, reviewer=user)
    audit(case, user, "Monitoring observations reconciled", str(result.pk))
    return result


def refresh_reminders(case):
    require_reviewer(case.created_by, case)
    now, cutoff = timezone.now(), timezone.now() + timedelta(days=7)
    candidates = []
    def midnight(day):
        return datetime.combine(day, time.min, tzinfo=dt_timezone.utc)
    if case.due_date and midnight(case.due_date) <= now + timedelta(days=30):
        candidates.append(("renewal", str(case.pk), midnight(case.due_date), "Case review or renewal is due: " + case.reference))
    for finding in case.findings.exclude(state="closed"):
        if midnight(finding.due_date) <= cutoff:
            candidates.append(("finding", str(finding.pk), midnight(finding.due_date), "Follow-up due: " + finding.title))
    for item in case.requests.all():
        if item.due_date and midnight(item.due_date) <= cutoff:
            candidates.append(("request", str(item.pk), midnight(item.due_date), "Evidence request due: " + item.title))
        for document in item.documents.all():
            version = document.latest_version
            expiry = version.observed_at + timedelta(days=item.freshness_days)
            if expiry <= cutoff:
                candidates.append(("freshness", str(version.pk), expiry, "Evidence freshness expires: " + document.title))
    policy = case.monitoring_policies.order_by("-number").first()
    if policy:
        for asset in case.monitored_assets.all():
            for obs in latest_observations(asset):
                expiry = obs.observed_at + timedelta(hours=policy.freshness_hours)
                if expiry <= now:
                    candidates.append(("monitoring", str(obs.pk), expiry, "Refresh monitoring evidence: " + asset.name))
    for kind, subject, due, message in candidates:
        Reminder.objects.get_or_create(key=checksum([str(case.pk), kind, subject, due.isoformat()]), defaults={"case": case, "message": message[:300], "due_at": due})


@transaction.atomic
def acknowledge(user, reminder):
    case = lock_case(user, reminder.case)
    require_reviewer(user, case)
    return ReminderAcknowledgment.objects.get_or_create(reminder=reminder, user=user)[0]
