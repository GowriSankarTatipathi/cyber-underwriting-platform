"""Validated assessment operations, sharing existing case locks and permissions."""
from datetime import timedelta
from decimal import Decimal
import json
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from evidence.models import EvidenceVersion
from evidence.permissions import require_reviewer
from evidence.rules import usable_version
from monitoring.models import Observation
from portfolio import services as portfolio
from underwriting.models import Finding
from underwriting.permissions import require_decider, visible_findings, require_responder
from underwriting.services import locked_case, save_finding
from underwriting.snapshots import checksum, normalize, records
from workspaces.permissions import insurer_role, require_evidence_access
from workspaces.services import audit
from .models import (Advisory, Claim, Comparison, ComparisonReview, DemoApproval, DemoCase,
    ImportReceipt, Investigation, OutageScenario, PolicyRecord, PriorityAssessment, PriorityRule,
    PublishedRisk, QuoteDraft, QuoteReview, Software, VulnerabilityMatch)


DEFAULT_WEIGHTS = {'impact': 35, 'exposure': 30, 'uncertainty': 15, 'recovery': 20}
AREAS = {'requirements': 'Evidence requirements', 'rules': 'Scoring and review rules',
         'journey': 'Full case journey', 'reports': 'Report and partner demonstration'}


def demo_required(case):
    if not settings.DEBUG or not DemoCase.objects.filter(case=case).exists():
        raise ValidationError('This action requires a marked fictional demo case in local debug mode.')


def save_record(model, user, case, **data):
    row = model(case=case, actor=user, **data)
    row.full_clean()
    row.save()
    audit(case, user, model.__name__ + ' recorded', str(row.pk))
    return row


def rationale(value):
    if not value or not value.strip():
        raise ValidationError('Explain the source, decision or action before saving.')


def same_case(case, *objects):
    for obj in objects:
        if obj is not None and obj.case_id != case.pk:
            raise ValidationError('Select records belonging to this case.')


def money(value):
    if not value.is_finite() or value < 0:
        raise ValidationError('Enter a finite, nonnegative amount.')


@transaction.atomic
def record_policy(user, case, data):
    case = locked_case(user, case)
    require_decider(user, case)
    from workspaces.models import Organization
    Organization.objects.select_for_update().get(pk=case.insurer_id)
    start, end, old = data['start'], data['end'], data.get('supersedes')
    same_case(case, old, data.get('decision'))
    if start >= end:
        raise ValidationError('Coverage end must follow its start; the end date is exclusive.')
    if not re.fullmatch(r'[A-Z]{3}', data['currency']):
        raise ValidationError('Use a three-letter uppercase currency code.')
    for key in ('limit', 'deductible'):
        money(data[key])
    if data['deductible'] > data['limit']:
        raise ValidationError('The deductible cannot exceed the policy limit.')
    if data.get('decision') and data['decision'].created_at.date() > start:
        raise ValidationError('A supporting decision must exist by the start of coverage. Leave it empty for imported history without a recorded prior decision.')
    if old and (hasattr(old, 'replacement') or old.reference != data['reference']):
        raise ValidationError('Correct the current version of the same policy reference.')
    existing = PolicyRecord.objects.filter(case__insurer=case.insurer, reference=data['reference'], replacement__isnull=True)
    if old:
        existing = existing.exclude(pk=old.pk)
    if existing.exists():
        raise ValidationError('That policy reference already exists. Select its current version to correct it.')
    # Do not silently move linked claims outside corrected coverage.
    if old and Claim.objects.filter(policy__case=case, policy__reference=old.reference, replacement__isnull=True).exclude(incident_date__gte=start, incident_date__lt=end).exists():
        raise ValidationError('Corrected coverage must still include the linked claim incidents.')
    if old and old.currency != data['currency'] and Claim.objects.filter(case=case, policy__reference=old.reference).exists():
        raise ValidationError('A policy with recorded claims cannot change currency. Reconcile currency separately.')
    rationale(data['source'])
    return save_record(PolicyRecord, user, case, **data)


@transaction.atomic
def record_claim(user, case, data):
    case = locked_case(user, case)
    require_reviewer(user, case)
    policy, old = data['policy'], data.get('supersedes')
    same_case(case, policy, old)
    if hasattr(policy, 'replacement'):
        raise ValidationError('Select the current policy version.')
    if not policy.start <= data['incident_date'] < policy.end or data['incident_date'] > timezone.localdate():
        raise ValidationError('The incident must fall within coverage and cannot be in the future.')
    money(data['incurred_loss']); money(data['downtime_hours'])
    if old and (hasattr(old, 'replacement') or old.reference != data['reference'] or old.policy.reference != policy.reference):
        raise ValidationError('Correct the latest claim with the same claim and policy references.')
    existing = Claim.objects.filter(case=case, reference=data['reference'], replacement__isnull=True)
    if old:
        existing = existing.exclude(pk=old.pk)
    if existing.exists():
        raise ValidationError('Duplicate claim reference. Select the current claim to append a correction.')
    rationale(data['source']); rationale(data['explanation'])
    return save_record(Claim, user, case, **data)


