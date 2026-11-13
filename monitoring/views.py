from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from evidence.permissions import require_reviewer
from underwriting.permissions import require_decider
from underwriting.snapshots import checksum
from workspaces.permissions import get_case, insurer_role, is_owner, require_evidence_access, require_owner
from .forms import AssetForm, AuthorizationForm, ConnectionForm, InventoryForm, PolicyForm, ReconcileForm
from .models import Authorization, Connection, SyncRun
from .rules import case_material, evaluate, latest_observations, reconciliation_context, reconciliation_state
from . import services


def case_access(request, case_id, staff=False):
    case = get_case(request.user, case_id)
    require_evidence_access(request.user, case)
    if staff:
        require_reviewer(request.user, case)
    elif not insurer_role(request.user, case) and not is_owner(request.user, case):
        raise Http404
    return case


def edit(request, case, form, title, intro, save, button="Save"):
    if request.method == "POST" and form.is_valid():
        try:
            save(dict(form.cleaned_data))
            return redirect("monitoring", case.pk)
        except ValidationError as error:
            form.add_error(None, error)
    return render(request, "workspaces/form.html", {"case": case, "form": form, "title": title, "intro": intro, "button": button})


@login_required
def overview(request, case_id):
    case = case_access(request, case_id)
    staff, owner = bool(insurer_role(request.user, case)), is_owner(request.user, case)
    policy = case.monitoring_policies.order_by("-number").first()
    assets = []
    for asset in case.monitored_assets.select_related("service", "request"):
        observations = latest_observations(asset) if staff else []
        assets.append({"asset": asset, "observations": [{"observation": o, "evaluation": evaluate(o, policy) if policy else None} for o in observations],
            "reconciliations": [{"kind": kind, **reconciliation_state(asset, kind)} for kind in sorted({o.group.kind for o in observations})]})
    connections = [{"connection": conn, "authorizations": conn.authorizations.order_by("-pk"), "runs": conn.runs.order_by("-created_at")[:5] if staff else []}
        for conn in case.monitoring_connections.prefetch_related("assets")]
    return render(request, "monitoring/overview.html", {"case": case, "staff": staff, "owner": owner, "assets": assets, "connections": connections, "policy": policy,
        "reminders": case.monitoring_reminders.exclude(acknowledgments__user=request.user).order_by("due_at") if staff else []})


@login_required
def asset_create(request, case_id):
    case = case_access(request, case_id, True)
    return edit(request, case, AssetForm(case, request.POST or None), "Register a monitored asset", "Bind a known asset to its case, service and evidence request. Business authorization is required before any connector polls it.",
        lambda data: services.create_asset(request.user, case, data))


@login_required
def connection_create(request, case_id):
    case = case_access(request, case_id, True)
    require_decider(request.user, case)
    return edit(request, case, ConnectionForm(case, request.POST or None, initial={"config": {}}), "Prepare monitoring connection", "Select exact assets and the provider. The business owner must authorize the displayed scope. Provider settings are fixed for this connection; create a new one for scope changes.",
        lambda data: services.create_connection(request.user, case, **data))


@login_required
def policy_create(request, case_id):
    case = case_access(request, case_id, True)
    require_decider(request.user, case)
    return edit(request, case, PolicyForm(request.POST or None), "Publish monitoring rules", "Rules are versioned. Automatic follow-up creates an internal finding assigned to you; it does not approve evidence or contact the business.",
        lambda data: services.publish_policy(request.user, case, data), "Publish rules")


@login_required
def authorize(request, case_id, connection_id):
    case = case_access(request, case_id)
    require_owner(request.user, case)
    connection = get_object_or_404(Connection, pk=connection_id, case=case)
    return edit(request, case, AuthorizationForm(request.POST or None), "Authorize " + connection.name,
        f"Provider: {connection.get_provider_display()}. Assets: " + "; ".join(a.identifier for a in connection.assets.all()) + f". Settings: {connection.config}. Revocation stops future access; existing observations remain case history.",
        lambda data: services.authorize(request.user, connection, **data), "Authorize recurring reads")


