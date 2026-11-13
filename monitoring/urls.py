from django.urls import path
from . import views

urlpatterns = [
    path("cases/<uuid:case_id>/monitoring/", views.overview, name="monitoring"),
    path("cases/<uuid:case_id>/monitoring/assets/new/", views.asset_create, name="monitoring_asset"),
    path("cases/<uuid:case_id>/monitoring/connections/new/", views.connection_create, name="monitoring_connection"),
    path("cases/<uuid:case_id>/monitoring/rules/new/", views.policy_create, name="monitoring_policy"),
    path("cases/<uuid:case_id>/monitoring/connections/<uuid:connection_id>/authorize/", views.authorize, name="monitoring_authorize"),
    path("cases/<uuid:case_id>/monitoring/authorizations/<int:authorization_id>/revoke/", views.revoke, name="monitoring_revoke"),
    path("cases/<uuid:case_id>/monitoring/connections/<uuid:connection_id>/poll/", views.poll, name="monitoring_poll"),
    path("cases/<uuid:case_id>/monitoring/runs/<uuid:run_id>/retry/", views.retry, name="monitoring_retry"),
    path("cases/<uuid:case_id>/monitoring/assets/<uuid:asset_id>/inventory/", views.inventory, name="monitoring_inventory"),
    path("cases/<uuid:case_id>/monitoring/assets/<uuid:asset_id>/history/", views.history, name="monitoring_history"),
    path("cases/<uuid:case_id>/monitoring/assets/<uuid:asset_id>/<str:kind>/reconcile/", views.reconcile, name="monitoring_reconcile"),
    path("cases/<uuid:case_id>/monitoring/export/", views.export, name="monitoring_export"),
    path("cases/<uuid:case_id>/monitoring/renewal/", views.renewal, name="monitoring_renewal"),
    path("cases/<uuid:case_id>/monitoring/reminders/refresh/", views.reminders, name="monitoring_reminders"),
    path("cases/<uuid:case_id>/monitoring/reminders/<int:reminder_id>/ack/", views.acknowledge, name="monitoring_ack"),
]