@transaction.atomic
def import_claims(user, case, payload):
    case = locked_case(user, case)
    require_reviewer(user, case)
    if not isinstance(payload, list) or not 1 <= len(payload) <= 100:
        raise ValidationError('Supply a JSON array containing 1 to 100 claims.')
    digest = checksum(payload)
    prior = ImportReceipt.objects.filter(case=case, digest=digest).first()
    if prior:
        return prior
    from .forms import ClaimForm
    ids = []
    for index, raw in enumerate(payload, 1):
        if not isinstance(raw, dict):
            raise ValidationError(f'Row {index}: expected an object.')
        values = dict(raw)
        ref = values.pop('policy_reference', '')
        policy = PolicyRecord.objects.filter(case=case, reference=ref, replacement__isnull=True).first()
        if not policy:
            raise ValidationError(f'Row {index}: policy reference is not available in this case.')
        allowed = {'reference', 'incident_date', 'incurred_loss', 'downtime_hours', 'maturity', 'source', 'explanation', 'supersedes'}
        if set(values) - allowed:
            raise ValidationError(f'Row {index}: contains unsupported fields.')
        form = ClaimForm(case, {**values, 'policy': policy.pk})
        if not form.is_valid():
            raise ValidationError(f'Row {index}: ' + form.errors.as_text())
        ids.append(record_claim(user, case, form.cleaned_data).pk)
    return save_record(ImportReceipt, user, case, digest=digest, summary={'rows': len(ids), 'claim_ids': ids, 'currency_handling': 'Each claim retains its policy currency.'})


@transaction.atomic
def publish_rule(user, case, weights, reason):
    case = locked_case(user, case)
    require_decider(user, case)
    rationale(reason)
    if set(weights) != set(DEFAULT_WEIGHTS) or any(type(v) is not int or not 0 <= v <= 100 for v in weights.values()) or sum(weights.values()) != 100:
        raise ValidationError('Use impact, exposure, uncertainty and recovery integer weights totaling 100.')
    return save_record(PriorityRule, user, case, weights=weights, rationale=reason)


def compute_score(inputs, weights):
    if set(inputs) != set(DEFAULT_WEIGHTS) or any(type(v) is not int or not 0 <= v <= 4 for v in inputs.values()):
        raise ValidationError('Each factor must be an integer from 0 to 4.')
    score = round(sum(inputs[k] * weights[k] / 4 for k in weights))
    return score, 'critical' if score >= 80 else 'high' if score >= 60 else 'medium' if score >= 30 else 'low'


@transaction.atomic
def prioritize(user, case, finding, inputs, reason, override='', revision=None):
    case = locked_case(user, case)
    require_reviewer(user, case)
    same_case(case, finding)
    finding.refresh_from_db()
    if finding.state == 'closed' or finding.revision != revision:
        raise ValidationError('The finding changed or is closed. Reload it before scoring.')
    rule = PriorityRule.objects.filter(case=case).order_by('-pk').first()
    if not rule:
        raise ValidationError('An underwriter must publish scoring rules first.')
    score, suggested = compute_score(inputs, rule.weights)
    selected = override or suggested
    if selected not in dict(Finding.PRIORITIES):
        raise ValidationError('Choose a valid priority.')
    rationale(reason)
    data = {key: getattr(finding, key) for key in ('request', 'title', 'description', 'kind', 'owner', 'due_date', 'retest_of')}
    data.update(priority=selected, priority_reason=f'Rule {rule.pk}: {score}/100; suggested {suggested}. ' + reason)
    updated = save_finding(user, case, data, finding, revision)
    return save_record(PriorityAssessment, user, case, finding=finding, rule=rule, inputs=inputs, score=score,
        suggested=suggested, selected=selected, rationale=reason, finding_revision=updated.revision)


def comparison_context(version, observation):
    latest = Observation.objects.filter(group__asset=observation.group.asset, group__kind=observation.group.kind,
        group__connection=observation.group.connection).order_by('-observed_at', '-created_at', '-pk').first()
    return {'evidence_id': str(version.pk), 'sha256': version.sha256, 'latest_version': str(version.document.latest_version.pk),
        'review': version.latest_review.pk if version.latest_review else None,
        'scan': version.latest_scan.pk if version.latest_scan else None,
        'observation_id': str(observation.pk), 'latest_observation': str(latest.pk) if latest else None,
        'observation_data': observation.data, 'source': observation.provenance}


