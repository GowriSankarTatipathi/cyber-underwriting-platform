import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from evidence.models import AppendOnly


class ProcessingConsent(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="ai_consents")
    provider = models.CharField(max_length=24, default="openai")
    scope = models.TextField(default="Extracted text from case evidence selected by an authorized insurer reviewer; no automatic decisions or messages.")
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def active(self):
        from workspaces.permissions import is_owner
        return self.expires_at > timezone.now() and not hasattr(self, "revocation") and self.case.consent_active and is_owner(self.authorized_by, self.case)


class ConsentRevocation(AppendOnly):
    consent = models.OneToOneField(ProcessingConsent, on_delete=models.PROTECT, related_name="revocation")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ReviewJob(models.Model):
    STATES = [("queued", "Queued"), ("running", "Processing"), ("retry_wait", "Retry scheduled"),
              ("completed", "Ready for human review"), ("failed", "Processing failed"), ("blocked", "Access or file checks blocked processing"), ("cancelled", "Cancelled")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="ai_jobs")
    version = models.ForeignKey("evidence.EvidenceVersion", on_delete=models.PROTECT, related_name="ai_jobs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    consent = models.ForeignKey(ProcessingConsent, on_delete=models.PROTECT, null=True, blank=True)
    policy = models.ForeignKey("evidence.RecoveryPolicy", on_delete=models.PROTECT, null=True, blank=True)
    provider = models.CharField(max_length=24)
    model_name = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=40, default="evidence-extraction-v1")
    schema_version = models.CharField(max_length=40, default="evidence-facts-v1")
    dedupe_key = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=20, choices=STATES, default="queued")
    attempts = models.PositiveIntegerField(default=0)
    retry_base = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=60, blank=True)
    error_message = models.CharField(max_length=300, blank=True)
    execution_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def stalled(self):
        return self.status == "running" and self.lease_expires_at and self.lease_expires_at <= timezone.now()


class ProcessingAttempt(models.Model):
    job = models.ForeignKey(ReviewJob, on_delete=models.PROTECT, related_name="runs")
    number = models.PositiveIntegerField()
    execution_token = models.UUIDField(unique=True)
    status = models.CharField(max_length=20, default="running")
    error_code = models.CharField(max_length=60, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "number"], name="unique_ai_processing_attempt")]


class ExtractionResult(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.OneToOneField(ReviewJob, on_delete=models.PROTECT, related_name="result")
    source_sha256 = models.CharField(max_length=64)
    preparation_version = models.CharField(max_length=40, default="document-preparation-v1")
    passages = models.JSONField(default=list)
    facts = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    provider_response_id = models.CharField(max_length=200, blank=True)
    actual_model = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)


class FactReview(AppendOnly):
    result = models.ForeignKey(ExtractionResult, on_delete=models.PROTECT, related_name="reviews")
    fact_key = models.CharField(max_length=16)
    action = models.CharField(max_length=16, choices=[("accept", "Accept suggestion"), ("correct", "Correct suggestion"), ("reject", "Reject suggestion")])
    value = models.JSONField(null=True, blank=True)
    citations = models.JSONField(default=list, blank=True)
    rationale = models.TextField()
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["result", "fact_key", "created_at"])]


class FollowUpDraft(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    result = models.ForeignKey(ExtractionResult, on_delete=models.PROTECT, related_name="drafts")
    context_sha256 = models.CharField(max_length=64)
    review_snapshot = models.JSONField()
    content = models.JSONField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class DraftDisposition(AppendOnly):
    draft = models.OneToOneField(FollowUpDraft, on_delete=models.PROTECT, related_name="disposition")
    action = models.CharField(max_length=16, choices=[("publish", "Sent to assigned workspace"), ("reject", "Rejected")])
    finding = models.OneToOneField("underwriting.Finding", on_delete=models.PROTECT, null=True, blank=True, related_name="ai_source")
    rationale = models.TextField()
    published_content = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
