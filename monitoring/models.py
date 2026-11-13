import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone
from evidence.models import AppendOnly


class Asset(AppendOnly):
    KINDS = [("tenant", "Entra tenant"), ("aws_resource", "AWS resource ARN"), ("ip", "Public IP address"), ("domain", "Verified email domain")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="monitored_assets")
    request = models.ForeignKey("workspaces.EvidenceRequest", on_delete=models.PROTECT)
    service = models.ForeignKey("evidence.Service", on_delete=models.PROTECT)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=KINDS)
    identifier = models.CharField(max_length=500)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["case", "kind", "identifier"], name="unique_monitored_case_asset")]

    def __str__(self):
        return f"{self.name} · {self.get_kind_display()}"


class AssetInventory(AppendOnly):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="inventories")
    cves = models.JSONField(default=list)
    source = models.CharField(max_length=240)
    observed_at = models.DateTimeField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Connection(AppendOnly):
    PROVIDERS = [("demo", "Fictional local observations"), ("entra", "Microsoft Entra ID"), ("aws_backup", "AWS Backup"),
        ("internetdb", "Shodan InternetDB (commercial license required)"), ("hibp", "Have I Been Pwned stealer alerts (licensed)"), ("kev", "CISA KEV matches against inventoried CVEs")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="monitoring_connections")
    name = models.CharField(max_length=160)
    provider = models.CharField(max_length=20, choices=PROVIDERS)
    assets = models.ManyToManyField(Asset, related_name="connections")
    config = models.JSONField(default=dict, blank=True)
    credential_ref = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Authorization(AppendOnly):
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT, related_name="authorizations")
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    expires_at = models.DateTimeField()
    scope = models.JSONField()
    rights_confirmed = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def active(self):
        from workspaces.permissions import is_owner
        return self.rights_confirmed and self.expires_at > timezone.now() and not hasattr(self, "revocation") and self.connection.case.consent_active and is_owner(self.authorized_by, self.connection.case)


class AuthorizationRevocation(AppendOnly):
    authorization = models.OneToOneField(Authorization, on_delete=models.PROTECT, related_name="revocation")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Policy(AppendOnly):
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="monitoring_policies")
    number = models.PositiveIntegerField()
    min_mfa_percent = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    max_backup_age_hours = models.PositiveIntegerField(default=24)
    freshness_hours = models.PositiveIntegerField(default=48)
    poll_minutes = models.PositiveIntegerField(default=60)
    blocked_ports = models.JSONField(default=list, blank=True)
    due_days = models.PositiveIntegerField(default=7)
    auto_followup = models.BooleanField(default=False)
    rationale = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["case", "number"], name="unique_monitoring_policy_version")]


class SyncRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT, related_name="runs")
    authorization = models.ForeignKey(Authorization, on_delete=models.PROTECT)
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    slot = models.CharField(max_length=100)
    status = models.CharField(max_length=20, default="queued")
    attempts = models.PositiveIntegerField(default=0)
    retry_base = models.PositiveIntegerField(default=0)
    token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["connection", "slot"], name="unique_monitoring_poll_slot")]


class SyncAttempt(models.Model):
    run = models.ForeignKey(SyncRun, on_delete=models.PROTECT, related_name="history")
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=20)
    error_code = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)


class ObservationGroup(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT)
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="observation_groups")
    kind = models.CharField(max_length=24)
    fingerprint = models.CharField(max_length=64)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["connection", "asset", "kind", "fingerprint"], name="unique_monitoring_observation_group")]


class Observation(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.ForeignKey(ObservationGroup, on_delete=models.PROTECT, related_name="observations")
    event_key = models.CharField(max_length=64)
    source_event_id = models.CharField(max_length=200)
    observed_at = models.DateTimeField()
    source_time_known = models.BooleanField()
    data = models.JSONField()
    provenance = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["group", "event_key"], name="unique_monitoring_source_event")]


class Receipt(AppendOnly):
    run = models.ForeignKey(SyncRun, on_delete=models.PROTECT, related_name="receipts")
    observation = models.ForeignKey(Observation, on_delete=models.PROTECT, related_name="receipts")
    collected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "observation"], name="unique_monitoring_receipt")]


class Reconciliation(AppendOnly):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="reconciliations")
    kind = models.CharField(max_length=24)
    observation = models.ForeignKey(Observation, null=True, blank=True, on_delete=models.PROTECT)
    context_sha256 = models.CharField(max_length=64)
    context = models.JSONField()
    rationale = models.TextField()
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Alert(AppendOnly):
    group = models.ForeignKey(ObservationGroup, on_delete=models.PROTECT, related_name="alerts")
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT)
    episode = models.PositiveIntegerField()
    observation = models.ForeignKey(Observation, on_delete=models.PROTECT)
    finding = models.OneToOneField("underwriting.Finding", on_delete=models.PROTECT, related_name="monitoring_source")
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["group", "policy", "episode"], name="unique_monitoring_alert_episode")]


class Reminder(AppendOnly):
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="monitoring_reminders")
    key = models.CharField(max_length=64, unique=True)
    message = models.CharField(max_length=300)
    due_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class ReminderAcknowledgment(AppendOnly):
    reminder = models.ForeignKey(Reminder, on_delete=models.PROTECT, related_name="acknowledgments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["reminder", "user"], name="unique_reminder_acknowledgment")]
