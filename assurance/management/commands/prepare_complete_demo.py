"""Add isolated full-feature fictional cases; never reset existing accounts or cases."""
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from evidence.services import ensure_case_service, upload_evidence, review_evidence, assess_control
from monitoring import services as monitoring
from monitoring.models import SyncRun, Observation
from monitoring.tasks import run_sync
from portfolio import services as portfolio
from underwriting import services as underwriting
from workspaces.management.commands.prepare_walkthrough_demo import Command as Walkthrough
from workspaces.models import Case, ChecklistVersion, EvidenceRequest, Membership, Organization, SharingGrant, User
from assurance import services
from assurance.models import DemoCase, PolicyRecord


class Command(BaseCommand):
    help = 'Create FULL-01 through FULL-03 for existing demo accounts; no live services or credentials are changed.'

    def handle(self, **options):
        if not settings.DEBUG or settings.EVIDENCE_STORAGE_BACKEND != 'local' or settings.EVIDENCE_SCAN_MODE != 'basic':
            raise CommandError('Complete demo requires local debug mode, private local evidence and development scanning.')
        required = ['underwriter@example.test', 'owner@example.test', 'technician@example.test']
        if User.objects.filter(email__in=required, is_active=True).count() != 3:
            raise CommandError('Run seed_phase2 first to create the original fictional accounts.')
        # Existing walkthroughs supply a separate fictional partner account, never reset.
        call_command('prepare_walkthrough_demo', stdout=self.stdout)
        user, owner, tech = [User.objects.get(email=email) for email in required]
        membership = Membership.objects.filter(user=user, is_active=True, role='underwriter').first()
        if not membership:
            raise CommandError('The demo underwriter needs an active insurer membership.')
        for index in range(1, 4):
            self.prepare(index, user, owner, tech, membership.organization)
        self.stdout.write('Full demonstration ready: FULL-01, FULL-02, FULL-03. Use your existing demo accounts. AWS and external feeds were not contacted.')

    @transaction.atomic
    def prepare(self, index, user, owner, tech, insurer):
        Organization.objects.select_for_update().get(pk=insurer.pk)
        ref = f'FULL-{index:02}'
        prior = Case.objects.filter(reference=ref).first()
        if prior:
            if prior.insurer_id != insurer.pk or not DemoCase.objects.filter(case=prior).exists():
                raise CommandError(f'{ref} already exists outside this demonstration. Nothing overwritten.')
            self.stdout.write(f'{ref}: preserved existing data and approvals.')
            return
        now = timezone.now()
        sample = json.loads((settings.BASE_DIR / 'pilot/data/pilot.json').read_text())
        requirements = [{'name': r['name'], 'evidence': r['evidence'], 'freshness_days': int(r['freshness'].split()[0])} for r in sample['checklist']]
        checklist, _ = ChecklistVersion.objects.get_or_create(insurer=insurer, version='complete-demo-v1', defaults={'product': 'Commercial cyber', 'requirements': requirements, 'recovery_targets': sample['targets']})
        business = Organization.objects.create(name=['Aster Commerce (fictional)', 'Brook Manufacturing (fictional)', 'Cove Services (fictional)'][index-1], kind='business')
        case = Case.objects.create(reference=ref, insurer=insurer, applicant=business, checklist=checklist, kind='renewal', service='Order processing',
            assignee=user, created_by=user, due_date=(now + timedelta(days=14)).date(), consent_granted_by=owner,
            consent_expires_at=now + timedelta(days=90), internal_notes='FICTIONAL COMPLETE DEMO. Self-approved demonstration only.')
        DemoCase.objects.create(case=case, actor=user)
        for account, role in ((owner, 'owner'), (tech, 'technician')):
            SharingGrant.objects.create(case=case, user=account, role=role, granted_by=user, expires_at=now + timedelta(days=90), organization_name='Fictional demo organization')
        items = {}
        for r in requirements:
            title = r['name']
            items[title] = EvidenceRequest.objects.create(case=case, title=title, description=r['evidence'], freshness_days=r['freshness_days'],
                kind={'Supervised restore': 'restore', 'Backup coverage': 'backup'}.get(title, 'control'), due_date=case.due_date, assigned_to=tech)
        service = ensure_case_service(case)
        restore_item = items['Supervised restore']
        first = Walkthrough().restore(case, service, restore_item, user, tech, now, 420)
        if index > 1:
            retest = Walkthrough().restore(case, service, restore_item, user, tech, now, 180, parent=first)
            assess_control(user, restore_item, 'verified', 'FICTIONAL three-hour retest verified.', list(retest.evidence_versions.all()))
        mfa_item = items['Privileged account MFA']
        body = b'FICTIONAL DEMO ONLY\nService: Order processing\nMFA coverage: 100%\nScope: Privileged accounts in fictional tenant\nSource: Manual identity export\n'
        version, _ = upload_evidence(tech, mfa_item, SimpleUploadedFile('fictional-mfa.txt', body), service, 'Fictional MFA export',
            'Fictional manual identity export', 'Privileged accounts', now - timedelta(hours=2), uuid.uuid4())
        review_evidence(user, version, 'accepted', True, 'FICTIONAL source accepted; comparison with monitoring remains open.')
        policy = services.record_policy(user, case, {'reference': f'FICTIONAL-POLICY-{index}', 'start': (now - timedelta(days=365)).date(),
            'end': (now + timedelta(days=30)).date(), 'currency': 'USD', 'limit': Decimal('1000000'), 'deductible': Decimal('10000'),
            'source': 'Fictional policy schedule, demonstration only', 'decision': None, 'supersedes': None})
        claim = services.record_claim(user, case, {'policy': policy, 'reference': f'FICTIONAL-CLAIM-{index}', 'incident_date': (now - timedelta(days=45)).date(),
            'incurred_loss': Decimal(10000 * index), 'downtime_hours': Decimal(8 * index), 'maturity': 'developing',
            'source': 'Fictional loss register', 'explanation': 'Invented outage for demonstration. No historical underwriting decision is asserted.', 'supersedes': None})
        if index == 1:
            services.record_claim(user, case, {'policy': policy, 'reference': claim.reference, 'incident_date': claim.incident_date,
                'incurred_loss': Decimal('12500'), 'downtime_hours': Decimal('10'), 'maturity': 'mature', 'source': 'Fictional corrected loss register',
                'explanation': 'Demonstrates an append-only loss-development correction.', 'supersedes': claim})
        services.publish_rule(user, case, services.DEFAULT_WEIGHTS, 'Self-approved fictional demo rule. Not a predictive loss score.')
        portfolio.authorize(owner, case, now + timedelta(days=60), True)
        dependency = portfolio.record_dependency(user, case, service, 'Fictional Shared Cloud', 'complete-demo-shared-cloud', True, 'Fictional service dependency register', now - timedelta(days=1))
        assets = []
        for kind, identifier, name, item in [
            ('tenant', str(uuid.uuid4()), 'Fictional identity tenant', mfa_item),
            ('ip', f'192.0.2.{10+index}', 'Fictional public service', mfa_item),
            ('domain', f'demo{index}.example.test', 'Fictional verified domain', items['Access review']),
            ('aws_resource', f'arn:aws:ec2:us-east-1:123456789012:volume/vol-demo{index}', 'Fictional backup resource', items['Backup coverage'])]:
            assets.append(monitoring.create_asset(user, case, {'kind': kind, 'identifier': identifier, 'name': name, 'request': item, 'service': service}))
        monitor_policy = monitoring.publish_policy(user, case, {'min_mfa_percent': Decimal(100), 'max_backup_age_hours': 24, 'freshness_hours': 48,
            'poll_minutes': 60, 'blocked_ports': [3389], 'due_days': 7, 'auto_followup': False, 'rationale': 'Fictional demo monitoring rules.'})
        connection = monitoring.create_connection(user, case, 'Fictional identity, exposure and backup observations', 'demo', assets, {'scenario': 'risk'})
        authorization = monitoring.authorize(owner, connection, now + timedelta(days=60), True)
        run = SyncRun.objects.create(connection=connection, authorization=authorization, policy=monitor_policy, requested_by=user, slot='complete-demo-initial')
        run_sync(run.pk)
        run.refresh_from_db()
        if run.status != 'completed':
            raise CommandError(f'Fictional monitoring failed: {run.error_code}')
        obs = Observation.objects.filter(group__asset=assets[0]).first()
        services.compare(user, case, {'version': version, 'observation': obs, 'topic': 'Manual MFA export versus identity registration observation',
            'internal_value': 'Manual export claims 100% privileged-account MFA coverage.', 'external_value': 'Fictional registration observation has incomplete coverage; registration is not enforcement. Confirm population and scope.'})
        for category, asset, text, recommendation in [
            ('exposure', assets[1], 'Fictional external service observation requires confirmation.', 'Confirm port ownership; restrict remote access; submit firewall evidence and request a recheck.'),
            ('credential', assets[2], 'Fictional exposure feed reported historical domain exposure counts.', 'Investigate the authorized domain; determine applicability, revoke affected sessions and document credential reset if warranted.')]:
            observation = Observation.objects.filter(group__asset=asset).first()
            finding = underwriting.save_finding(user, case, {'request': asset.request, 'title': 'Review ' + asset.name, 'description': text, 'kind': 'fix', 'priority': 'high',
                'priority_reason': 'Unverified external signal affecting a critical demo service.', 'owner': tech, 'due_date': case.due_date, 'retest_of': None})
            risk = services.publish_risk(user, case, {'finding': finding, 'service': service, 'category': category, 'summary': text, 'recommendation': recommendation, 'observation': observation})
            services.prioritize(user, case, finding, {'impact': 4, 'exposure': 3, 'uncertainty': 3, 'recovery': 2}, 'Fictional critical service with an unverified external signal.', revision=finding.revision)
            services.investigate(tech, case, risk, 'investigate', 'Fictional technician is checking the affected service.')
        services.record_software(user, case, {'asset': assets[1], 'product': 'Fictional Commerce Gateway', 'version': '2.3.0',
            'source': 'Fictional dated software inventory', 'observed_at': now - timedelta(hours=1), 'supersedes': None})
        services.record_advisory(user, case, {'reference': f'DEMO-ADVISORY-{index}', 'product': 'Fictional Commerce Gateway', 'minimum': '2.0.0', 'fixed': '2.4.0',
            'source': 'Invented advisory for demonstration; not a real CVE', 'remediation': 'In this fictional scenario, update to 2.4.0 or later, verify service functionality and submit a new inventory.'})
        services.match_vulnerabilities(user, case)
        for item in items.values():
            if item.kind == 'restore' and index > 1:
                continue
            underwriting.save_finding(user, case, {'request': item, 'title': 'Complete ' + item.title, 'description': 'Provide dated scope-matched evidence and reviewer verification.',
                'kind': 'retest' if item.kind == 'restore' else 'evidence', 'priority': 'high', 'priority_reason': 'Open evidence or recovery gap in the fictional demonstration.',
                'owner': tech, 'due_date': case.due_date, 'retest_of': first if item.kind == 'restore' else None})
        services.prepare_quote(user, case)
        services.outage(user, case, dependency.provider, Decimal(8), 'Fictional eight-hour provider outage, no failover; compare against four-hour service recovery target.')
        if index == 3:
            findings = list(case.findings.exclude(state='closed'))
            snapshot = underwriting.prepare_decision(user, case)
            decision = underwriting.record_decision(user, snapshot, 'conditional', 'FICTIONAL DEMO: recovery passed; outstanding evidence and risks remain explicit conditions.', findings, True)
            report = underwriting.draft_report(user, decision, 'Fictional complete-demo summary', 'Three-hour recovery retest passed. Outstanding identity, access, backup and external findings remain conditions.')
            underwriting.approve_report(user, report, 'Self-approved fictional demonstration report.')
            partner = User.objects.get(email='walkthrough-partner@example.test')
            relation = portfolio.create_relationship(user, case, 'Fictional Demo Capacity Partner', partner, 'FICTIONAL-DEMO-AGREEMENT-NO-COMMERCIAL-EFFECT', now + timedelta(days=60), True)
            submission = portfolio.prepare_submission(user, case, relation, report, now + timedelta(days=14))
            portfolio.consent_submission(owner, submission, True)
            portfolio.release_submission(user, submission, 'Release the fictional demo summary only.')
        self.stdout.write(f'{ref}: created policy, claims, risk actions, comparisons, software matches, preparation draft and outage scenario.')
