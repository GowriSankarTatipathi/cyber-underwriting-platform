from django.urls import path
from . import views

urlpatterns = [
    path("cases/<uuid:case_id>/ai/", views.overview, name="ai_overview"),
    path("cases/<uuid:case_id>/ai/authorize/", views.consent_create, name="ai_consent"),
    path("cases/<uuid:case_id>/ai/authorizations/<uuid:consent_id>/revoke/", views.consent_revoke, name="ai_consent_revoke"),
    path("cases/<uuid:case_id>/versions/<uuid:version_id>/extract/", views.queue, name="ai_queue"),
    path("cases/<uuid:case_id>/ai/jobs/<uuid:job_id>/", views.job_detail, name="ai_job"),
    path("cases/<uuid:case_id>/ai/jobs/<uuid:job_id>/status/", views.job_status, name="ai_job_status"),
    path("cases/<uuid:case_id>/ai/jobs/<uuid:job_id>/facts/<str:fact_key>/", views.fact_review, name="ai_fact_review"),
    path("cases/<uuid:case_id>/ai/jobs/<uuid:job_id>/draft/", views.draft_prepare, name="ai_draft_prepare"),
    path("cases/<uuid:case_id>/ai/jobs/<uuid:job_id>/<str:action>/", views.job_action, name="ai_job_action"),
    path("cases/<uuid:case_id>/ai/drafts/<uuid:draft_id>/", views.draft_detail, name="ai_draft"),
    path("cases/<uuid:case_id>/ai/drafts/<uuid:draft_id>/reject/", views.draft_reject, name="ai_draft_reject"),
]
