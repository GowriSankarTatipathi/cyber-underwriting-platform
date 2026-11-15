from django import forms
from evidence.models import EvidenceVersion
from monitoring.models import Asset, Observation
from portfolio.models import Provider
from underwriting.models import Decision, Finding
from . import models


class CaseModelForm(forms.ModelForm):
    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.case = case
        for name, field in self.fields.items():
            if isinstance(field, forms.ModelChoiceField):
                model = field.queryset.model
                if model is EvidenceVersion:
                    field.queryset = model.objects.filter(document__request__case=case)
                elif model is Observation:
                    field.queryset = model.objects.filter(group__asset__case=case)
                    field.label_from_instance = lambda o: f'{o.group.asset.name} · {o.group.kind} · {o.observed_at:%Y-%m-%d %H:%M}'
                else:
                    field.queryset = model.objects.filter(case=case)
            if isinstance(field, forms.DateTimeField):
                field.widget = forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M')
            elif isinstance(field, forms.DateField):
                field.widget = forms.DateInput(attrs={'type': 'date'})
        if 'supersedes' in self.fields:
            self.fields['supersedes'].queryset = self.fields['supersedes'].queryset.filter(replacement__isnull=True)
            self.fields['supersedes'].label = 'Current record to correct (optional)'


class PolicyForm(CaseModelForm):
    class Meta:
        model = models.PolicyRecord
        fields = ['reference', 'start', 'end', 'currency', 'limit', 'deductible', 'source', 'decision', 'supersedes']


class ClaimForm(CaseModelForm):
    class Meta:
        model = models.Claim
        fields = ['policy', 'reference', 'incident_date', 'incurred_loss', 'downtime_hours', 'maturity', 'source', 'explanation', 'supersedes']

    def __init__(self, case, *args, **kwargs):
        super().__init__(case, *args, **kwargs)
        self.fields['policy'].queryset = models.PolicyRecord.objects.filter(case=case, replacement__isnull=True)


class ComparisonForm(CaseModelForm):
    class Meta:
        model = models.Comparison
        fields = ['version', 'observation', 'topic', 'internal_value', 'external_value']


class RiskForm(CaseModelForm):
    class Meta:
        model = models.PublishedRisk
        fields = ['finding', 'service', 'category', 'summary', 'recommendation', 'observation']

    def __init__(self, case, *args, **kwargs):
        super().__init__(case, *args, **kwargs)
        self.fields['finding'].queryset = case.findings.filter(published_risk__isnull=True).exclude(state='closed')
        self.fields['summary'].help_text = 'Approved customer-facing wording only. Internal notes are not copied.'


class SoftwareForm(CaseModelForm):
    class Meta:
        model = models.Software
        fields = ['asset', 'product', 'version', 'source', 'observed_at', 'supersedes']


class AdvisoryForm(CaseModelForm):
    class Meta:
        model = models.Advisory
        fields = ['reference', 'product', 'minimum', 'fixed', 'source', 'remediation']


class RuleForm(forms.Form):
    impact = forms.IntegerField(min_value=0, max_value=100, initial=35)
    exposure = forms.IntegerField(min_value=0, max_value=100, initial=30)
    uncertainty = forms.IntegerField(min_value=0, max_value=100, initial=15)
    recovery = forms.IntegerField(min_value=0, max_value=100, initial=20)
    rationale = forms.CharField(widget=forms.Textarea, max_length=4000)


class ScoreForm(forms.Form):
    impact = forms.IntegerField(min_value=0, max_value=4, help_text='0 minimal; 4 critical business service')
    exposure = forms.IntegerField(min_value=0, max_value=4, help_text='0 isolated; 4 confirmed significant exposure')
    uncertainty = forms.IntegerField(min_value=0, max_value=4, help_text='0 strong current evidence; 4 missing or conflicting evidence')
    recovery = forms.IntegerField(min_value=0, max_value=4, help_text='0 verified target met; 4 failed or unknown recovery')
    override = forms.ChoiceField(choices=[('', 'Use calculated priority')] + Finding.PRIORITIES, required=False)
    rationale = forms.CharField(widget=forms.Textarea, max_length=4000)
    revision = forms.IntegerField(widget=forms.HiddenInput)


class ImportForm(forms.Form):
    payload = forms.JSONField(widget=forms.Textarea(attrs={'rows': 16}), help_text='JSON array, up to 100 claim rows. Policy reference is required. An exact repeat is idempotent.')

    def clean_payload(self):
        import json
        data = self.cleaned_data['payload']
        if len(json.dumps(data)) > 100000:
            raise forms.ValidationError('Keep this demo import below 100 KB.')
        return data


class ComparisonReviewForm(forms.Form):
    disposition = forms.ChoiceField(choices=models.ComparisonReview._meta.get_field('disposition').choices)
    rationale = forms.CharField(widget=forms.Textarea, max_length=4000)


class InvestigationForm(forms.Form):
    action = forms.ChoiceField(choices=models.Investigation._meta.get_field('action').choices)
    rationale = forms.CharField(widget=forms.Textarea, max_length=4000)
    version = forms.ModelChoiceField(queryset=EvidenceVersion.objects.none(), required=False, label='Supporting evidence version')
    observation = forms.ModelChoiceField(queryset=Observation.objects.none(), required=False, label='Later recheck observation (reviewers only)')

    def __init__(self, risk, staff, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['version'].queryset = EvidenceVersion.objects.filter(document__request=risk.finding.request)
        if staff:
            self.fields['observation'].queryset = Observation.objects.filter(group__asset__case=risk.case, group__asset__service=risk.service)
        else:
            self.fields.pop('observation')
            self.fields['action'].choices = [c for c in self.fields['action'].choices if c[0] not in ('dismiss', 'recheck_passed')]


class QuoteReviewForm(forms.Form):
    outcome = forms.ChoiceField(choices=models.QuoteReview._meta.get_field('outcome').choices)
    notes = forms.CharField(widget=forms.Textarea, max_length=4000)


class OutageForm(forms.Form):
    provider = forms.ModelChoiceField(queryset=Provider.objects.none())
    hours = forms.DecimalField(min_value=0.01, max_value=8760, decimal_places=2)
    assumptions = forms.CharField(widget=forms.Textarea, max_length=4000, initial='Assume service interruption lasts for the provider outage. No failover is assumed. Verify recovery plans separately.')

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['provider'].queryset = Provider.objects.filter(insurer=case.insurer)


class ApprovalForm(forms.Form):
    from .services import AREAS
    area = forms.ChoiceField(choices=list(AREAS.items()))
    rationale = forms.CharField(widget=forms.Textarea, max_length=4000)
    confirm = forms.BooleanField(label='I approve this fictional demonstration only; this does not represent an insurer or production certification.')
