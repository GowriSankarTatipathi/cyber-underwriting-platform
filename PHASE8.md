# Phase 8 — portfolio research and capacity partners

Phase 8 adds runnable internal portfolio workflows and authenticated partner submissions. Open an authorized case and select **Portfolio research & capacity partners**. Live AI, live monitoring credentials and hosted staging remain deferred. No real loss model has been validated, no insurer pilot has been completed, and no commercial partner has been contacted.

## Portfolio permission and scope

1. Sign in as the business owner and open the case's portfolio page.
2. Select **Authorize portfolio research**, review the internal research purpose and choose an expiry within the existing owner grant and case authorization.
3. Sign in as the assigned insurer reviewer or underwriter to record outcomes and provider dependencies.

Portfolio permission is separate from case access, document-processing permission and capacity sharing. The owner can revoke it. Each record is pinned to the permission used when it was created. Expired/revoked permissions, revoked owner grants and revoked case consent exclude those records from portfolio views and future evaluations. Previously saved research exports also require current access to every source case and its pinned permission. A later permission does not silently reactivate previously revoked records. Retained database records remain audit history; already downloaded copies cannot be remotely recalled.

The dashboard covers only the viewer's accessible, authorized cases for one insurer. It is not necessarily the insurer's entire portfolio. An insurer administrator can see all permitted cases in their organization. Unassigned reviewers, IT providers and other insurers cannot see portfolio data. The business owner sees permission and submission controls, not other applicants' outcomes.

## Link incident and loss outcomes to earlier evidence

Use **Record outcome or correction**. Select the prior decision, completed observation period, incident count, total incurred loss, currency, source and development status. Explain scope and any reserves or corrections. The decision links to its frozen checklist, evidence versions, findings and rule versions.

- Observation periods use an inclusive start and exclusive end. The decision date must precede the start. Future or zero-length periods are rejected. A newly created pilot decision cannot yet have a completed subsequent historical period.
- Record zero-incident periods too, with zero loss; omitting them would bias frequency estimates. This platform cannot establish completeness of the insurer's source register—reviewers must reconcile it.
- Overlapping periods for the same applicant and insurer are rejected, including across renewal cases. Exposure is elapsed days divided by 365.25. Currencies are neither converted nor combined.
- A correction appends a new record linked to the previous one for the exact same period. The current record supplies totals, while the original remains in history.
- Developing loss estimates may be recorded but are excluded from model evaluation. Marking a period sufficiently developed is a documented reviewer judgment, not an automated claim that losses are final.

Do not backdate development decisions to manufacture a live evaluation dataset. Synthetic historical records are used only in isolated automated tests. Importing a real insurer's historical decisions and outcomes needs a separately reviewed migration with source reconciliation.

## Shared-provider dependencies

Use **Record dependency** to link a service to a provider, a stable canonical provider key, a supporting source, observation date and criticality. Reuse the same key for the same legal/provider entity across cases. Provider matching is explicit and insurer-specific; names alone are not automatically merged.

The portfolio view groups active dependencies and shows distinct case counts, distinct critical-case counts, case links and records older than 90 days. Multiple services in one case count once. Separate renewal cases remain separate cases, not distinct insured businesses. The 90-day indicator is a review cue, not an insurer-defined evidence acceptance rule. Retire a superseded dependency with a reason before recording its replacement. This is a direct shared-provider inventory; it does not infer indirect supplier chains or simulate catastrophe loss.

## Frequency, severity and priority research

An underwriter can select **Run research evaluation**, a past holdout cutoff and one currency. The tool freezes a dataset with outcome IDs, sources, recording times, permission IDs, decision IDs and evidence-snapshot hashes. It stores a SHA-256 fingerprint, model version and results for reproducibility.

The implemented candidate is a transparent **historical risk-band baseline**, not a production pricing model:

- A higher band means the original decision snapshot contained an open high/critical finding. Other snapshots are lower band. Current findings and subsequent outcomes never change that historical feature.
- Training periods end on or before the cutoff. Holdout periods start on or after it. Periods spanning the cutoff are excluded. Applicants appearing in holdout are removed from training, including their earlier renewal periods.
- Training requires at least 30 distinct applicants, holdout at least 10, at least 10/5 training/holdout incidents, and at least 10 training observations in each band. These are pilot guardrails, not statistical proof of adequate sample size. Insufficient data produces reasons and no fitted coefficients.
- Frequency is incidents per exposure-year. Severity is total incurred loss per incident. The overall baseline is compared with separate band baselines. A band with no events uses the overall frequency and severity.
- Holdout metrics are mean Poisson deviance for incident counts, with expected count equal to frequency times exposure, and incident-weighted absolute error of each period's average loss. Observed holdout band rates are also shown. Holdout outcomes are never used to fit coefficients.

