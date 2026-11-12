from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("underwriter/", views.dashboard, {"workspace": "underwriter"}, name="underwriter"),
    path("portal/", views.dashboard, {"workspace": "portal"}, name="portal"),
    path("cases/new/", views.case_create, name="case_create"),
    path("cases/<uuid:case_id>/", views.case_detail, name="case"),
    path("cases/<uuid:case_id>/assign/", views.assign_case, name="assign_case"),
    path("cases/<uuid:case_id>/notes/", views.save_notes, name="save_notes"),
    path("cases/<uuid:case_id>/invite/", views.invite, name="invite"),
    path("cases/<uuid:case_id>/consent/", views.consent, name="consent"),
    path("cases/<uuid:case_id>/grants/<uuid:grant_id>/revoke/", views.revoke_grant, name="revoke_grant"),
    path("cases/<uuid:case_id>/invitations/<uuid:invitation_id>/revoke/", views.revoke_invitation, name="revoke_invitation"),
    path("cases/<uuid:case_id>/requests/<uuid:request_id>/assign/", views.assign_request, name="assign_request"),
    path("cases/<uuid:case_id>/files/<uuid:file_id>/", views.download_file, name="download_file"),
    path("cases/<uuid:case_id>/export/", views.export_case, name="export_case"),
    path("cases/<uuid:case_id>/jobs/<uuid:job_id>/", views.job_detail, name="job_detail"),
    path("invitations/<str:token>/", views.invitation, name="invitation"),
]
