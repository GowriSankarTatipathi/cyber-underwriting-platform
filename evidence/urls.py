from django.urls import path
from . import views

urlpatterns = [
    path("cases/<uuid:case_id>/services/", views.inventory, name="service_inventory"),
    path("cases/<uuid:case_id>/services/new/", views.service_create, name="service_create"),
    path("cases/<uuid:case_id>/services/<uuid:service_id>/targets/", views.policy_create, name="policy_create"),
    path("cases/<uuid:case_id>/evidence/<uuid:request_id>/", views.request_detail, name="evidence_request"),
    path("cases/<uuid:case_id>/evidence/<uuid:request_id>/upload/", views.upload, name="evidence_upload"),
    path("cases/<uuid:case_id>/evidence/<uuid:request_id>/documents/<uuid:document_id>/upload/", views.upload, name="evidence_new_version"),
    path("cases/<uuid:case_id>/evidence/<uuid:request_id>/assess/", views.control_review, name="control_review"),
    path("cases/<uuid:case_id>/evidence/<uuid:request_id>/backups/new/", views.backup_create, name="backup_create"),
    path("cases/<uuid:case_id>/evidence/<uuid:request_id>/restores/new/", views.restore_create, name="restore_create"),
    path("cases/<uuid:case_id>/versions/<uuid:version_id>/download/", views.download, name="evidence_download"),
    path("cases/<uuid:case_id>/versions/<uuid:version_id>/review/", views.version_review, name="version_review"),
    path("cases/<uuid:case_id>/versions/<uuid:version_id>/scan/", views.rescan, name="evidence_rescan"),
    path("cases/<uuid:case_id>/restores/<uuid:test_id>/", views.restore_detail, name="restore_detail"),
]
