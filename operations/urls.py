from django.urls import path
from . import views

urlpatterns = [
    path("health/live/", views.live, name="health_live"),
    path("health/ready/", views.ready, name="health_ready"),
    path("cases/<uuid:case_id>/pilot/", views.dashboard, name="pilot_metrics"),
    path("cases/<uuid:case_id>/pilot/export/", views.export, name="pilot_metrics_export"),
    path("cases/<uuid:case_id>/pilot/timer/<str:action>/", views.timer, name="pilot_timer"),
    path("cases/<uuid:case_id>/pilot/evaluate/<str:kind>/<uuid:item_id>/", views.evaluate, name="pilot_evaluate"),
]
