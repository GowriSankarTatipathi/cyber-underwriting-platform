from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from ai_review.models import ExtractionResult
from evidence.permissions import require_reviewer
from workspaces.permissions import get_case
from .forms import ExtractionEvaluationForm, FollowupEvaluationForm
from . import services


def live(request):
    return JsonResponse({"status": "alive"})


def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})


@login_required
def dashboard(request, case_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    return render(request, "operations/dashboard.html", {"case": case, "metrics": services.metrics(request.user, case),
        "active": case.pilot_sessions.filter(reviewer=request.user, ended_at__isnull=True).first(),
        "sessions": case.pilot_sessions.select_related("reviewer").order_by("-started_at")[:30],
        "results": ExtractionResult.objects.filter(job__case=case).select_related("job__version__document"), "findings": case.findings.filter(responses__isnull=False).distinct()})


@login_required
def export(request, case_id):
    case = get_case(request.user, case_id)
    return JsonResponse(services.metrics(request.user, case))


@login_required
@require_POST
def timer(request, case_id, action):
    case = get_case(request.user, case_id)
    try:
        services.timer(request.user, case, action, request.POST.get("notes", ""))
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    return redirect("pilot_metrics", case.pk)


@login_required
def evaluate(request, case_id, kind, item_id):
    case = get_case(request.user, case_id)
    require_reviewer(request.user, case)
    if kind == "extraction":
        obj = get_object_or_404(ExtractionResult, pk=item_id, job__case=case)
        from ai_review.permissions import require_result_access
        require_result_access(request.user, obj.job)
        form_type, callback = ExtractionEvaluationForm, services.evaluate_extraction
        intro = f"Evaluate the original extraction against the source, independently of accepted/corrected suggestions. {len(obj.facts)} extracted facts."
    else:
        obj = get_object_or_404(case.findings, pk=item_id)
        form_type, callback = FollowupEvaluationForm, services.evaluate_followup
        intro = "Assess whether the first response supplied the requested evidence. This does not close the finding."
    form = form_type(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            callback(request.user, obj, **form.cleaned_data)
            return redirect("pilot_metrics", case.pk)
        except ValidationError as error:
            form.add_error(None, error)
    return render(request, "workspaces/form.html", {"case": case, "title": "Record pilot evaluation", "intro": intro, "form": form, "button": "Save evaluation"})
