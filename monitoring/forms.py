from django import forms
from evidence.models import Service
from workspaces.models import EvidenceRequest
from .models import Asset, Connection, Observation, Policy


class AssetForm(forms.ModelForm):
    class Meta:
        model = Asset
        fields = ["name", "kind", "identifier", "request", "service"]

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["request"].queryset = case.requests.all()
        self.fields["service"].queryset = case.services.all()


class ConnectionForm(forms.ModelForm):
    class Meta:
        model = Connection
        fields = ["name", "provider", "assets", "config", "credential_ref"]
        widgets = {"config": forms.Textarea(attrs={"rows": 4}), "assets": forms.CheckboxSelectMultiple}

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assets"].queryset = case.monitored_assets.all()
        self.fields["config"].help_text = 'Entra: {"client_id":"application-uuid"}. AWS: {"account_id":"123456789012","region":"us-east-1"}, optionally role_arn. Demo: {"scenario":"risk"} or "healthy". Others: {}.'
        self.fields["credential_ref"].help_text = "Server environment name only, e.g. MONITOR_SECRET_ENTRA. Never paste a secret here."


class PolicyForm(forms.ModelForm):
    class Meta:
        model = Policy
        fields = ["min_mfa_percent", "max_backup_age_hours", "freshness_hours", "poll_minutes", "blocked_ports", "due_days", "auto_followup", "rationale"]
        widgets = {"rationale": forms.Textarea(attrs={"rows": 3}), "blocked_ports": forms.TextInput}
        labels = {"auto_followup": "Automatically create internal review findings for triggered rules", "blocked_ports": "Restricted ports (JSON list, e.g. [22, 3389])"}


class AuthorizationForm(forms.Form):
    expires_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), label="Expires at (UTC)")
    rights_confirmed = forms.BooleanField(label="I authorize recurring read-only monitoring of these assets for this case, confirm ownership or delegated authority, and confirm the required provider subscriptions and commercial/customer-domain licenses.")


class InventoryForm(forms.Form):
    cves = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), required=False, label="Known asset CVEs (one per line; empty means none recorded)")
    source = forms.CharField(max_length=240)
    observed_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), label="Inventory observed at (UTC)")

    def clean_cves(self):
        return [v.strip().upper() for v in self.cleaned_data["cves"].replace(",", "\n").splitlines() if v.strip()]


class ReconcileForm(forms.Form):
    selected = forms.ModelChoiceField(queryset=Observation.objects.none(), required=False, label="Preferred observation (leave blank to request clarification)")
    rationale = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
    context_sha256 = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, observations, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["selected"].queryset = Observation.objects.filter(pk__in=[o.pk for o in observations])
        self.fields["selected"].label_from_instance = lambda o: f"{o.group.connection.name} · {o.observed_at:%Y-%m-%d %H:%M} · {o.data}"
