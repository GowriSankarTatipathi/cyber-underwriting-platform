from django.conf import settings
from django.db import models
from django.db.models import Q
from evidence.models import AppendOnly


class ReviewSession(models.Model):
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="pilot_sessions")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["reviewer"], condition=Q(ended_at__isnull=True), name="one_active_pilot_review_session")]


class ExtractionEvaluation(AppendOnly):
    result = models.ForeignKey("ai_review.ExtractionResult", on_delete=models.PROTECT, related_name="pilot_evaluations")
    expected_facts = models.PositiveIntegerField()
    correct_facts = models.PositiveIntegerField()
    correct_citations = models.PositiveIntegerField(null=True)
    extracted_facts = models.PositiveIntegerField()
    extracted_citations = models.PositiveIntegerField(null=True)
    rationale = models.TextField()
    evaluator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class FollowupEvaluation(AppendOnly):
    finding = models.ForeignKey("underwriting.Finding", on_delete=models.PROTECT, related_name="pilot_evaluations")
    first_response = models.ForeignKey("underwriting.FindingResponse", on_delete=models.PROTECT)
    sufficient = models.BooleanField()
    rationale = models.TextField()
    evaluator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class RecoveryDrill(AppendOnly):
    bundle_sha256 = models.CharField(max_length=64)
    database_tables = models.PositiveIntegerField()
    evidence_files = models.PositiveIntegerField()
    elapsed_seconds = models.FloatField()
    verified = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
