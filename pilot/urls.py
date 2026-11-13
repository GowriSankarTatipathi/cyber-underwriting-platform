import json
from pathlib import Path

from django.shortcuts import render
from django.urls import include, path

from .assessment import assess_restore


def pilot_case(request):
    fixture = Path(__file__).parent / "data" / "pilot.json"
    case = json.loads(fixture.read_text(encoding="utf-8"))
    selected = request.GET.get("test", "initial")
    if selected not in case["tests"]:
        selected = "initial"
    test = case["tests"][selected]
    result = assess_restore(test, case["targets"])
    return render(request, "pilot.html", {
        "case": case,
        "test": test,
        "selected": selected,
        "result": result,
    })


urlpatterns = [
    path("", include("portfolio.urls")),
    path("", include("operations.urls")),
    path("", include("monitoring.urls")),
    path("demo/", pilot_case, name="pilot_case"),
    path("accounts/", include("allauth.urls")),
    path("", include("ai_review.urls")),
    path("", include("underwriting.urls")),
    path("", include("evidence.urls")),
    path("", include("workspaces.urls")),
]
