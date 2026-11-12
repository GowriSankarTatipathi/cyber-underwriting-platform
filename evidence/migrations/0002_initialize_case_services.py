from django.db import migrations


def initialize(apps, schema_editor):
    Case = apps.get_model("workspaces", "Case")
    Request = apps.get_model("workspaces", "EvidenceRequest")
    Service = apps.get_model("evidence", "Service")
    Policy = apps.get_model("evidence", "RecoveryPolicy")
    alias = schema_editor.connection.alias
    Request.objects.using(alias).filter(title="Supervised restore").update(kind="restore")
    Request.objects.using(alias).filter(title="Backup coverage").update(kind="backup")
    for case in Case.objects.using(alias).select_related("applicant", "checklist").iterator():
        service, created = Service.objects.using(alias).get_or_create(case_id=case.pk, name=case.service, defaults={
            "owner_name": case.applicant.name, "description": "Primary service from the underwriting case.", "created_by_id": case.created_by_id,
        })
        targets = case.checklist.recovery_targets
        if targets.get("duration_minutes") is not None and targets.get("data_age_minutes") is not None:
            Policy.objects.using(alias).get_or_create(service_id=service.pk, number=1, defaults={
                "checklist_id": case.checklist_id, "duration_minutes": targets["duration_minutes"],
                "data_age_minutes": targets["data_age_minutes"], "created_by_id": case.created_by_id,
                "required_scope": f"Restore {service.name}, its required dependencies, and verify the service is usable.",
            })


class Migration(migrations.Migration):
    dependencies = [("evidence", "0001_initial"), ("workspaces", "0003_evidencerequest_kind")]
    operations = [migrations.RunPython(initialize, migrations.RunPython.noop)]
