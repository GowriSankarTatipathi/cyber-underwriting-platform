from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.http import Http404
from django.test import TestCase, SimpleTestCase, override_settings, Client
from django.urls import reverse
from django.utils import timezone
from evidence import tests as evidence_tests
from evidence.services import review_evidence
from monitoring import services as monitoring
from monitoring.models import Observation, SyncRun
from monitoring.tasks import run_sync
from portfolio import services as portfolio
from underwriting import services as underwriting
from underwriting.snapshots import case_snapshot, checksum
from workspaces.models import Case, SharingGrant, User
from . import models, services, research, forms


@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND='local', EVIDENCE_SCAN_MODE='basic')
class AssuranceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        evidence_tests.EvidenceTests.setUpTestData.__func__(cls)

    setUp = evidence_tests.EvidenceTests.setUp
    login = evidence_tests.EvidenceTests.login
    upload = evidence_tests.EvidenceTests.upload

    def policy(self, **changes):
        data = dict(reference='POLICY-001', start=timezone.localdate()-timedelta(days=365), end=timezone.localdate()+timedelta(days=1),
            currency='USD', limit=Decimal(100000), deductible=Decimal(1000), source='Fictional schedule', decision=None, supersedes=None)
        data.update(changes)
        return services.record_policy(self.underwriter, self.case, data)

    def claim_data(self, policy):
        return dict(policy=policy, reference='CLAIM-001', incident_date=timezone.localdate()-timedelta(days=20), incurred_loss=Decimal(10000),
            downtime_hours=Decimal(10), maturity='developing', source='Fictional register', explanation='Demo outage', supersedes=None)

    def finding(self):
        return underwriting.save_finding(self.underwriter, self.case, dict(request=self.control_request, title='Check identity exposure',
            description='Customer-safe investigation', kind='fix', priority='high', priority_reason='Unknown exposure', owner=self.tech,
            due_date=timezone.localdate()+timedelta(days=10), retest_of=None))

    def observation(self, scenario='risk'):
        asset = monitoring.create_asset(self.underwriter, self.case, dict(request=self.control_request, service=self.service, name='Fictional identity', kind='tenant', identifier='11111111-1111-4111-8111-111111111111'))
        policy = monitoring.publish_policy(self.underwriter, self.case, dict(min_mfa_percent=Decimal(100), max_backup_age_hours=24, freshness_hours=48,
            poll_minutes=60, blocked_ports=[3389], due_days=7, auto_followup=False, rationale='Demo'))
        connection = monitoring.create_connection(self.underwriter, self.case, 'Demo', 'demo', [asset], {'scenario': scenario})
        authorization = monitoring.authorize(self.owner, connection, timezone.now()+timedelta(days=10), True)
        run = SyncRun.objects.create(connection=connection, authorization=authorization, policy=policy, requested_by=self.underwriter, slot='test')
        run_sync(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed', run.error_code)
        return Observation.objects.get(group__asset=asset)

    def risk(self):
        return services.publish_risk(self.underwriter, self.case, dict(finding=self.finding(), service=self.service, category='credential',
            summary='Approved customer message', recommendation='Investigate and submit dated proof', observation=None))

    def test_policy_dates_currency_amounts_and_history(self):
        for changes in ({'end': timezone.localdate()-timedelta(days=400)}, {'currency': 'usd'}, {'limit': Decimal(-1)}, {'deductible': Decimal(200000)}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.policy(**changes)
        first = self.policy()
        with self.assertRaises(ValidationError):
            self.policy()
        second = self.policy(supersedes=first, limit=Decimal(200000))
        self.assertEqual(second.supersedes_id, first.pk)
        first.refresh_from_db()
        self.assertEqual(first.limit, Decimal(100000))

    def test_claim_corrections_duplicate_and_coverage_validation(self):
        p = self.policy()
        data = self.claim_data(p)
        first = services.record_claim(self.underwriter, self.case, data)
        with self.assertRaises(ValidationError):
            services.record_claim(self.underwriter, self.case, data)
        correction = services.record_claim(self.underwriter, self.case, {**data, 'supersedes': first, 'incurred_loss': Decimal(12000)})
        self.assertEqual(correction.supersedes_id, first.pk)
        for bad in ({'incident_date': p.end}, {'incurred_loss': Decimal(-1)}, {'downtime_hours': Decimal(-1)}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                services.record_claim(self.underwriter, self.case, {**data, 'reference': 'CLAIM-NEW', **bad})

    def test_policy_correction_cannot_reassign_claim_currency_or_coverage(self):
        p = self.policy()
        services.record_claim(self.underwriter, self.case, self.claim_data(p))
        with self.assertRaises(ValidationError):
            self.policy(supersedes=p, currency='EUR')
        with self.assertRaises(ValidationError):
            self.policy(supersedes=p, start=timezone.localdate())

    def test_import_is_atomic_idempotent_and_validates_foreign_keys(self):
        p = self.policy()
        data = self.claim_data(p)
        data.pop('policy'); data.pop('supersedes')
        raw = services.normalize({**data, 'policy_reference': p.reference})
        with self.assertRaises(ValidationError):
            services.import_claims(self.underwriter, self.case, [raw, {**raw, 'reference': 'BAD', 'policy_reference': 'another-case'}])
        self.assertEqual(models.Claim.objects.count(), 0)
        first = services.import_claims(self.underwriter, self.case, [raw])
        self.assertEqual(services.import_claims(self.underwriter, self.case, [raw]).pk, first.pk)
        self.assertEqual(models.Claim.objects.count(), 1)
        with self.assertRaises(ValidationError):
            services.import_claims(self.underwriter, self.case, [{**raw, 'case': 'tampered'}])

    def test_priority_rule_score_override_and_stale_revision(self):
        f = self.finding()
        rule = services.publish_rule(self.underwriter, self.case, services.DEFAULT_WEIGHTS, 'Demo weights')
        row = services.prioritize(self.underwriter, self.case, f, dict(impact=4, exposure=4, uncertainty=4, recovery=4), 'Override for documented reason', 'high', f.revision)
        self.assertEqual((row.score, row.suggested, row.selected), (100, 'critical', 'high'))
        f.refresh_from_db()
        self.assertEqual(f.priority, 'high')
        with self.assertRaises(ValidationError):
            services.prioritize(self.underwriter, self.case, f, dict(impact=0, exposure=0, uncertainty=0, recovery=0), 'Stale', revision=1)
        with self.assertRaises(ValidationError):
            services.publish_rule(self.underwriter, self.case, {'impact': 100}, 'Invalid')
        with self.assertRaises(ValidationError):
            services.compute_score(dict(impact=-1, exposure=0, uncertainty=0, recovery=0), rule.weights)

    def test_comparison_pins_sources_and_rejects_review_after_change(self):
        v = self.upload(self.control_request)
        o = self.observation()
        c = services.compare(self.underwriter, self.case, dict(version=v, observation=o, topic='MFA scope', internal_value='100%', external_value='Partial'))
        services.resolve_comparison(self.underwriter, self.case, c, 'clarify', 'Different account populations need investigation')
        self.assertTrue(services.comparison_current(c))
        self.upload(self.control_request, body=b'New scope record', document=v.document)
        self.assertFalse(services.comparison_current(c))
        with self.assertRaises(ValidationError):
            services.resolve_comparison(self.underwriter, self.case, c, 'internal', 'Old source')

    def test_comparison_rejects_wrong_service(self):
        from evidence.models import Service
        v = self.upload(self.control_request)
        o = self.observation()
        other = Service.objects.create(case=self.case, name='Other', owner_name='Owner', created_by=self.underwriter)
        v.document.service = other
        v.document.save()
        with self.assertRaises(ValidationError):
            services.compare(self.underwriter, self.case, dict(version=v, observation=o, topic='Bad', internal_value='x', external_value='y'))

    def test_customer_risk_permissions_and_internal_privacy(self):
        risk = self.risk()
        self.case.internal_notes = 'PRIVATE-NOTES-NEVER-PUBLISH'
        self.case.save()
        self.login(self.tech)
        response = self.client.get(reverse('assessment_section', args=[self.case.pk, 'risks']))
        self.assertContains(response, 'Approved customer message')
        self.assertNotContains(response, 'PRIVATE-NOTES-NEVER-PUBLISH')
        for section in ('overview', 'policies', 'quotes', 'priorities', 'software', 'outages'):
            self.assertEqual(self.client.get(reverse('assessment_section', args=[self.case.pk, section])).status_code, 404)
        self.control_request.assigned_to = None
        self.control_request.save()
        self.assertEqual(self.client.get(reverse('assessment_risk', args=[self.case.pk, risk.pk])).status_code, 404)

    def test_investigation_cannot_close_or_self_verify(self):
        risk = self.risk()
        services.investigate(self.tech, self.case, risk, 'dismiss_request', 'Investigated potential false match')
        with self.assertRaises(Http404):
            services.investigate(self.tech, self.case, risk, 'dismiss', 'Self approval')
        with self.assertRaises(ValidationError):
            services.investigate(self.underwriter, self.case, risk, 'dismiss', 'No evidence')
        v = self.upload(self.control_request)
        services.investigate(self.underwriter, self.case, risk, 'dismiss', 'Reviewed accepted evidence', version=v)
        risk.finding.refresh_from_db()
        self.assertEqual(risk.finding.state, 'open')

    def test_risk_access_revocation_blocks_post_and_get(self):
        risk = self.risk()
        SharingGrant.objects.filter(pk=self.tech_grant.pk).update(revoked_at=timezone.now())
        with self.assertRaises(Http404):
            services.investigate(self.tech, self.case, risk, 'investigate', 'Cannot after revoke')
        self.login(self.tech)
        self.assertEqual(self.client.get(reverse('assessment_risk', args=[self.case.pk, risk.pk])).status_code, 404)

    def software(self):
        o = self.observation()
        return services.record_software(self.underwriter, self.case, dict(asset=o.group.asset, product='Demo Product', version='2.3.1',
            source='Inventory', observed_at=timezone.now()-timedelta(hours=1), supersedes=None))

    def advisory(self):
        return services.record_advisory(self.underwriter, self.case, dict(reference='DEMO-1', product='demo product', minimum='2.0', fixed='2.4', source='Fictional bulletin', remediation='Patch and verify'))

    def test_product_matching_deduplication_and_fixed_boundary(self):
        s = self.software(); self.advisory()
        self.assertEqual(services.match_vulnerabilities(self.underwriter, self.case), 1)
        self.assertEqual(services.match_vulnerabilities(self.underwriter, self.case), 0)
        services.record_software(self.underwriter, self.case, dict(asset=s.asset, product=s.product, version='2.4.0', source='Patched inventory', observed_at=timezone.now(), supersedes=s))
        self.assertEqual(services.match_vulnerabilities(self.underwriter, self.case), 0)
        self.assertEqual(models.VulnerabilityMatch.objects.count(), 1)
        self.assertEqual(models.PublishedRisk.objects.first().finding.state, 'open')

    def test_stale_inventory_and_unsupported_versions_do_not_match(self):
        s = self.software(); self.advisory()
        models.Software.objects.filter(pk=s.pk).update(observed_at=timezone.now()-timedelta(days=500))
        self.assertEqual(services.match_vulnerabilities(self.underwriter, self.case), 0)
        for value in ('2.3-beta', '2.x', '-1', '1.2.3.4.5'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                services.version_tuple(value)

    def test_quote_approval_and_source_change_guard(self):
        self.policy()
        draft = services.prepare_quote(self.underwriter, self.case)
        services.review_quote(self.underwriter, self.case, draft, 'approved', 'Demo only')
        self.assertIn('no quote', draft.payload['label'])
        draft2 = services.prepare_quote(self.underwriter, self.case)
        self.finding()
        with self.assertRaises(ValidationError):
            services.review_quote(self.underwriter, self.case, draft2, 'approved', 'Stale')
        with self.assertRaises(Http404):
            services.prepare_quote(self.tech, self.case)

    def test_decisions_capture_assessment_material_and_change_invalidates_snapshot(self):
        old = underwriting.prepare_decision(self.underwriter, self.case)
        self.policy()
        self.assertIn('assessment', case_snapshot(self.case))
        with self.assertRaises(ValidationError):
            underwriting.record_decision(self.underwriter, old, 'deferred', 'Stale review', [], True)

    def test_outage_deduplicates_business_and_revocation_blocks_history(self):
        permission = portfolio.authorize(self.owner, self.case, timezone.now()+timedelta(days=10), True)
        dep = portfolio.record_dependency(self.underwriter, self.case, self.service, 'Provider', 'provider', True, 'Inventory', timezone.now())
        row = services.outage(self.underwriter, self.case, dep.provider, Decimal(8), 'No failover')
        self.assertEqual(row.payload['business_count'], 1)
        self.assertTrue(row.payload['services'][0]['exceeds_target'])
        self.assertEqual(row.payload['services'][0]['excess_hours'], '4.00')
        services.require_outage(self.underwriter, row)
        portfolio.revoke_permission(self.owner, permission)
        with self.assertRaises(Http404):
            services.require_outage(self.underwriter, row)

    def test_demo_approval_is_scoped_and_requires_debug(self):
        with self.assertRaises(ValidationError):
            services.approve_demo(self.underwriter, self.case, 'rules', 'Self approval')
        models.DemoCase.objects.create(case=self.case, actor=self.underwriter)
        a = services.approve_demo(self.underwriter, self.case, 'rules', 'Demo only')
        self.assertEqual(a.context_sha256, services.quote_context(self.case))
        self.policy()
        self.assertNotEqual(a.context_sha256, services.quote_context(self.case))
        with override_settings(DEBUG=False), self.assertRaises(ValidationError):
            services.approve_demo(self.underwriter, self.case, 'rules', 'No production approval')

    def test_all_staff_pages_forms_and_exports_render(self):
        models.DemoCase.objects.create(case=self.case, actor=self.underwriter)
        self.login(self.underwriter)
        for name in ('overview', 'policies', 'risks', 'priorities', 'comparisons', 'software', 'quotes', 'outages', 'research', 'acceptance'):
            with self.subTest(section=name):
                self.assertEqual(self.client.get(reverse('assessment_section', args=[self.case.pk, name])).status_code, 200)
        for action in ('policy', 'claim', 'comparison', 'risk', 'software', 'advisory', 'import', 'rule', 'outage', 'approval'):
            with self.subTest(action=action):
                self.assertEqual(self.client.get(reverse('assessment_edit', args=[self.case.pk, action])).status_code, 200)
        for kind in ('claims', 'sample-import', 'research'):
            self.assertEqual(self.client.get(reverse('assessment_export', args=[self.case.pk, kind])).status_code, 200)
        for action in ('quote', 'match'):
            self.assertEqual(self.client.get(reverse('assessment_run', args=[self.case.pk, action])).status_code, 405)

    def test_form_post_validation_and_csrf(self):
        self.login(self.underwriter)
        response = self.client.post(reverse('assessment_edit', args=[self.case.pk, 'policy']), {'reference': 'incomplete'})
        self.assertContains(response, 'Nothing has been saved')
        self.assertEqual(models.PolicyRecord.objects.count(), 0)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.underwriter)
        with override_settings(LOCAL_PASSWORD_ONLY=True):
            self.assertEqual(csrf_client.post(reverse('assessment_run', args=[self.case.pk, 'quote'])).status_code, 403)

    def test_cross_case_choices_are_rejected(self):
        other = Case.objects.create(reference='OTHER', insurer=self.insurer, applicant=self.business, checklist=self.checklist,
            service='Other', created_by=self.outsider, assignee=self.outsider)
        p = self.policy()
        form = forms.ClaimForm(other, services.normalize({**self.claim_data(p), 'policy': p.pk}))
        self.assertFalse(form.is_valid())
        self.assertIn('policy', form.errors)
        with self.assertRaises(Http404):
            services.record_claim(self.outsider, self.case, self.claim_data(p))


class SyntheticResearchTests(SimpleTestCase):
    def test_reproducible_disjoint_data_and_intervals(self):
        first, second = research.report(), research.report()
        self.assertEqual(first, second)
        metrics = first['metrics']
        self.assertFalse(set(metrics['train_ids']) & set(metrics['holdout_ids']))
        for bounds in metrics['bootstrap_95_percent_intervals'].values():
            self.assertLessEqual(bounds[0], bounds[1])
        self.assertIn('SYNTHETIC', first['label'])

    def test_holdout_changes_do_not_change_fitted_estimates(self):
        rows = research.fixture()
        first = research.evaluate(rows)
        for row in rows:
            if row['start'] >= '2024-01-01':
                row['loss'] *= 10
                row['downtime_hours'] *= 10
        changed = research.evaluate(rows)
        self.assertEqual(first['baseline'], changed['baseline'])
        self.assertEqual(first['bootstrap_95_percent_intervals'], changed['bootstrap_95_percent_intervals'])
        self.assertNotEqual(first['downtime_holdout_mae'], changed['downtime_holdout_mae'])

    def test_insufficient_data_stays_insufficient(self):
        self.assertEqual(research.evaluate(research.fixture()[:4])['status'], 'insufficient_data')


@override_settings(DEBUG=True, LOCAL_PASSWORD_ONLY=True, EVIDENCE_STORAGE_BACKEND='local', EVIDENCE_SCAN_MODE='basic')
class CompleteDemoTests(TestCase):
    def setUp(self):
        import tempfile
        import shutil
        from pathlib import Path
        from django.conf import settings
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'pilot/data').mkdir(parents=True)
        shutil.copy(settings.BASE_DIR / 'pilot/data/pilot.json', self.root / 'pilot/data/pilot.json')
        self.override = override_settings(BASE_DIR=self.root, EVIDENCE_LOCAL_ROOT=self.root / '.local/evidence', PRIVATE_FILE_ROOT=self.root / '.local/private')
        self.override.enable()
        self.addCleanup(self.override.disable)
        call_command('seed_phase2', stdout=StringIO())

    def seed(self):
        call_command('prepare_complete_demo', stdout=StringIO())

    def test_complete_demo_and_idempotency(self):
        self.seed()
        self.assertEqual(models.DemoCase.objects.count(), 3)
        self.assertEqual(models.VulnerabilityMatch.objects.count(), 3)
        self.assertEqual(models.Claim.objects.count(), 4)
        before = dict(User.objects.values_list('email', 'password'))
        count = models.PolicyRecord.objects.count()
        case = Case.objects.get(reference='FULL-03')
        self.assertTrue(hasattr(case.capacity_submissions.get(), 'release'))
        self.seed()
        self.assertEqual(models.PolicyRecord.objects.count(), count)
        self.assertEqual(dict(User.objects.values_list('email', 'password')), before)
        from portfolio.models import Outcome
        self.assertEqual(Outcome.objects.count(), 0)

    def test_populated_pages_and_self_approval_journey(self):
        self.seed()
        user = User.objects.get(email='underwriter@example.test')
        self.client.force_login(user, backend='django.contrib.auth.backends.ModelBackend')
        case = Case.objects.get(reference='FULL-03')
        for section in ('overview', 'policies', 'risks', 'priorities', 'comparisons', 'software', 'quotes', 'outages', 'research', 'acceptance'):
            with self.subTest(section=section):
                self.assertEqual(self.client.get(reverse('assessment_section', args=[case.pk, section])).status_code, 200)
        for name, rows in [('assessment_risk', models.PublishedRisk.objects.filter(case=case)), ('assessment_quote', models.QuoteDraft.objects.filter(case=case)), ('assessment_outage', models.OutageScenario.objects.filter(case=case))]:
            for row in rows:
                self.assertEqual(self.client.get(reverse(name, args=[case.pk, row.pk])).status_code, 200)
        response = self.client.post(reverse('assessment_edit', args=[case.pk, 'approval']), {'area': 'journey', 'rationale': 'Demo journey reviewed by project owner.', 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(reverse('assessment_section', args=[case.pk, 'acceptance'])), 'Approved for current assessment')
        draft = services.prepare_quote(user, case)
        response = self.client.post(reverse('assessment_quote', args=[case.pk, draft.pk]), {'outcome': 'approved', 'notes': 'Demo notes reviewed.'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(models.QuoteReview.objects.filter(draft=draft).exists())

    def test_seed_rejects_nonlocal_configuration(self):
        for config in ({'DEBUG': False}, {'EVIDENCE_STORAGE_BACKEND': 's3'}, {'EVIDENCE_SCAN_MODE': 'external'}):
            with self.subTest(config=config), override_settings(**config), self.assertRaises(CommandError):
                self.seed()
        self.assertEqual(models.DemoCase.objects.count(), 0)


@override_settings(DEBUG=True, EVIDENCE_STORAGE_BACKEND='local', EVIDENCE_SCAN_MODE='basic')
class DemoBoundaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        evidence_tests.EvidenceTests.setUpTestData.__func__(cls)

    setUp = evidence_tests.EvidenceTests.setUp
    login = evidence_tests.EvidenceTests.login

    def test_unmarked_case_and_production_demo_routes_are_not_found(self):
        self.login(self.underwriter)
        urls = [reverse('assessment_section', args=[self.case.pk, section]) for section in ('research', 'acceptance')]
        urls += [reverse('assessment_edit', args=[self.case.pk, 'approval']), reverse('assessment_export', args=[self.case.pk, 'research'])]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404)
        models.DemoCase.objects.create(case=self.case, actor=self.underwriter)
        with override_settings(DEBUG=False):
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_partner_cannot_access_assessment_exports(self):
        self.login(self.outsider)
        for url in [reverse('assessment', args=[self.case.pk]), reverse('assessment_export', args=[self.case.pk, 'claims'])]:
            self.assertEqual(self.client.get(url).status_code, 404)