def comparison_current(row):
    return row.context == comparison_context(row.version, row.observation)


@transaction.atomic
def compare(user, case, data):
    case = locked_case(user, case)
    require_reviewer(user, case)
    v, o = data['version'], data['observation']
    if v.document.request.case_id != case.pk or o.group.asset.case_id != case.pk or v.document.service_id != o.group.asset.service_id:
        raise ValidationError('Compare evidence and observations for the same case and service.')
    return save_record(Comparison, user, case, **data, context=comparison_context(v, o))


@transaction.atomic
def resolve_comparison(user, case, comparison, disposition, reason):
    case = locked_case(user, case)
    require_reviewer(user, case)
    same_case(case, comparison)
    if not comparison_current(comparison):
        raise ValidationError('Sources changed. Create a new comparison before resolving it.')
    rationale(reason)
    return save_record(ComparisonReview, user, case, comparison=comparison, disposition=disposition, rationale=reason)


def visible_risks(user, case):
    return PublishedRisk.objects.filter(case=case, finding__in=visible_findings(user, case)).select_related('finding', 'service')


@transaction.atomic
def publish_risk(user, case, data):
    case = locked_case(user, case)
    require_reviewer(user, case)
    same_case(case, data['finding'], data['service'])
    o = data.get('observation')
    if o and (o.group.asset.case_id != case.pk or o.group.asset.service_id != data['service'].pk or o.group.asset.request_id != data['finding'].request_id):
        raise ValidationError('The source must match this service and finding request.')
    if PublishedRisk.objects.filter(finding=data['finding']).exists():
        raise ValidationError('This finding is already published in customer risk actions.')
    return save_record(PublishedRisk, user, case, **data)


@transaction.atomic
def investigate(user, case, risk, action, reason, version=None, observation=None):
    case = locked_case(user, case)
    if not visible_risks(user, case).filter(pk=risk.pk).exists():
        raise Http404
    require_responder(user, risk.finding)
    rationale(reason)
    if version and version.document.request_id != risk.finding.request_id:
        raise ValidationError('The evidence must belong to this assigned request.')
    if observation and (observation.group.asset.case_id != case.pk or observation.group.asset.service_id != risk.service_id):
        raise ValidationError('The recheck source must belong to the affected service.')
    if action in ('dismiss', 'recheck_passed'):
        require_reviewer(user, case)
        if not version or not usable_version(version, risk.finding.request.freshness_days) or version.document.latest_version.pk != version.pk:
            raise ValidationError('Reviewer disposition requires current, accepted, fresh evidence.')
        if action == 'recheck_passed' and risk.observation_id:
            if not observation or observation.observed_at <= risk.observation.observed_at or observation.group.kind != risk.observation.group.kind or observation.group.asset_id != risk.observation.group.asset_id:
                raise ValidationError('Select a later observation for the same asset and signal kind.')
            from monitoring.rules import evaluate, latest_observations
            policy = case.monitoring_policies.order_by('-number').first()
            if not policy or evaluate(observation, policy)['reasons'] or observation.pk not in {o.pk for o in latest_observations(observation.group.asset, observation.group.kind)}:
                raise ValidationError('The latest recheck must satisfy the current monitoring rules; a later risky result cannot pass.')
        if action == 'recheck_passed' and version.observed_at <= risk.created_at:
            raise ValidationError('A recheck needs evidence observed after the risk was published.')
    elif action not in ('investigate', 'confirm', 'dismiss_request', 'recheck'):
        raise ValidationError('Choose an investigation action.')
    # Investigation never closes a finding; standard evidence review controls closure.
    return save_record(Investigation, user, case, risk=risk, action=action, rationale=reason, version=version, observation=observation)


def version_tuple(value):
    if not re.fullmatch(r'\d{1,6}(?:\.\d{1,6}){0,3}', value):
        raise ValidationError('Use a numeric release version such as 2.4.1. Unsupported version schemes require manual review.')
    parts = tuple(int(p) for p in value.split('.'))
    return parts + (0,) * (4 - len(parts))


