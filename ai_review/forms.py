from django import forms
from underwriting.forms import FindingForm


class ConfirmForm(forms.Form):
    confirm = forms.BooleanField(label="Process this exact evidence version using the configured provider.")


class ConsentForm(forms.Form):
    expires_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), label="Authorization expires at (UTC)")
    confirm = forms.BooleanField(label="I authorize sending text extracted from this case's selected evidence documents to OpenAI for assisted review until this expiry. Human reviewers retain decision authority.")


class FactReviewForm(forms.Form):
    action = forms.ChoiceField(choices=[("accept", "Accept suggestion"), ("correct", "Correct value or citation"), ("reject", "Reject suggestion")])
    value = forms.CharField(max_length=1200, required=False, label="Corrected value (minutes for measurements; true/false for checks)")
    passage_id = forms.ChoiceField(required=False, label="Corrected source passage")
    quote = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), max_length=1600, required=False, label="Exact quotation for a correction")
    rationale = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
    previous_review_id = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    confirm = forms.BooleanField(label="I checked the cited passage against the original evidence and confirmed this review action.")

    def __init__(self, result, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["passage_id"].choices = [("", "Choose a source passage")] + [(row["id"], row["id"] + " · " + row["locator"]) for row in result.passages]


class PublishForm(FindingForm):
    rationale = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="Why this request should be sent")
    confirm = forms.BooleanField(label="I reviewed the facts, citations, priority, final request wording, owner, and deadline. Send this request to the assigned workspace.")


class RejectForm(forms.Form):
    rationale = forms.CharField(widget=forms.Textarea, label="Reason for rejecting this draft")