The frequency/severity separation and exposure treatment follow the concepts in the official [scikit-learn insurance example](https://scikit-learn.org/1.7/auto_examples/linear_model/plot_tweedie_regression_insurance_claims.html); the count metric follows [mean Poisson deviance](https://sklearn.org/stable/modules/generated/sklearn.metrics.mean_poisson_deviance.html). This implementation uses small explicit formulas rather than adding a machine-learning dependency.

This is a retrospective research comparison: loss development and corrections may have been recorded after the cutoff. It is not a simulation of exactly what loss labels were available at a historical deployment date. It does not establish causality, calibrate a premium, estimate tail risk, quantify uncertainty, validate fairness or automatically change finding priorities. Severity metrics use aggregate period losses and cannot replace individual-claim analysis. Production use requires representative data, independent actuarial/model review, development adjustments, uncertainty and stability checks, subgroup analysis and prospective validation. The current fictional pilot has insufficient historical data; that is the expected result.

## Capacity-partner workflow

Provision a named account only after verifying its email out of band. This operator command prompts for a password, applies password validation and grants no insurer membership, administrator privilege or case access:

```sh
python manage.py provision_capacity_partner --email partner@your-domain.example --email-verified
```

Use a fictional address for local testing. Existing accounts are never reset. Provisioning sends no email. Provide account access through your established onboarding process; the partner must enroll in MFA in staging. The existing `LOCAL_PASSWORD_ONLY=true` switch remains limited to debug-mode local testing.

1. As an underwriter, **Record existing relationship** with the partner name, exact account email, contract reference and expiry. Confirm that the commercial relationship actually permits submissions. This records your attestation; the app does not verify contracts externally.
2. Complete the decision workflow and approve a report based on the latest current decision.
3. Select **Prepare submission**, the relationship, report and expiry. The immutable projection includes the applicant/insurer, product, report summary, outcome, checklist version, control titles/statuses, condition descriptions/dates/statuses, and approved exception summaries with expiry and compensating controls. It excludes raw files, storage references, internal decision rationale, internal notes, portfolio losses and condition-owner emails. Free-text report summaries and conditions still require human disclosure review.
4. The business owner opens the submission, previews the entire payload, named recipient and expiry, then approves that exact content fingerprint. The expiry must fit their grant and case authorization.
5. The underwriter previews it again and records a release rationale. A changed decision or changed review snapshot blocks release and requires a new submission.
6. The named partner signs in and opens **Capacity partner inbox**. They can read/download the released summary and append a receipt, clarification request, indicative interest or decline. They receive no case-page, evidence-file or insurer-workspace access. Responses do not change the underwriting decision or bind insurance.
7. Owners or underwriters may revoke individual submissions. Underwriters may revoke the commercial relationship, blocking all its submissions. Each read, download and response rechecks the recipient, case consent, owner grant, content fingerprint, report approval, relationship and submission expiry/revocation.

The partner inbox and release links are authenticated application routes, not public bearer links. No email or external partner API call is sent by release. A previously released report remains a dated historical snapshot if a newer decision is recorded; withdraw the report or revoke its submission to stop access. Revocation cannot recall files already downloaded.

## Verification

The final 232-test regression suite passed, including 30 Phase 8 tests and the enabled real queue/OCR/recovery checks. The Phase 8 migration was applied to the local development database. Django system checks, migration consistency and the running server's readiness/sign-in routes passed. Screens and forms were exercised through Django HTTP tests; browser visual verification was not available.

Apply the migration and run checks from the activated virtual environment:

```sh
python manage.py migrate
python manage.py check
python manage.py makemigrations --check --dry-run
LOCAL_PASSWORD_ONLY=false python manage.py test portfolio --noinput
```

The Phase 8 suite covers owner permission, outcome chronology/corrections/overlap, currency and maturity filters, case/tenant isolation, dependency grouping, dataset provenance, data-sufficiency gates, held-out fitting, revoked saved exports, account provisioning, submission approvals, payload isolation, partner responses, expiry/revocation, MFA and CSRF. Tests use a separate PostgreSQL database and synthetic records.

For the full suite, with Redis and matching PostgreSQL client tools available:

```sh
LOCAL_PASSWORD_ONLY=false AI_RUN_QUEUE_TEST=1 RECOVERY_RUN_TEST=1 python manage.py test workspaces evidence underwriting ai_review operations monitoring portfolio --noinput
```

Hosted Docker execution, live integrations, representative insurer acceptance and real-data model validation remain external validation work. See [Phase 6](PHASE6.md) and [Phase 7](PHASE7.md) for the deferred setup.