@transaction.atomic
def record_software(user, case, data):
    case = locked_case(user, case)
    require_reviewer(user, case)
    same_case(case, data['asset'], data.get('supersedes'))
    version_tuple(data['version'])
    if data['observed_at'] > timezone.now():
        raise ValidationError('Inventory observation cannot be in the future.')
    old = data.get('supersedes')
    if old and (hasattr(old, 'replacement') or old.asset_id != data['asset'].pk or old.product.casefold() != data['product'].casefold()):
        raise ValidationError('Replace the latest inventory for the same asset and product.')
    current = Software.objects.filter(case=case, asset=data['asset'], product__iexact=data['product'], replacement__isnull=True)
    if old:
        current = current.exclude(pk=old.pk)
    if current.exists():
        raise ValidationError('A current inventory exists. Select it as the version to replace.')
    return save_record(Software, user, case, **data)


@transaction.atomic
def record_advisory(user, case, data):
    case = locked_case(user, case)
    require_decider(user, case)
    if version_tuple(data['minimum']) >= version_tuple(data['fixed']):
        raise ValidationError('Fixed version must be later than the first affected version.')
    if Advisory.objects.filter(case=case, reference=data['reference']).exists():
        raise ValidationError('Advisory reference already exists; use a distinct revision reference.')
    return save_record(Advisory, user, case, **data)


@transaction.atomic
def match_vulnerabilities(user, case):
    case = locked_case(user, case)
    require_reviewer(user, case)
    count = 0
    for software in Software.objects.filter(case=case, replacement__isnull=True).select_related('asset__request', 'asset__service'):
        # Stale inventory does not create a fresh alert.
        if software.observed_at < timezone.now() - timedelta(days=software.asset.request.freshness_days):
            continue
        for advisory in Advisory.objects.filter(case=case, product__iexact=software.product):
            if not version_tuple(advisory.minimum) <= version_tuple(software.version) < version_tuple(advisory.fixed):
                continue
            if VulnerabilityMatch.objects.filter(software=software, advisory=advisory).exists():
                continue
            item = software.asset.request
            from underwriting.permissions import finding_owners
            owner = item.assigned_to if item.assigned_to and finding_owners(case, item).filter(pk=item.assigned_to_id).exists() else user
            finding = save_finding(user, case, {'request': item, 'title': f'{advisory.reference}: {software.product}'[:180],
                'description': f'Confirm applicability to {software.version}. {advisory.remediation}', 'kind': 'fix', 'priority': 'high',
                'priority_reason': 'Exact product match and numeric version inside the sourced affected range; applicability needs review.',
                'owner': owner, 'due_date': timezone.localdate() + timedelta(days=7), 'retest_of': None})
            risk = publish_risk(user, case, {'finding': finding, 'service': software.asset.service, 'category': 'vulnerability',
                'summary': f'{software.product} {software.version}; {advisory.reference}; source: {advisory.source}', 'recommendation': advisory.remediation, 'observation': None})
            save_record(VulnerabilityMatch, user, case, software=software, advisory=advisory, risk=risk)
            count += 1
    return count


def assessment_material(case):
    """Included in saved decisions; excludes draft approvals and storage locations."""
    result = {}
    for key, model in [('policies', PolicyRecord), ('claims', Claim), ('priority_rules', PriorityRule),
        ('priorities', PriorityAssessment), ('comparisons', Comparison), ('comparison_reviews', ComparisonReview),
        ('software', Software), ('advisories', Advisory), ('published_risks', PublishedRisk), ('investigations', Investigation)]:
        rows = records(model.objects.filter(case=case))
        if rows:
            result[key] = rows
    return normalize(result)


def quote_context(case):
    from underwriting.snapshots import case_snapshot
    from ai_review.models import FactReview
    return checksum({'case': case_snapshot(case), 'fact_reviews': records(FactReview.objects.filter(result__job__case=case))})


