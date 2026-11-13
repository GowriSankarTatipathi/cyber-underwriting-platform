from django.shortcuts import render


def csrf_failure(request, reason=""):
    # Never weaken Origin or token validation to make an embedded preview work.
    return render(request, "errors/csrf.html", status=403)


def not_found(request, exception=None):
    return render(request, "errors/404.html", status=404)


def forbidden(request, exception=None):
    return render(request, "errors/403.html", status=403)


def server_error(request):
    return render(request, "errors/500.html", status=500)
