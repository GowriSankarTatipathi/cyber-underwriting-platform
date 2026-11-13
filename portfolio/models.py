import uuid
from django.conf import settings
from django.db import models
from evidence.models import AppendOnly


class Permission(AppendOnly):
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="portfolio_permissions")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    expires_at = models.DateTimeField()
    scope = models.TextField(default="Use permitted incident/loss outcomes and provider dependencies for this insurer's internal portfolio research.")
    created_at = models.DateTimeField(auto_now_add=True)


class PermissionRevocation(AppendOnly):
    permission = models.OneToOneField(Permission, on_delete=models.PROTECT, related_name="revocation")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Outcome(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="portfolio_outcomes")
    decision = models.ForeignKey("underwriting.Decision", on_delete=models.PROTECT, related_name="observed_outcomes")
    permission = models.ForeignKey(Permission, on_delete=models.PROTECT)
    period_start = models.DateField()
    period_end = models.DateField()
    incidents = models.PositiveIntegerField()
    incurred_loss = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3)
    source = models.CharField(max_length=240)
    maturity = models.CharField(max_length=16, choices=[("developing", "Developing loss estimate"), ("mature", "Complete observed period, sufficiently developed losses")])
    rationale = models.TextField()
    supersedes = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="replacement")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Provider(AppendOnly):
    insurer = models.ForeignKey("workspaces.Organization", on_delete=models.PROTECT)
    name = models.CharField(max_length=160)
    canonical_key = models.CharField(max_length=160)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["insurer", "canonical_key"], name="unique_portfolio_provider")]

    def __str__(self):
        return self.name


class Dependency(AppendOnly):
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="provider_dependencies")
    service = models.ForeignKey("evidence.Service", on_delete=models.PROTECT)
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT)
    permission = models.ForeignKey(Permission, on_delete=models.PROTECT)
    critical = models.BooleanField()
    source = models.CharField(max_length=240)
    observed_at = models.DateTimeField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class DependencyRetirement(AppendOnly):
    dependency = models.OneToOneField(Dependency, on_delete=models.PROTECT, related_name="retirement")
    rationale = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ValidationRun(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    insurer = models.ForeignKey("workspaces.Organization", on_delete=models.PROTECT)
    cutoff = models.DateField()
    currency = models.CharField(max_length=3)
    dataset = models.JSONField()
    dataset_sha256 = models.CharField(max_length=64)
    metrics = models.JSONField()
    model_version = models.CharField(max_length=60, default="historical-risk-band-baseline-v1")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Relationship(AppendOnly):
    insurer = models.ForeignKey("workspaces.Organization", on_delete=models.PROTECT)
    partner_name = models.CharField(max_length=160)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="capacity_relationships")
    contract_reference = models.CharField(max_length=240)
    expires_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.partner_name


class RelationshipRevocation(AppendOnly):
    relationship = models.OneToOneField(Relationship, on_delete=models.PROTECT, related_name="revocation")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Submission(AppendOnly):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey("workspaces.Case", on_delete=models.PROTECT, related_name="capacity_submissions")
    relationship = models.ForeignKey(Relationship, on_delete=models.PROTECT)
    report = models.ForeignKey("underwriting.Report", on_delete=models.PROTECT)
    payload = models.JSONField()
    sha256 = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class SubmissionConsent(AppendOnly):
    submission = models.OneToOneField(Submission, on_delete=models.PROTECT, related_name="consent")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    payload_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class SubmissionRelease(AppendOnly):
    submission = models.OneToOneField(Submission, on_delete=models.PROTECT, related_name="release")
    rationale = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class SubmissionRevocation(AppendOnly):
    submission = models.OneToOneField(Submission, on_delete=models.PROTECT, related_name="revocation")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class PartnerResponse(AppendOnly):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="responses")
    status = models.CharField(max_length=20, choices=[("received", "Received"), ("clarification", "Clarification requested"), ("indicative", "Indicative interest only"), ("declined", "Declined")])
    message = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
