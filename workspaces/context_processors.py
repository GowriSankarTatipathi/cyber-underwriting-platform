from django.conf import settings


def deployment_context(request):
    return {"is_development": settings.DEBUG}
