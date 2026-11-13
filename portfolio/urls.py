from django.urls import path
from . import views

urlpatterns = [
    path("cases/<uuid:case_id>/portfolio/", views.overview, name="portfolio"),
    path("cases/<uuid:case_id>/portfolio/authorize/", views.authorize, name="portfolio_authorize"),
    path("cases/<uuid:case_id>/portfolio/permissions/<int:permission_id>/revoke/", views.revoke_permission, name="portfolio_revoke"),
    path("cases/<uuid:case_id>/portfolio/outcomes/new/", views.outcome_create, name="portfolio_outcome"),
    path("cases/<uuid:case_id>/portfolio/dependencies/new/", views.dependency_create, name="portfolio_dependency"),
    path("cases/<uuid:case_id>/portfolio/dependencies/<int:dependency_id>/retire/", views.dependency_retire, name="portfolio_retire"),
    path("cases/<uuid:case_id>/portfolio/validation/new/", views.validation_create, name="portfolio_validate"),
    path("cases/<uuid:case_id>/portfolio/validation/<uuid:run_id>/", views.validation_detail, name="portfolio_validation_detail"),
    path("cases/<uuid:case_id>/portfolio/validation/<uuid:run_id>/export/", views.validation_detail, {"export": True}, name="portfolio_validation_export"),
    path("cases/<uuid:case_id>/capacity/relationships/new/", views.relationship_create, name="capacity_relationship"),
    path("cases/<uuid:case_id>/capacity/relationships/<int:relationship_id>/revoke/", views.relationship_revoke, name="capacity_relationship_revoke"),
    path("cases/<uuid:case_id>/capacity/new/", views.submission_create, name="capacity_prepare"),
    path("cases/<uuid:case_id>/capacity/<uuid:submission_id>/", views.submission_detail, name="capacity_detail"),
    path("cases/<uuid:case_id>/capacity/<uuid:submission_id>/approve/", views.submission_action, {"action": "consent"}, name="capacity_consent"),
    path("cases/<uuid:case_id>/capacity/<uuid:submission_id>/release/", views.submission_action, {"action": "release"}, name="capacity_release"),
    path("cases/<uuid:case_id>/capacity/<uuid:submission_id>/revoke/", views.submission_revoke, name="capacity_revoke"),
    path("capacity/", views.partner_inbox, name="capacity_inbox"),
    path("capacity/submissions/<uuid:submission_id>/", views.partner_detail, name="capacity_partner"),
    path("capacity/submissions/<uuid:submission_id>/export/", views.partner_detail, {"export": True}, name="capacity_export"),
]
