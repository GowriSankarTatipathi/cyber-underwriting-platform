import json
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from evidence.exports import request_payload
from evidence.permissions import require_reviewer
from monitoring.models import Observation
from underwriting.permissions import require_decider
from workspaces.permissions import get_case, require_evidence_access, insurer_role, can_manage
from workspaces.services import audit
from . import forms, models, research, services


TABS = [('overview', 'Unified assessment'), ('policies', 'Policies & claims'), ('risks', 'Customer risk actions'),
        ('priorities', 'Priority scoring'), ('comparisons', 'Evidence comparisons'), ('software', 'Software & vulnerabilities'),
        ('quotes', 'Submission preparation'), ('outages', 'Provider outages'), ('research', 'Research demo'), ('acceptance', 'Demo approvals')]


def access(request, case_id, staff=True):
    case = get_case(request.user, case_id)
    require_evidence_access(request.user, case)
    if staff:
        require_reviewer(request.user, case)
    return case


def context(request, case, section):
    staff = bool(insurer_role(request.user, case))
    demo = settings.DEBUG and models.DemoCase.objects.filter(case=case).exists()
    tabs = [(key, label) for key, label in TABS if demo or key not in ('research', 'acceptance')]
    return {'case': case, 'staff': staff, 'manager': can_manage(request.user, case), 'section': section,
        'tabs': tabs if staff else [('risks', 'Customer risk actions')], 'demo': demo}


def demo_access(case):
    try:
        services.demo_required(case)
    except ValidationError:
        raise Http404


@login_required
def overview(request, case_id):
    return section(request, case_id, 'overview' if insurer_role(request.user, get_case(request.user, case_id)) else 'risks')


@login_required
def section(request, case_id, section):
    if section not in dict(TABS):
        raise Http404
    case = access(request, case_id, staff=section != 'risks')
    ctx = context(request, case, section)
    if section == 'overview':
        ctx.update(controls=[request_payload(i) for i in case.requests.order_by('title')],
            policies=models.PolicyRecord.objects.filter(case=case, replacement__isnull=True),
            claims=models.Claim.objects.filter(case=case, replacement__isnull=True).select_related('policy'),
            findings=case.findings.exclude(state='closed'),
            observations=Observation.objects.filter(group__asset__case=case).select_related('group__asset').order_by('-observed_at')[:20])
    elif section == 'policies':
        ctx.update(policies=models.PolicyRecord.objects.filter(case=case).order_by('-pk'), claims=models.Claim.objects.filter(case=case).select_related('policy').order_by('-pk'), imports=models.ImportReceipt.objects.filter(case=case).order_by('-pk'))
    elif section == 'risks':
        ctx['risks'] = services.visible_risks(request.user, case)
    elif section == 'priorities':
        ctx.update(rules=models.PriorityRule.objects.filter(case=case).order_by('-pk'), findings=case.findings.exclude(state='closed'), assessments=models.PriorityAssessment.objects.filter(case=case).select_related('finding').order_by('-pk'))
    elif section == 'comparisons':
        rows = list(models.Comparison.objects.filter(case=case).select_related('version__document', 'observation__group').order_by('-pk'))
        for row in rows:
            row.current = services.comparison_current(row)
            row.latest_review = row.reviews.order_by('-pk').first()
        ctx['comparisons'] = rows
    elif section == 'software':
        ctx.update(software=models.Software.objects.filter(case=case).select_related('asset').order_by('-pk'), advisories=models.Advisory.objects.filter(case=case), matches=models.VulnerabilityMatch.objects.filter(case=case).select_related('software', 'advisory', 'risk'))
    elif section == 'quotes':
        ctx['drafts'] = models.QuoteDraft.objects.filter(case=case).order_by('-pk')
    elif section == 'outages':
        ctx['scenarios'] = []
        for row in models.OutageScenario.objects.filter(case=case).select_related('provider').order_by('-pk'):
            try:
                ctx['scenarios'].append(services.require_outage(request.user, row))
            except Http404:
                pass
    elif section == 'research':
        demo_access(case)
        data = research.report()
        ctx.update(research=data, metrics=json.dumps(data['metrics'], indent=2))
    elif section == 'acceptance':
        demo_access(case)
        digest = services.quote_context(case)
        ctx['approvals'] = [{'label': label, 'row': models.DemoApproval.objects.filter(case=case, area=key, context_sha256=digest).order_by('-pk').first()} for key, label in services.AREAS.items()]
        ctx['history'] = models.DemoApproval.objects.filter(case=case).order_by('-pk')
    return render(request, 'assurance/section.html', ctx)


