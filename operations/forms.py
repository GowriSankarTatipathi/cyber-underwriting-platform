from django import forms


class ExtractionEvaluationForm(forms.Form):
    expected_facts = forms.IntegerField(min_value=0, label="Facts expected from independently labelled source")
    correct_facts = forms.IntegerField(min_value=0, label="Correct extracted facts (each expected fact counts once)")
    correct_citations = forms.IntegerField(min_value=0, label="Correct source citations (match and support the claimed fact)")
    rationale = forms.CharField(widget=forms.Textarea, label="Evaluation method, expected facts and rationale")


class FollowupEvaluationForm(forms.Form):
    sufficient = forms.TypedChoiceField(choices=[("yes", "Yes — the first response supplied the requested evidence"), ("no", "No — another follow-up was needed")], coerce=lambda v: v == "yes")
    rationale = forms.CharField(widget=forms.Textarea)
