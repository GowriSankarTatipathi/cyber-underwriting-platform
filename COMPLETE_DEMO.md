# Complete cyber underwriting demonstration

This iteration fills the demonstration gaps across the 16-feature roadmap. The project owner can act as the fictional underwriter and approve the demo. Real insurer participation, AWS/S3 setup, external feed subscriptions and live model access are not prerequisites for the local demonstration.

## Prepare and open

With the existing virtual environment, project database and queue running:

```sh
python manage.py migrate
python manage.py prepare_complete_demo
python manage.py runserver 127.0.0.1:8000
```

The seed command requires debug mode, local evidence storage and basic development scanning. It preserves existing cases, passwords and approvals. It does not poll external services. Rerunning it skips existing complete-demo cases instead of resetting user changes. The existing `seed_phase2` accounts must already exist. Three isolated `FULL-` cases appear for `underwriter@example.test`, `owner@example.test` and `technician@example.test`.

Open **Demo walkthrough**, select a `FULL-` case, then **Unified assessment**. Business owners and assigned technicians use **Customer risk actions**. The original case and the separate `WALK-` examples remain available.

| Case | Demonstration |
| --- | --- |
| FULL-01 | Seven-hour failed restore, corrected fictional claim, external signals, source discrepancy and open fixes. |
| FULL-02 | Separate three-hour passing retest, unresolved access review and other conditions. |
| FULL-03 | Passing retest, conditional decision, approved report and released fictional partner submission. Its provider-outage scenario spans all three businesses. |

The partner uses the existing `walkthrough-partner@example.test` account. Its locally generated password remains in `.local/walkthrough-credentials.txt`. No password is embedded in this guide or reset by the new command.

## A complete demo walkthrough

1. **Unified assessment:** inspect controls, both restore measurements, policy details, loss records, open work and dated observations. A passing retest does not close unrelated missing evidence.
2. **Policies & claims:** view a developing loss and an appended correction. Record a policy or claim, or download the JSON import example. Change its policy reference and incident date to match the selected policy before importing. Invalid batches roll back; exact repeats reuse the import receipt. Claims retain their policy currency. Unlinked historical policies do not pretend a current decision existed before an incident.
3. **Customer risk actions:** open the exposure or credential example. As the technician, record an investigation, request dismissal or request a recheck. Use the existing finding page to submit a fix and exact evidence version. As underwriter, accept the evidence and verify the response. A technician cannot approve their own dismissal or close a finding by marking an investigation complete.
4. **Priority scoring:** inspect the published demo rule. Score a finding from 0–4 for impact, exposure, evidence uncertainty and recovery gaps. Review the explanation or select an override with a reason. Old scores, weights and finding revisions remain in history. A score is a review priority, not an incident probability.
5. **Evidence comparisons:** inspect the fictional manual MFA export alongside the conflicting registration observation. Confirm scope, populations and dates before selecting a preference or requesting clarification. A changed document, review, scan or later source observation makes the comparison stale. Resolution does not approve a control.
6. **Software & vulnerabilities:** inspect the fictional gateway product at version 2.3.0 and a fictional advisory fixed at 2.4.0. Run matching twice to demonstrate deduplication. Record 2.4.0 as a replacement inventory and rerun. The historical match stays visible; a fixed version does not automatically close the original finding. Applicability, evidence and closure still need review.
7. **Submission preparation:** prepare evidence-linked notes from current policies, controls, claims, open work and reviewed extraction facts. Inspect retained citations and record approval or rejection. Changes to assessment or fact reviews block stale approval. This is deterministic local drafting, not a claim that a live language model generated a quote. Existing OCR/extraction and human review remain available under **AI review**.
8. **Provider outages:** choose the shared fictional cloud provider and eight hours. Inspect distinct businesses, policy periods, services, critical dependencies and the amount by which the assumed interruption exceeds the four-hour target. The scenario assumes no failover. Revoking a contributing portfolio permission blocks the saved scenario until access is valid; it cannot expose previously authorized records to an unauthorized viewer.
9. **Research demo:** inspect the separate generated dataset of 80 fictional businesses, frequency/severity/downtime estimates, held-out errors, stability comparisons and seeded bootstrap intervals. Download the exact dataset and fingerprint. No synthetic historical decisions or loss outcomes are inserted into the operational portfolio to manufacture validation success.
10. **Demo approvals:** approve requirements, scoring/review rules, the case journey and report/partner demonstration yourself as the demo underwriter. These approvals are tied to the assessment fingerprint and become historical when the assessed material changes. They do not impersonate a real insurer or bypass business sharing controls.
11. **Portfolio & partners:** inspect the FULL-03 approved submission. Sign in as the fictional partner, open its inbox and submit a clarification or indicative response. The partner cannot access the complete case or raw evidence.

