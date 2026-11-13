from datetime import datetime, timedelta
from decimal import Decimal
from django.utils import timezone
from underwriting.snapshots import checksum, normalize
from .models import Observation


def latest_observations(asset, kind=None):
    query = Observation.objects.filter(group__asset=asset).select_related("group__connection", "group__asset")
    if kind:
        query = query.filter(group__kind=kind)
    latest = {}
    for observation in query.order_by("-observed_at", "-created_at", "-pk"):
        latest.setdefault((observation.group.connection_id, observation.group.kind), observation)
    return list(latest.values())


def reconciliation_context(asset, kind):
    return sorted([{"id": str(o.pk), "connection": str(o.group.connection_id), "observed_at": o.observed_at.isoformat(), "data": o.data}
        for o in latest_observations(asset, kind)], key=lambda row: row["id"])


def reconciliation_state(asset, kind):
    context = reconciliation_context(asset, kind)
    conflict = len({checksum(row["data"]) for row in context}) > 1
    review = asset.reconciliations.filter(kind=kind).order_by("-pk").first()
    current = review and review.context_sha256 == checksum(context)
    return {"conflict": conflict, "review": review, "current": bool(current), "context": context,
        "status": "Reconciled" if current and review.observation_id else "Clarification required" if current else "Conflicting observations" if conflict else "No conflicting current observations"}


def evaluate(observation, policy, at=None):
    at = at or timezone.now()
    data, reasons = observation.data, []
    stale = observation.observed_at < at - timedelta(hours=policy.freshness_hours)
    if stale:
        reasons.append("The source observation is outside the insurer's freshness window; obtain current evidence.")
    kind = observation.group.kind
    if kind == "identity":
        if data["total"] == 0:
            reasons.append("The identity report contains no eligible accounts; coverage is unknown.")
        elif Decimal(data["registered"]) * 100 / data["total"] < policy.min_mfa_percent:
            reasons.append(f"MFA registration is {data['registered']}/{data['total']} accounts, below the {policy.min_mfa_percent}% target. Verify enforcement separately.")
    elif kind == "backup":
        if data["status"] != "success":
            reasons.append(f"The latest backup job status is {data['status']}; confirm job health and coverage.")
        if not data["completed_at"] or datetime.fromisoformat(data["completed_at"]) < at - timedelta(hours=policy.max_backup_age_hours):
            reasons.append("No completed latest backup within the insurer's backup-age target.")
    elif kind == "exposure":
        ports = sorted(set(data["ports"]) & set(policy.blocked_ports))
        if ports:
            reasons.append("Review observed ports restricted by the insurer: " + ", ".join(map(str, ports)) + ".")
        if data["cves"]:
            reasons.append("Review potential vulnerability matches: " + ", ".join(data["cves"][:20]) + ".")
    elif kind == "credential" and data["affected_aliases"]:
        reasons.append(f"The licensed feed reports historical stealer exposure for {data['affected_aliases']} aliases. Investigate credential hygiene; this does not establish current compromise.")
    elif kind == "vulnerability" and data["cves"]:
        reasons.append("Inventoried asset CVEs appear in CISA KEV: " + ", ".join(data["cves"][:20]) + ". Verify applicability and remediation.")
    caveats = []
    if not observation.source_time_known:
        caveats.append("The provider does not supply a complete source observation date; the displayed date is collection time.")
    return {"status": "Follow-up indicated" if reasons else "No configured trigger; human verification remains required", "reasons": reasons,
        "stale": stale, "caveats": caveats, "rule_version": "monitoring-v1", "policy_id": policy.pk, "policy_number": policy.number}


def case_material(case):
    policy = case.monitoring_policies.order_by("-number").first()
    assets = []
    for asset in case.monitored_assets.order_by("pk"):
        observations = latest_observations(asset)
        assets.append({"id": asset.pk, "identifier": asset.identifier, "kind": asset.kind,
            "observations": [{"id": o.pk, "connection_id": o.group.connection_id, "source": o.provenance, "data": o.data,
                "observed_at": o.observed_at, "source_time_known": o.source_time_known, "evaluation": evaluate(o, policy) if policy else None} for o in sorted(observations, key=lambda o: str(o.pk))],
            "reconciliations": [{"id": r.pk, "context": r.context, "selected": r.observation_id, "rationale": r.rationale, "reviewer_id": r.reviewer_id} for r in asset.reconciliations.order_by("pk")]})
    return normalize({"policy_id": policy.pk if policy else None, "policy_number": policy.number if policy else None, "assets": assets})
