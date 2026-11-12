from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from evidence.models import EvidenceVersion, RestoreTest
from workspaces.models import ChecklistVersion, User
from .models import Decision, Finding
from .permissions import finding_owners


class FindingForm(forms.ModelForm):
    revision = forms.IntegerField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Finding
        fields = ["title", "request", "kind", "description", "priority", "priority_reason", "owner", "due_date", "retest_of"]
        widgets = {"due_date": forms.DateInput(attrs={"type": "date"}), "description": forms.Textarea(attrs={"rows": 4}), "priority_reason": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["request"].queryset = case.requests.all()
        self.fields["request"].label = "Related evidence request"
        self.fields["retest_of"].queryset = RestoreTest.objects.filter(request__case=case)
        self.fields["retest_of"].help_text = "For retest findings, choose the original test. The response must link a later retest."
        selected = self.data.get("request") if self.is_bound else self.initial.get("request")
        try:
            item = case.requests.filter(pk=selected).first() if selected else None
        except (ValueError, ValidationError):
            item = None
        # Populate all eligible owners for display; the service rechecks the selected request.
        self.fields["owner"].queryset = finding_owners(case, item) if item else User.objects.filter(pk__in={user.pk for req in case.requests.all() for user in finding_owners(case, req)})
        self.fields["owner"].help_text = "Providers must already be assigned to the related evidence request."
        if not self.instance._state.adding:
            self.fields["revision"].initial = self.instance.revision
            for name in ("request", "kind", "retest_of"):
                self.fields[name].disabled = True

    def clean_due_date(self):
        value = self.cleaned_data["due_date"]
        if value < timezone.localdate() and (self.instance._state.adding or value != self.instance.due_date):
            raise forms.ValidationError("Choose today or a future deadline.")
        return value


class ResponseForm(forms.Form):
    message = forms.CharField(widget=forms.Textarea, label="Response / fix completed")
    evidence_versions = forms.ModelMultipleChoiceField(queryset=EvidenceVersion.objects.none(), required=False)
    restore_test = forms.ModelChoiceField(queryset=RestoreTest.objects.none(), required=False)

    def __init__(self, finding, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["evidence_versions"].queryset = EvidenceVersion.objects.filter(document__request=finding.request)
        self.fields["evidence_versions"].help_text = "Upload new documents through the evidence request first, then cite their exact versions here."
        self.fields["restore_test"].queryset = finding.request.restore_tests.filter(retest_of=finding.retest_of) if finding.kind == "retest" else RestoreTest.objects.none()
        if finding.kind != "retest":
            self.fields["restore_test"].widget = forms.HiddenInput()


class FindingReviewForm(forms.Form):
    action = forms.ChoiceField(choices=[("close", "Accept response and close"), ("request_changes", "Request changes"), ("reopen", "Reopen finding")])
    rationale = forms.CharField(widget=forms.Textarea)
    revision = forms.IntegerField(widget=forms.HiddenInput)


class ExceptionForm(forms.Form):
    rationale = forms.CharField(widget=forms.Textarea, label="Exception rationale")
    compensating_controls = forms.CharField(widget=forms.Textarea, label="Compensating controls and required follow-up")
    expires_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), label="Expires at (UTC)")


class DecisionForm(forms.Form):
    outcome = forms.ChoiceField(choices=Decision.OUTCOMES)
    rationale = forms.CharField(widget=forms.Textarea, label="Internal decision rationale")
    conditions = forms.ModelMultipleChoiceField(queryset=Finding.objects.none(), required=False, widget=forms.CheckboxSelectMultiple)
    confirm = forms.BooleanField(label="I reviewed this snapshot, its evidence and rule versions, and all open conditions and exceptions.")

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["conditions"].queryset = case.findings.exclude(state="closed")
        self.fields["conditions"].help_text = "For conditional approval, explicitly select the findings that remain conditions. Each retains its owner and deadline."


class ReportForm(forms.Form):
    title = forms.CharField(max_length=180)
    summary = forms.CharField(widget=forms.Textarea, label="Summary for the business")


class RationaleForm(forms.Form):
    rationale = forms.CharField(widget=forms.Textarea)
    confirm = forms.BooleanField(label="Confirm this action")


class ShareForm(forms.Form):
    recipient = forms.ModelChoiceField(queryset=User.objects.none())
    expires_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), label="Sharing expires at (UTC)")

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].queryset = User.objects.filter(is_active=True, sharinggrant__case=case, sharinggrant__role="owner", sharinggrant__revoked_at__isnull=True, sharinggrant__expires_at__gt=timezone.now())


class RenewalForm(forms.Form):
    checklist = forms.ModelChoiceField(queryset=ChecklistVersion.objects.none())
    due_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    owner_email = forms.EmailField(label="Business owner's email for a fresh invitation")

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["checklist"].queryset = ChecklistVersion.objects.filter(insurer=case.insurer, product=case.checklist.product)