def form_page(request, case, form, title, section, save, intro='Records retain their source and history. Review the selected case and scope before saving.'):
    if request.method == 'POST' and form.is_valid():
        try:
            save(dict(form.cleaned_data))
            messages.success(request, 'Saved. The history is available in this workspace.')
            return redirect('assessment_section', case.pk, section)
        except ValidationError as error:
            form.add_error(None, '; '.join(error.messages))
    return render(request, 'assurance/form.html', {**context(request, case, section), 'form': form, 'title': title, 'intro': intro})


@login_required
def edit(request, case_id, action):
    case = access(request, case_id)
    data = request.POST if request.method == 'POST' else None
    specifications = {
        'policy': (forms.PolicyForm, services.record_policy, 'Record policy or correction', 'policies'),
        'claim': (forms.ClaimForm, services.record_claim, 'Record claim or correction', 'policies'),
        'comparison': (forms.ComparisonForm, services.compare, 'Compare internal and external evidence', 'comparisons'),
        'risk': (forms.RiskForm, services.publish_risk, 'Publish customer risk guidance', 'risks'),
        'software': (forms.SoftwareForm, services.record_software, 'Record software inventory', 'software'),
        'advisory': (forms.AdvisoryForm, services.record_advisory, 'Record sourced advisory', 'software'),
    }
    if action in specifications:
        cls, save, title, destination = specifications[action]
        if action in ('policy', 'advisory'):
            require_decider(request.user, case)
        return form_page(request, case, cls(case, data), title, destination, lambda values: save(request.user, case, values))
    if action == 'import':
        return form_page(request, case, forms.ImportForm(data), 'Import claims with validation', 'policies', lambda d: services.import_claims(request.user, case, d['payload']))
    if action == 'rule':
        require_decider(request.user, case)
        def save(d):
            reason = d.pop('rationale')
            services.publish_rule(request.user, case, d, reason)
        return form_page(request, case, forms.RuleForm(data), 'Publish priority rule version', 'priorities', save)
    if action == 'outage':
        require_decider(request.user, case)
        return form_page(request, case, forms.OutageForm(case, data), 'Simulate provider outage', 'outages', lambda d: services.outage(request.user, case, **d))
    if action == 'approval':
        require_decider(request.user, case)
        demo_access(case)
        return form_page(request, case, forms.ApprovalForm(data), 'Approve a demo acceptance area', 'acceptance', lambda d: services.approve_demo(request.user, case, d['area'], d['rationale']))
    raise Http404


@login_required
def score(request, case_id, finding_id):
    case = access(request, case_id)
    finding = get_object_or_404(case.findings, pk=finding_id)
    form = forms.ScoreForm(request.POST if request.method == 'POST' else None, initial={'revision': finding.revision})
    def save(d):
        reason, override, revision = d.pop('rationale'), d.pop('override'), d.pop('revision')
        services.prioritize(request.user, case, finding, d, reason, override, revision)
    return form_page(request, case, form, 'Score: ' + finding.title, 'priorities', save)