@login_required
@require_POST
def revoke(request, case_id, authorization_id):
    case = case_access(request, case_id)
    authorization = get_object_or_404(Authorization, pk=authorization_id, connection__case=case)
    services.revoke(request.user, authorization)
    return redirect("monitoring", case.pk)


@login_required
@require_POST
def poll(request, case_id, connection_id):
    case = case_access(request, case_id, True)
    connection = get_object_or_404(Connection, pk=connection_id, case=case)
    try:
        services.queue_sync(request.user, connection)
        messages.success(request, "Poll queued or already present in this polling interval. Refresh to see the worker result.")
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    return redirect("monitoring", case.pk)


@login_required
@require_POST
def retry(request, case_id, run_id):
    case = case_access(request, case_id, True)
    run = get_object_or_404(SyncRun, pk=run_id, connection__case=case)
    try:
        services.retry_sync(request.user, run)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    return redirect("monitoring", case.pk)


@login_required
def inventory(request, case_id, asset_id):
    case = case_access(request, case_id, True)
    asset = get_object_or_404(case.monitored_assets, pk=asset_id)
    return edit(request, case, InventoryForm(request.POST or None), "Record CVE inventory: " + asset.name,
        "Record CVEs reported for this specific asset by your inventory or vulnerability tool. CISA matching checks these exact identifiers; it does not infer applicability from a product name.",
        lambda data: services.record_inventory(request.user, asset, **data))


@login_required
def reconcile(request, case_id, asset_id, kind):
    case = case_access(request, case_id, True)
    asset = get_object_or_404(case.monitored_assets, pk=asset_id)
    observations = latest_observations(asset, kind)
    if not observations:
        raise Http404
    context = reconciliation_context(asset, kind)
    return edit(request, case, ReconcileForm(observations, request.POST or None, initial={"context_sha256": checksum(context)}), "Reconcile observations: " + asset.name,
        "Compare scope, collection dates and sources. Select a preferred observation or leave it unresolved with a request for clarification. History remains intact; new observations require a fresh reconciliation.",
        lambda data: services.reconcile(request.user, asset, kind, **data))


@login_required
def history(request, case_id, asset_id):
    case = case_access(request, case_id, True)
    asset = get_object_or_404(case.monitored_assets, pk=asset_id)
    return render(request, "monitoring/history.html", {"case": case, "asset": asset, "groups": asset.observation_groups.select_related("connection").prefetch_related("observations__receipts", "alerts__finding").order_by("-last_seen")})


@login_required
def export(request, case_id):
    case = case_access(request, case_id, True)
    return JsonResponse(case_material(case))


@login_required
def renewal(request, case_id):
    case = case_access(request, case_id, True)
    if not case.renewal_of_id:
        raise Http404
    previous = case_access(request, case.renewal_of_id, True)
    if previous.applicant_id != case.applicant_id or previous.insurer_id != case.insurer_id:
        raise Http404
    def by_asset(value):
        return {(a["kind"], a["identifier"]): a for a in value["assets"]}
    before, after = by_asset(case_material(previous)), by_asset(case_material(case))
    rows = [{"identifier": key[1], "before": before.get(key), "after": after.get(key)} for key in sorted(before.keys() | after.keys())]
    return render(request, "monitoring/renewal.html", {"case": case, "previous": previous, "rows": rows})


@login_required
@require_POST
def reminders(request, case_id):
    case = case_access(request, case_id, True)
    services.refresh_reminders(case)
    return redirect("monitoring", case.pk)


@login_required
@require_POST
def acknowledge(request, case_id, reminder_id):
    case = case_access(request, case_id, True)
    services.acknowledge(request.user, get_object_or_404(case.monitoring_reminders, pk=reminder_id))
    return redirect("monitoring", case.pk)
