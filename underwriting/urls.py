from django.urls import path
from . import views

urlpatterns = [
    path("cases/<uuid:case_id>/workflow/", views.workflow, name="workflow"),
    path("cases/<uuid:case_id>/findings/new/", views.finding_edit, name="finding_create"),
    path("cases/<uuid:case_id>/findings/<uuid:finding_id>/", views.finding_detail, name="finding_detail"),
    path("cases/<uuid:case_id>/findings/<uuid:finding_id>/edit/", views.finding_edit, name="finding_edit"),
    path("cases/<uuid:case_id>/findings/<uuid:finding_id>/respond/", views.finding_respond, name="finding_respond"),
    path("cases/<uuid:case_id>/findings/<uuid:finding_id>/review/", views.finding_review, name="finding_review"),
    path("cases/<uuid:case_id>/findings/<uuid:finding_id>/exceptions/new/", views.exception_create, name="exception_create"),
    path("cases/<uuid:case_id>/exceptions/<uuid:exception_id>/revoke/", views.exception_revoke, name="exception_revoke"),
    path("cases/<uuid:case_id>/decisions/prepare/", views.decision_prepare, name="decision_prepare"),
    path("cases/<uuid:case_id>/decisions/review/<uuid:snapshot_id>/", views.decision_review, name="decision_review"),
    path("cases/<uuid:case_id>/decisions/<uuid:decision_id>/", views.decision_detail, name="decision_detail"),
    path("cases/<uuid:case_id>/decisions/<uuid:decision_id>/reports/new/", views.report_create, name="report_create"),
    path("cases/<uuid:case_id>/reports/<uuid:report_id>/", views.report_detail, name="report_detail"),
    path("cases/<uuid:case_id>/reports/<uuid:report_id>/share/", views.report_share, name="report_share"),
    path("cases/<uuid:case_id>/reports/<uuid:report_id>/<str:action>/", views.report_action, name="report_action"),
    path("cases/<uuid:case_id>/report-shares/<uuid:share_id>/revoke/", views.share_revoke, name="share_revoke"),
    path("shared-reports/<uuid:share_id>/", views.shared_report, name="shared_report"),
    path("cases/<uuid:case_id>/renewal/", views.renewal_create, name="renewal_create"),
    path("cases/<uuid:case_id>/history/", views.history, name="case_history"),
]