## Feature-by-feature demonstration coverage

| Feature | Demo implementation |
| --- | --- |
| 1. Controls monitor | Evidence collection, date/freshness rules, assessments and fictional identity/backup observations. |
| 2. Recovery readiness | Separate backup status, restore measurement, functional/data-age checks and linked retests. |
| 3. Exposure and loss history | Dated observations, findings, policy references, claim development and append-only corrections/import receipts. |
| 4. External risks and guidance | Approved customer guidance tied to affected services, assigned findings, fixes and evidence review. |
| 5. Prioritization | Versioned weighted scoring, recorded factors and reviewer overrides; synthetic band-model comparison remains separate. |
| 6. Combined risk view | One assessment page with security, recovery, policy/loss context and traceable source dates. |
| 7. Assisted preparation | Existing OCR/cited extraction and reviewed follow-ups plus evidence-linked submission/quote-preparation notes and underwriter disposition. |
| 8. Evidence comparison | Internal evidence versus external observations, source snapshots, explicit reviewer resolutions and stale-source detection. |
| 9. Expert review | Existing reviewer permissions, findings, evidence acceptance, exceptions and decision snapshots now include the new assessment records. |
| 10. Public infrastructure | Authorized recurring fictional/passive observations, grouped history and linked follow-up/recheck workflow. No active network scanning. |
| 11. Credential alerts | Fictional domain-level exposure signals, safe customer guidance, investigation and reviewer-controlled dispositions. No leaked passwords or real account data. |
| 12. Vulnerabilities | Exact product-name and numeric-version-range matching against a sourced demo catalog; linked remediation and historical matches. |
| 13. Customer reports | Existing approved HTML/print and JSON reports, authenticated links, expiry, revocation and audit history. Raw claims and private analysis are excluded from customer projections. |
| 14. Capacity partnerships | Existing consent/release workflow, fictional partner inbox and responses under a clearly fictional demo relationship. |
| 15. Frequency and severity | Existing permissioned research plus a separate synthetic lab with downtime estimates, reproducible uncertainty intervals and held-out results. |
| 16. Shared-provider failures | Direct dependencies, distinct-business grouping, affected policy/service view and assumed outage-versus-target comparison. |

## Deliberate demo boundaries

- AWS infrastructure, private S3, hosted deployment, production secrets and real provider credentials remain deployment work. Existing adapters and Docker files are retained.
- No paid AI request, live external poll, public scan, email delivery, insurance quote or binding action is performed by setup. Local Celery scheduling can refresh authorized fictional observations; new observations can make an earlier comparison or draft stale, as intended.
- Product matching supports numeric release versions with up to four components, not arbitrary vendor-specific version rules or a universal vulnerability database. Stale inventory cannot create fresh matches.
- Credential evidence is domain-level, with aggregated historical exposure counts. Investigations do not require storing exposed account credentials.
- Imported policy history may be linked to an existing decision only when it existed by coverage start. Missing historical snapshots stay explicitly missing. The claims register does not imply complete loss reporting or a claim-free period.
- Bootstrap intervals illustrate uncertainty under the artificial dataset's assumptions. They are not predictive catastrophe bounds, validated prices, actuarial certification or proof of model quality. Live production portfolio analysis still needs permitted representative data.
- Outage scenarios describe direct dependencies and assumed interruption. They do not infer indirect suppliers, perform recovery tests or calculate financial losses.
- Owner and technician permissions, MFA outside the development-only switch, CSRF checks, approved sharing and revocation remain enforced. Self-approval applies to the marked local demo acceptance records only.

## Verification and deployment preparation

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
LOCAL_PASSWORD_ONLY=false AI_RUN_QUEUE_TEST=1 RECOVERY_RUN_TEST=1 python manage.py test workspaces evidence underwriting ai_review operations monitoring portfolio assurance --noinput
```

Verification on 14 November 2026: **277 tests passed**, with real queue, OCR and isolated recovery enabled. Django checks and migration consistency passed. All **45 populated pages** returned HTTP 200 after normal local underwriter sign-in. Browser visual QA was unavailable because the browser access host could not be reached.

The added tests cover policy/claim validation and correction, atomic idempotent imports, priority rules and stale edits, comparison invalidation, customer privacy and revocation, vulnerability boundaries and deduplication, draft review, decision snapshots, provider-permission revocation, synthetic holdout separation, seeded scenarios and self-approval. The GitHub CI test command includes `assurance`. No Git commit or push is part of this work.

Before deployment, run the existing staging preflight and Docker build/check workflow, configure AWS/storage and other chosen live integrations, and replace fictional demonstration arrangements with the actual operating requirements. The demo can be reviewed and approved locally before those steps.