@login_required
def comparison_review(request, case_id, pk):
    case = access(request, case_id)
    row = get_object_or_404(models.Comparison, pk=pk, case=case)
    return form_page(request, case, forms.ComparisonReviewForm(request.POST if request.method == 'POST' else None),
        'Resolve: ' + row.topic, 'comparisons', lambda d: services.resolve_comparison(request.user, case, row, d['disposition'], d['rationale']),
        f'Internal: {row.internal_value}. External: {row.external_value}. Review preference does not approve a control or erase either source.')


@login_required
def risk_detail(request, case_id, pk):
    case = access(request, case_id, staff=False)
    risk = get_object_or_404(services.visible_risks(request.user, case), pk=pk)
    staff = bool(insurer_role(request.user, case))
    form = forms.InvestigationForm(risk, staff, request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            d = form.cleaned_data
            services.investigate(request.user, case, risk, d['action'], d['rationale'], d.get('version'), d.get('observation'))
            return redirect('assessment_risk', case.pk, risk.pk)
        except ValidationError as e:
            form.add_error(None, '; '.join(e.messages))
    return render(request, 'assurance/risk.html', {**context(request, case, 'risks'), 'risk': risk, 'form': form,
        'events': risk.investigations.order_by('-pk')})


@login_required
def quote_detail(request, case_id, pk):
    case = access(request, case_id)
    draft = get_object_or_404(models.QuoteDraft, pk=pk, case=case)
    current = services.quote_context(case) == draft.context_sha256
    form = forms.QuoteReviewForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST':
        require_decider(request.user, case)
        if form.is_valid():
            try:
                services.review_quote(request.user, case, draft, **form.cleaned_data)
                return redirect('assessment_quote', case.pk, pk)
            except ValidationError as e:
                form.add_error(None, '; '.join(e.messages))
    return render(request, 'assurance/quote.html', {**context(request, case, 'quotes'), 'draft': draft, 'current': current, 'form': form, 'payload': json.dumps(draft.payload, indent=2)})


@login_required
def outage_detail(request, case_id, pk):
    case = access(request, case_id)
    scenario = services.require_outage(request.user, get_object_or_404(models.OutageScenario, pk=pk, case=case))
    return render(request, 'assurance/outage.html', {**context(request, case, 'outages'), 'scenario': scenario})


@login_required
@require_POST
def run(request, case_id, action):
    case = access(request, case_id)
    try:
        if action == 'quote':
            draft = services.prepare_quote(request.user, case)
            return redirect('assessment_quote', case.pk, draft.pk)
        if action == 'match':
            count = services.match_vulnerabilities(request.user, case)
            messages.success(request, f'{count} new product/version matches published for applicability review.')
            return redirect('assessment_section', case.pk, 'software')
        raise Http404
    except ValidationError as e:
        messages.error(request, '; '.join(e.messages))
        return redirect('assessment', case.pk)


@login_required
def export(request, case_id, kind):
    case = access(request, case_id)
    if kind == 'research':
        demo_access(case)
        data = research.report()
    elif kind == 'claims':
        data = services.normalize(list(models.Claim.objects.filter(case=case, replacement__isnull=True).values('reference', 'incident_date', 'policy__reference', 'policy__currency', 'incurred_loss', 'downtime_hours', 'maturity', 'source', 'explanation')))
    elif kind == 'sample-import':
        data = [{'policy_reference': 'YOUR-POLICY-REFERENCE', 'reference': 'FICTIONAL-CLAIM-NEW', 'incident_date': str(services.timezone.localdate() - services.timedelta(days=30)),
            'incurred_loss': '12500.00', 'downtime_hours': '8.00', 'maturity': 'developing', 'source': 'Fictional demo loss register', 'explanation': 'Illustrative record only. Set a date within the chosen policy.'}]
    else:
        raise Http404
    audit(case, request.user, 'Assessment export', kind)
    response = JsonResponse(data, safe=isinstance(data, dict))
    response['Content-Disposition'] = f'attachment; filename="{kind}.json"'
    return response
