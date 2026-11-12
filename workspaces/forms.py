from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.utils import timezone
from .models import Case, ChecklistVersion, EvidenceRequest, Invitation, User
from .permissions import insurer_memberships


class CaseForm(forms.Form):
    checklist = forms.ModelChoiceField(queryset=ChecklistVersion.objects.none())
    business_name = forms.CharField(max_length=160, label="Applicant business")
    kind = forms.ChoiceField(choices=Case.Kind.choices, label="Case type")
    service = forms.CharField(max_length=160, initial="Order processing", label="Critical service")
    due_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    owner_email = forms.EmailField(label="Business owner's email")

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organizations = insurer_memberships(user).filter(role__in=["admin", "underwriter"]).values("organization_id")
        self.fields["checklist"].queryset = ChecklistVersion.objects.filter(insurer_id__in=organizations).select_related("insurer")

    def clean_due_date(self):
        value = self.cleaned_data.get("due_date")
        if value and value < timezone.localdate():
            raise forms.ValidationError("Choose today or a future date.")
        return value


class InviteForm(forms.Form):
    email = forms.EmailField()
    organization_name = forms.CharField(max_length=160, required=False, label="IT provider or business name")
    access_days = forms.IntegerField(min_value=1, max_value=365, initial=90, label="Access expires after (days)")


class ConsentForm(forms.Form):
    days = forms.IntegerField(min_value=1, max_value=365, initial=90, label="Authorize access for (days)")
    confirm = forms.BooleanField(label="I authorize this insurer and my invited IT providers to review evidence for this case.")


class InvitationSignupForm(UserCreationForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name"]

    def __init__(self, invitation, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.email = invitation.email


class AssignmentForm(forms.Form):
    assignee = forms.ModelChoiceField(queryset=User.objects.none())

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignee"].queryset = User.objects.filter(is_active=True, membership__organization=case.insurer, membership__is_active=True, membership__role__in=["admin", "underwriter", "reviewer"]).distinct()


class RequestAssignmentForm(forms.Form):
    assigned_to = forms.ModelChoiceField(queryset=User.objects.none(), required=False, empty_label="Unassigned")

    def __init__(self, case, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = User.objects.filter(is_active=True, sharinggrant__case=case, sharinggrant__role="technician", sharinggrant__revoked_at__isnull=True, sharinggrant__expires_at__gt=timezone.now())


class NotesForm(forms.ModelForm):
    class Meta:
        model = Case
        fields = ["internal_notes"]
        widgets = {"internal_notes": forms.Textarea(attrs={"rows": 4})}
