from allauth.mfa.models import Authenticator
from allauth.account.internal.flows.login import get_authentication_records
from django.shortcuts import redirect
from django.urls import reverse
from urllib.parse import urlencode


class WorkspaceSecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        protected = not request.path.startswith(("/accounts/", "/static/", "/demo/"))
        if protected and request.user.is_authenticated:
            if not Authenticator.objects.filter(user=request.user, type="totp").exists():
                request.session["workspace_return_to"] = request.get_full_path()
                return redirect("mfa_activate_totp")
            if not any(record.get("method") == "mfa" for record in get_authentication_records(request, request.user)):
                return redirect(reverse("mfa_reauthenticate") + "?" + urlencode({"next": request.get_full_path()}))
        response = self.get_response(request)
        if not request.path.startswith("/static/"):
            response["Cache-Control"] = "no-store, private"
        return response
