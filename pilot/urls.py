import json
from pathlib import Path

from django.shortcuts import render
from django.urls import path

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


urlpatterns = [path("", pilot_case, name="pilot_case")]
