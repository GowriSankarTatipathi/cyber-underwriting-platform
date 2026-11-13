from django import forms
from django.contrib.auth import get_user_model
from underwriting.models import Report
from .models import Outcome, PartnerResponse, Relationship


def expiry():
    return forms.DateTimeField(label="Expires at (UTC)", widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))


class PermissionForm(forms.Form):
    expires_at = expiry()
    confirm = forms.BooleanField(label="I authorize this insurer to use this case's incident/loss outcomes and provider dependencies for internal portfolio research until this expiry. I can revoke future portfolio access.")


class OutcomeForm(forms.ModelForm):
    class Meta:
        model = Outcome
        fields = ["decision", "period_start", "period_end", "incidents", "incurred_loss", "currency", "source", "maturity", "rationale", "supersedes"]
        widgets = {"period_start": forms.DateInput(attrs={"type": "date"}), "period_end": forms.DateInput(attrs={"type": "date"}), "rationale": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["decision"].queryset = case.decisions.all()
        self.fields["decision"].label_from_instance = lambda d: f"Decision {d.number}: {d.get_outcome_display()} ({d.created_at:%Y-%m-%d})"
        from .services import permission_active
        allowed = [row.pk for row in case.portfolio_outcomes.filter(replacement__isnull=True) if permission_active(row.permission)]
        self.fields["supersedes"].queryset = case.portfolio_outcomes.filter(pk__in=allowed)
        self.fields["supersedes"].label_from_instance = lambda o: f"{o.period_start}–{o.period_end}: {o.incidents} incidents, {o.currency} {o.incurred_loss}"
        self.fields["period_end"].help_text = "Exclusive end date. Include zero-incident periods to measure exposure."
        self.fields["source"].help_text = "Approved loss register or incident record reference."
        self.fields["rationale"].help_text = "Explain scope, loss development and any correction. Do not include personal identifiers."


class DependencyForm(forms.Form):
    service = forms.ModelChoiceField(queryset=None)
    provider_name = forms.CharField(max_length=160)
    provider_key = forms.CharField(max_length=160, help_text="Stable insurer-wide identifier, e.g. provider legal entity or canonical domain. Reuse it across cases.")
    critical = forms.BooleanField(required=False, label="Critical to the selected service")
    source = forms.CharField(max_length=240, help_text="Reference the inventory, contract or reviewed evidence supporting this dependency.")
    observed_at = forms.DateTimeField(label="Observed at (UTC)", widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = case.services.all()


class RationaleForm(forms.Form):
    rationale = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=5000)


class ValidationForm(forms.Form):
    cutoff = forms.DateField(label="Start of later holdout period", widget=forms.DateInput(attrs={"type": "date"}))
    currency = forms.RegexField(r"^[A-Za-z]{3}$", max_length=3, initial="USD")


class RelationshipForm(forms.Form):
    partner_name = forms.CharField(max_length=160)
    recipient_email = forms.EmailField(label="Existing partner account email")
    contract_reference = forms.CharField(max_length=240)
    expires_at = expiry()
    confirm = forms.BooleanField(label="I confirm an existing commercial relationship permits this named recipient to receive approved submissions.")

    def clean_recipient_email(self):
        value = self.cleaned_data["recipient_email"]
        recipient = get_user_model().objects.filter(email__iexact=value, is_active=True).first()
        if not recipient:
            raise forms.ValidationError("An active account with this exact email must be provisioned first.")
        return recipient


class SubmissionForm(forms.Form):
    relationship = forms.ModelChoiceField(queryset=Relationship.objects.none())
    report = forms.ModelChoiceField(queryset=Report.objects.none())
    expires_at = expiry()

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.utils import timezone
        self.fields["relationship"].queryset = Relationship.objects.filter(insurer=case.insurer, revocation__isnull=True, expires_at__gt=timezone.now(), recipient__is_active=True)
        self.fields["relationship"].label_from_instance = lambda r: f"{r.partner_name} · {r.recipient.email}"
        self.fields["report"].queryset = Report.objects.filter(decision__case=case, approval__isnull=False, withdrawal__isnull=True)
        self.fields["report"].label_from_instance = lambda r: f"{r.title} · decision {r.decision.number}"


class ConsentForm(forms.Form):
    confirm = forms.BooleanField(label="I approve the exact content shown below for this named partner recipient until the displayed expiry.")


class ResponseForm(forms.ModelForm):
    class Meta:
        model = PartnerResponse
        fields = ["status", "message"]
        widgets = {"message": forms.Textarea(attrs={"rows": 4})}
