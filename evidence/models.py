import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class AppendOnly(models.Model):
    """Application records are appended, never edited through a review form."""
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("This record is immutable. Create a new version or review.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Evidence history cannot be deleted through the application.")


class Service(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="services")
    name = models.CharField(max_length=160)
    owner_name = models.CharField(max_length=160)
    dependencies = models.TextField(blank=True)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["case", "name"], name="unique_case_service")]
        ordering = ["created_at"]

    def __str__(self):
        return self.name

    @property
    def current_policy(self):
        return self.policies.order_by("-number").first()


class RecoveryPolicy(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="policies")
    checklist = models.ForeignKey("workspaces.ChecklistVersion", on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    duration_minutes = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    data_age_minutes = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    required_scope = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["service", "number"], name="unique_recovery_policy_version"),
            models.CheckConstraint(condition=Q(duration_minutes__gte=0) & Q(data_age_minutes__gte=0), name="nonnegative_recovery_targets"),
        ]

    def __str__(self):
        return f"{self.service.name} · targets v{self.number}"


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey("workspaces.EvidenceRequest", on_delete=models.PROTECT, related_name="documents")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="documents")
    title = models.CharField(max_length=180)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def latest_version(self):
        return self.versions.order_by("-number").first()

    def __str__(self):
        return self.title


class EvidenceVersion(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    submission_key = models.UUIDField(unique=True)
    fingerprint = models.CharField(max_length=64)
    original_name = models.CharField(max_length=180)
    content_type = models.CharField(max_length=100)
    byte_size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_backend = models.CharField(max_length=16, choices=[("local", "Private local development"), ("s3", "Private S3")])
    storage_key = models.CharField(max_length=300, unique=True)
    storage_bucket = models.CharField(max_length=160, blank=True)
    object_version = models.CharField(max_length=1024, blank=True)
    source = models.CharField(max_length=200)
    scope = models.TextField()
    observed_at = models.DateTimeField()
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["document", "number"], name="unique_document_version"),
            models.UniqueConstraint(fields=["document", "fingerprint"], name="unique_document_payload"),
        ]

    @property
    def latest_scan(self):
        return self.scans.order_by("-created_at", "-pk").first()

    @property
    def latest_review(self):
        return self.reviews.order_by("-created_at", "-pk").first()

    def __str__(self):
        return f"{self.document.title} · v{self.number}"


class FileScan(AppendOnly):
    version = models.ForeignKey(EvidenceVersion, on_delete=models.PROTECT, related_name="scans")
    status = models.CharField(max_length=20, choices=[("clean", "File checks passed"), ("quarantined", "Quarantined"), ("rejected", "Unsafe file")])
    engine = models.CharField(max_length=80)
    detail = models.CharField(max_length=300)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class EvidenceReview(AppendOnly):
    version = models.ForeignKey(EvidenceVersion, on_delete=models.PROTECT, related_name="reviews")
    decision = models.CharField(max_length=20, choices=[("accepted", "Accept evidence"), ("rejected", "Reject evidence"), ("needs_review", "Request clarification")])
    scope_confirmed = models.BooleanField(default=False)
    rationale = models.TextField()
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ControlAssessment(AppendOnly):
    request = models.ForeignKey("workspaces.EvidenceRequest", on_delete=models.PROTECT, related_name="assessments")
    result = models.CharField(max_length=20, choices=[("verified", "Verified"), ("failed", "Failed"), ("needs_review", "Needs review")])
    rationale = models.TextField()
    evidence_versions = models.ManyToManyField(EvidenceVersion, related_name="control_assessments")
    review_snapshot = models.JSONField(default=dict)
    rule_version = models.CharField(max_length=40, default="evidence-v1")
    checklist_version = models.CharField(max_length=40)
    freshness_days = models.PositiveIntegerField()
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class BackupJob(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey("workspaces.EvidenceRequest", on_delete=models.PROTECT, related_name="backup_jobs")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="backup_jobs")
    observed_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=[("success", "Successful backup job"), ("failed", "Failed backup job"), ("partial", "Partial backup"), ("unknown", "Unknown")])
    coverage = models.TextField()
    source_version = models.ForeignKey(EvidenceVersion, on_delete=models.PROTECT)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class RestoreTest(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey("workspaces.EvidenceRequest", on_delete=models.PROTECT, related_name="restore_tests")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="restore_tests")
    policy = models.ForeignKey(RecoveryPolicy, on_delete=models.PROTECT)
    retest_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="retests")
    performed_at = models.DateTimeField()
    scope = models.TextField()
    service_snapshot = models.JSONField(default=dict)
    supervised_by = models.CharField(max_length=180)
    duration_minutes = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal("0"))])
    data_age_minutes = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal("0"))])
    functional_checks_passed = models.BooleanField(null=True, blank=True)
    functional_checks = models.TextField(blank=True)
    evidence_versions = models.ManyToManyField(EvidenceVersion, related_name="restore_tests")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-performed_at", "-created_at"]
        constraints = [models.CheckConstraint(condition=(Q(duration_minutes__isnull=True) | Q(duration_minutes__gte=0)) & (Q(data_age_minutes__isnull=True) | Q(data_age_minutes__gte=0)), name="nonnegative_restore_measurements")]

    @property
    def latest_review(self):
        return self.reviews.order_by("-created_at", "-pk").first()

    def __str__(self):
        return f"{self.service.name} · {self.performed_at:%Y-%m-%d %H:%M} UTC"


class RestoreReview(AppendOnly):
    test = models.ForeignKey(RestoreTest, on_delete=models.PROTECT, related_name="reviews")
    scope_confirmed = models.BooleanField(null=True)
    result = models.CharField(max_length=24)
    reasons = models.JSONField(default=list)
    evidence_snapshot = models.JSONField(default=dict)
    rule_version = models.CharField(max_length=40, default="restore-v1")
    rationale = models.TextField()
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
