import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from evidence.models import AppendOnly


class Finding(models.Model):
    KINDS = [("evidence", "Evidence request"), ("fix", "Fix required"), ("retest", "Restore retest")]
    PRIORITIES = [("critical", "Critical"), ("high", "High"), ("medium", "Medium"), ("low", "Low")]
    STATES = [("open", "Open"), ("submitted", "Response awaiting review"), ("closed", "Closed")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="findings")
    request = models.ForeignKey("workspaces.EvidenceRequest", on_delete=models.PROTECT, related_name="findings")
    title = models.CharField(max_length=180)
    description = models.TextField()
    kind = models.CharField(max_length=16, choices=KINDS)
    priority = models.CharField(max_length=16, choices=PRIORITIES)
    priority_reason = models.TextField()
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_findings")
    due_date = models.DateField()
    retest_of = models.ForeignKey("evidence.RestoreTest", null=True, blank=True, on_delete=models.PROTECT)
    state = models.CharField(max_length=16, choices=STATES, default="open")
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due_date", "created_at"]

    @property
    def overdue(self):
        return self.state != "closed" and self.due_date < timezone.localdate()

    def __str__(self):
        return self.title


class FindingEvent(AppendOnly):
    finding = models.ForeignKey(Finding, on_delete=models.PROTECT, related_name="events")
    action = models.CharField(max_length=32)
    rationale = models.TextField()
    snapshot = models.JSONField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


class FindingResponse(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    finding = models.ForeignKey(Finding, on_delete=models.PROTECT, related_name="responses")
    message = models.TextField()
    evidence_versions = models.ManyToManyField("evidence.EvidenceVersion")
    restore_test = models.ForeignKey("evidence.RestoreTest", null=True, blank=True, on_delete=models.PROTECT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class ExceptionApproval(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    finding = models.ForeignKey(Finding, on_delete=models.PROTECT, related_name="exceptions")
    rationale = models.TextField()
    compensating_controls = models.TextField()
    expires_at = models.DateTimeField()
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def active(self):
        return self.expires_at > timezone.now() and not hasattr(self, "revocation")


class ExceptionRevocation(AppendOnly):
    exception = models.OneToOneField(ExceptionApproval, on_delete=models.PROTECT, related_name="revocation")
    rationale = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ReviewSnapshot(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="decision_snapshots")
    payload = models.JSONField()
    sha256 = models.CharField(max_length=64)
    base_decision_number = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Decision(AppendOnly):
    OUTCOMES = [("approved", "Approved"), ("conditional", "Approved with conditions"), ("deferred", "Deferred"), ("declined", "Declined")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="decisions")
    number = models.PositiveIntegerField()
    snapshot = models.OneToOneField(ReviewSnapshot, on_delete=models.PROTECT, related_name="decision")
    outcome = models.CharField(max_length=16, choices=OUTCOMES)
    rationale = models.TextField()
    conditions = models.JSONField(default=list, blank=True)
    rule_version = models.CharField(max_length=40, default="underwriting-v1")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["case", "number"], name="unique_case_decision_number")]


class Report(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.ForeignKey(Decision, on_delete=models.PROTECT, related_name="reports")
    title = models.CharField(max_length=180)
    summary = models.TextField()
    payload = models.JSONField()
    sha256 = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def approved(self):
        return hasattr(self, "approval") and not hasattr(self, "withdrawal")


class ReportApproval(AppendOnly):
    report = models.OneToOneField(Report, on_delete=models.PROTECT, related_name="approval")
    rationale = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ReportWithdrawal(AppendOnly):
    report = models.OneToOneField(Report, on_delete=models.PROTECT, related_name="withdrawal")
    rationale = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ReportShare(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(Report, on_delete=models.PROTECT, related_name="shares")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="received_reports")
    expires_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def active(self):
        return self.expires_at > timezone.now() and not hasattr(self, "revocation") and self.report.approved


class ShareRevocation(AppendOnly):
    share = models.OneToOneField(ReportShare, on_delete=models.PROTECT, related_name="revocation")
    rationale = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
