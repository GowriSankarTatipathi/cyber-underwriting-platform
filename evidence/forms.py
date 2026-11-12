import uuid
from django import forms
from django.utils import timezone
from .models import BackupJob, ControlAssessment, EvidenceReview, EvidenceVersion, RecoveryPolicy, RestoreTest, Service


def datetime_field(label):
    return forms.DateTimeField(label=label + " (UTC)", widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), initial=lambda: timezone.now().replace(second=0, microsecond=0))


def clean_versions(item):
    # Scan status is checked again by the service when the form is submitted.
    return EvidenceVersion.objects.filter(document__request=item).select_related("document").order_by("document__title", "-number")


class UploadForm(forms.Form):
    submission_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    service = forms.ModelChoiceField(queryset=Service.objects.none())
    title = forms.CharField(max_length=180, label="Document title")
    file = forms.FileField(help_text="PDF, DOCX, TXT, CSV, JSON, PNG, or JPEG. Maximum 10 MB.")
    source = forms.CharField(max_length=200, label="Evidence source", help_text="For example, the backup product, test report, or identity export.")
    scope = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="Systems, accounts, and service covered")
    observed_at = datetime_field("Evidence observation time")

    def __init__(self, case, *args, document=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = case.services.all()
        if document:
            self.fields["service"].initial = document.service
            self.fields["service"].disabled = True
            self.fields["title"].initial = document.title
            self.fields["title"].disabled = True
        elif case.services.count() == 1:
            self.fields["service"].initial = case.services.first()


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ["name", "owner_name", "description", "dependencies"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3}), "dependencies": forms.Textarea(attrs={"rows": 3})}


class PolicyForm(forms.ModelForm):
    class Meta:
        model = RecoveryPolicy
        fields = ["duration_minutes", "data_age_minutes", "required_scope"]
        labels = {"duration_minutes": "Maximum elapsed recovery time (minutes)", "data_age_minutes": "Maximum recovered data age (minutes)"}
        widgets = {"required_scope": forms.Textarea(attrs={"rows": 4})}


class EvidenceReviewForm(forms.ModelForm):
    class Meta:
        model = EvidenceReview
        fields = ["decision", "scope_confirmed", "rationale"]
        labels = {"scope_confirmed": "The evidence covers the stated service and required scope"}
        widgets = {"rationale": forms.Textarea(attrs={"rows": 3})}


class ControlForm(forms.ModelForm):
    class Meta:
        model = ControlAssessment
        fields = ["result", "evidence_versions", "rationale"]
        widgets = {"evidence_versions": forms.CheckboxSelectMultiple, "rationale": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, item, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["evidence_versions"].queryset = clean_versions(item)
        self.fields["evidence_versions"].required = True


class BackupForm(forms.ModelForm):
    observed_at = datetime_field("Backup job observation time")

    class Meta:
        model = BackupJob
        fields = ["service", "observed_at", "status", "coverage", "source_version"]
        widgets = {"coverage": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, item, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = item.case.services.all()
        self.fields["source_version"].queryset = clean_versions(item)


class RestoreForm(forms.ModelForm):
    performed_at = datetime_field("Supervised test time")
    functional_checks_passed = forms.TypedChoiceField(label="Did the recovered service work?", choices=[("unknown", "Unknown / not measured"), ("yes", "Yes — functional checks passed"), ("no", "No — functional checks failed")], coerce=lambda value: {"unknown": None, "yes": True, "no": False}[value], initial="unknown")

    class Meta:
        model = RestoreTest
        fields = ["service", "retest_of", "performed_at", "supervised_by", "scope", "duration_minutes", "data_age_minutes", "functional_checks_passed", "functional_checks", "evidence_versions"]
        labels = {"duration_minutes": "Measured elapsed recovery time (minutes)", "data_age_minutes": "Measured recovered data age (minutes)", "retest_of": "Retest of an earlier test (optional)"}
        help_texts = {"duration_minutes": "Leave blank if this measurement is unavailable. Zero means a measured zero.", "data_age_minutes": "Age of the recovered data at the agreed reference time. Leave blank if unavailable."}
        widgets = {"scope": forms.Textarea(attrs={"rows": 3}), "functional_checks": forms.Textarea(attrs={"rows": 3}), "evidence_versions": forms.CheckboxSelectMultiple}

    def __init__(self, item, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = item.case.services.all()
        self.fields["retest_of"].queryset = item.restore_tests.select_related("service").all()
        self.fields["evidence_versions"].queryset = clean_versions(item)
        self.fields["evidence_versions"].required = True


class RestoreReviewForm(forms.Form):
    scope_confirmed = forms.TypedChoiceField(label="Did the test cover the required scope?", choices=[("unknown", "Unknown — more evidence required"), ("yes", "Yes — full required scope"), ("no", "No — partial scope")], coerce=lambda value: {"unknown": None, "yes": True, "no": False}[value], initial="unknown")
    rationale = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), label="Review rationale")
