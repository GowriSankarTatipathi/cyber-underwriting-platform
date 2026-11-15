"""Case assessment extensions. Synthetic research is kept outside live outcome tables."""
from django.conf import settings
from django.db import models
from evidence.models import AppendOnly


class Record(AppendOnly):
    case = models.ForeignKey('workspaces.Case', on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class DemoCase(Record):
    case = models.OneToOneField('workspaces.Case', on_delete=models.PROTECT, related_name='demo_profile')
    label = models.CharField(max_length=160, default='Fictional full-platform demonstration')


class PolicyRecord(Record):
    reference = models.CharField(max_length=100)
    start = models.DateField()
    end = models.DateField()
    currency = models.CharField(max_length=3)
    limit = models.DecimalField(max_digits=16, decimal_places=2)
    deductible = models.DecimalField(max_digits=16, decimal_places=2)
    source = models.CharField(max_length=240)
    decision = models.ForeignKey('underwriting.Decision', null=True, blank=True, on_delete=models.PROTECT)
    supersedes = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT, related_name='replacement')

    def __str__(self):
        return f'{self.reference} ({self.start} to {self.end})'


class Claim(Record):
    policy = models.ForeignKey(PolicyRecord, on_delete=models.PROTECT, related_name='claims')
    reference = models.CharField(max_length=100)
    incident_date = models.DateField()
    incurred_loss = models.DecimalField(max_digits=16, decimal_places=2)
    downtime_hours = models.DecimalField(max_digits=10, decimal_places=2)
    maturity = models.CharField(max_length=16, choices=[('developing', 'Developing'), ('mature', 'Sufficiently developed')])
    source = models.CharField(max_length=240)
    explanation = models.TextField()
    supersedes = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT, related_name='replacement')

    def __str__(self):
        return f'{self.reference} · {self.incident_date}'


class ImportReceipt(Record):
    digest = models.CharField(max_length=64)
    summary = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['case', 'digest'], name='assurance_import_once')]


class PriorityRule(Record):
    weights = models.JSONField()
    rationale = models.TextField()


class PriorityAssessment(Record):
    finding = models.ForeignKey('underwriting.Finding', on_delete=models.PROTECT, related_name='priority_assessments')
    rule = models.ForeignKey(PriorityRule, on_delete=models.PROTECT)
    inputs = models.JSONField()
    score = models.PositiveIntegerField()
    suggested = models.CharField(max_length=16)
    selected = models.CharField(max_length=16)
    rationale = models.TextField()
    finding_revision = models.PositiveIntegerField()


class Comparison(Record):
    version = models.ForeignKey('evidence.EvidenceVersion', on_delete=models.PROTECT)
    observation = models.ForeignKey('monitoring.Observation', on_delete=models.PROTECT)
    topic = models.CharField(max_length=180)
    internal_value = models.CharField(max_length=500)
    external_value = models.CharField(max_length=500)
    context = models.JSONField()


class ComparisonReview(Record):
    comparison = models.ForeignKey(Comparison, on_delete=models.PROTECT, related_name='reviews')
    disposition = models.CharField(max_length=20, choices=[('clarify', 'Request clarification'), ('internal', 'Prefer internal evidence'), ('external', 'Prefer external observation'), ('explained', 'Difference explained')])
    rationale = models.TextField()


class PublishedRisk(Record):
    finding = models.OneToOneField('underwriting.Finding', on_delete=models.PROTECT, related_name='published_risk')
    observation = models.ForeignKey('monitoring.Observation', on_delete=models.PROTECT, null=True, blank=True)
    category = models.CharField(max_length=24, choices=[('exposure', 'External exposure'), ('credential', 'Credential exposure'), ('vulnerability', 'Vulnerability')])
    service = models.ForeignKey('evidence.Service', on_delete=models.PROTECT)
    summary = models.TextField()
    recommendation = models.TextField()


class Investigation(Record):
    risk = models.ForeignKey(PublishedRisk, on_delete=models.PROTECT, related_name='investigations')
    action = models.CharField(max_length=24, choices=[('investigate', 'Investigating'), ('confirm', 'Applicable'), ('dismiss_request', 'Request dismissal'), ('dismiss', 'Reviewer approved dismissal'), ('recheck', 'Request recheck'), ('recheck_passed', 'Reviewer verified recheck')])
    rationale = models.TextField()
    version = models.ForeignKey('evidence.EvidenceVersion', on_delete=models.PROTECT, null=True, blank=True)
    observation = models.ForeignKey('monitoring.Observation', on_delete=models.PROTECT, null=True, blank=True)


class Software(Record):
    asset = models.ForeignKey('monitoring.Asset', on_delete=models.PROTECT)
    product = models.CharField(max_length=160)
    version = models.CharField(max_length=80)
    source = models.CharField(max_length=240)
    observed_at = models.DateTimeField()
    supersedes = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT, related_name='replacement')

    def __str__(self):
        return f'{self.product} {self.version} · {self.asset.name}'


class Advisory(Record):
    reference = models.CharField(max_length=100)
    product = models.CharField(max_length=160)
    minimum = models.CharField(max_length=80)
    fixed = models.CharField(max_length=80)
    source = models.CharField(max_length=240)
    remediation = models.TextField()


class VulnerabilityMatch(Record):
    software = models.ForeignKey(Software, on_delete=models.PROTECT)
    advisory = models.ForeignKey(Advisory, on_delete=models.PROTECT)
    risk = models.OneToOneField(PublishedRisk, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['software', 'advisory'], name='assurance_match_once')]


class QuoteDraft(Record):
    payload = models.JSONField()
    context_sha256 = models.CharField(max_length=64)
    # Local deterministic preparation; never represented as a live language-model call.
    method = models.CharField(max_length=80, default='Evidence-linked local demo preparation v1')


class QuoteReview(Record):
    draft = models.OneToOneField(QuoteDraft, on_delete=models.PROTECT, related_name='review')
    outcome = models.CharField(max_length=16, choices=[('approved', 'Approved for demo preparation'), ('rejected', 'Rejected')])
    notes = models.TextField()


class OutageScenario(Record):
    provider = models.ForeignKey('portfolio.Provider', on_delete=models.PROTECT)
    hours = models.DecimalField(max_digits=8, decimal_places=2)
    assumptions = models.TextField()
    payload = models.JSONField()


class DemoApproval(Record):
    area = models.CharField(max_length=32)
    rationale = models.TextField()
    context_sha256 = models.CharField(max_length=64)