@transaction.atomic
def prepare_quote(user, case):
    case = locked_case(user, case)
    require_reviewer(user, case)
    from evidence.exports import request_payload
    controls = [request_payload(item) for item in case.requests.order_by('pk')]
    policies = PolicyRecord.objects.filter(case=case, replacement__isnull=True)
    facts = [{'text': f'Policy {p.reference}, {p.start} to {p.end}, limit {p.currency} {p.limit}, deductible {p.deductible}',
              'source_type': 'policy', 'source_id': p.pk, 'source_date': p.created_at.isoformat()} for p in policies]
    from ai_review.models import ExtractionResult
    from ai_review.permissions import require_current_result
    from ai_review.errors import ProcessingError
    from ai_review.rules import effective_facts
    for result in ExtractionResult.objects.filter(job__case=case).select_related('job__version'):
        try:
            require_current_result(user, result)
        except (Http404, ValidationError, ProcessingError):
            continue
        reviewed, conflicts = effective_facts(result)
        if conflicts:
            continue
        for field, fact in reviewed.items():
            facts.append({'text': f'{field}: {fact["value"]}', 'source_type': 'reviewed_fact', 'source_id': str(result.job_id),
                'source_date': result.created_at.isoformat(), 'citations': fact['citations'], 'review_id': fact['review_id']})
    payload = {'label': 'DEMO preparation notes — no quote, premium or binding coverage', 'facts': facts,
        'controls': controls, 'open_conditions': [{'title': f.title, 'priority': f.priority, 'source_id': str(f.pk), 'due_date': str(f.due_date)} for f in case.findings.exclude(state='closed')],
        'claims': [{'reference': c.reference, 'policy': c.policy.reference, 'incident_date': str(c.incident_date), 'loss': str(c.incurred_loss), 'currency': c.policy.currency, 'downtime_hours': str(c.downtime_hours), 'source': c.source} for c in Claim.objects.filter(case=case, replacement__isnull=True).select_related('policy')],
        'follow_up': ['Resolve open evidence and source discrepancies.', 'Review requested coverage, exclusions and terms manually.'],
        'method': 'Deterministic preparation from dated records and reviewed extracted facts; not a live AI generation.'}
    return save_record(QuoteDraft, user, case, payload=normalize(payload), context_sha256=quote_context(case))


@transaction.atomic
def review_quote(user, case, draft, outcome, notes):
    case = locked_case(user, case)
    require_decider(user, case)
    same_case(case, draft)
    rationale(notes)
    if QuoteReview.objects.filter(draft=draft).exists():
        raise ValidationError('This draft already has a disposition. Prepare a new draft.')
    if quote_context(case) != draft.context_sha256:
        raise ValidationError('Case evidence changed. Prepare a new draft before approval.')
    return save_record(QuoteReview, user, case, draft=draft, outcome=outcome, notes=notes)


@transaction.atomic
def outage(user, case, provider, hours, assumptions):
    case = locked_case(user, case)
    require_decider(user, case)
    if provider.insurer_id != case.insurer_id or hours <= 0 or not hours.is_finite():
        raise ValidationError('Choose a provider for this insurer and a positive outage duration.')
    rationale(assumptions)
    eligible = portfolio.eligible_cases(user, case.insurer)
    rows = []
    for dependency in portfolio.Dependency.objects.filter(case__in=eligible, provider=provider, retirement__isnull=True).select_related('case__applicant', 'service', 'permission'):
        if not portfolio.permission_active(dependency.permission):
            continue
        target = dependency.service.current_policy
        target_hours = target.duration_minutes / 60 if target else None
        rows.append({'case_id': str(dependency.case_id), 'case': dependency.case.reference, 'business_id': str(dependency.case.applicant_id),
            'business': dependency.case.applicant.name, 'service': dependency.service.name, 'critical': dependency.critical,
            'permission_id': dependency.permission_id, 'dependency_id': dependency.pk,
            'target_hours': str(target_hours) if target_hours is not None else None,
            'exceeds_target': hours > target_hours if target_hours is not None else None,
            'excess_hours': str(max(Decimal(0), hours - target_hours)) if target_hours is not None else None,
            'source': dependency.source, 'observed_at': dependency.observed_at.isoformat(),
            'policies': list(PolicyRecord.objects.filter(case=dependency.case, replacement__isnull=True).values('reference', 'start', 'end'))})
    if not rows:
        raise ValidationError('No currently permitted dependencies are available for that provider.')
    payload = {'business_count': len({r['business_id'] for r in rows}), 'case_count': len({r['case_id'] for r in rows}), 'services': rows,
        'interpretation': 'Scenario assumes provider outage duration equals service interruption, with no failover. This is not a loss prediction or proof of recovery.'}
    return save_record(OutageScenario, user, case, provider=provider, hours=hours, assumptions=assumptions, payload=normalize(payload))


def require_outage(user, scenario):
    require_reviewer(user, scenario.case)
    eligible = {str(c.pk) for c in portfolio.eligible_cases(user, scenario.case.insurer)}
    for row in scenario.payload['services']:
        permission = portfolio.Permission.objects.get(pk=row['permission_id'])
        if row['case_id'] not in eligible or not portfolio.permission_active(permission):
            raise Http404
    return scenario


@transaction.atomic
def approve_demo(user, case, area, reason):
    case = locked_case(user, case)
    require_decider(user, case)
    demo_required(case)
    if area not in AREAS:
        raise ValidationError('Choose a demo acceptance area.')
    rationale(reason)
    return save_record(DemoApproval, user, case, area=area, rationale=reason, context_sha256=quote_context(case))
