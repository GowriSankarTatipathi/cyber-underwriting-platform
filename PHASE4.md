# Phase 4 underwriting workflow

Phase 4 connects evidence review to owned follow-up work, fixes, retests, expiring exceptions, saved decisions, approved business reports, authenticated sharing, and linked renewals. It remains a local Django/PostgreSQL prototype with fictional data.

## Start

```sh
source .venv/bin/activate
python scripts/dev_db.py start
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Use the existing demo accounts and MFA. From an authorized case, open **Findings, decisions & reports**. The business and IT provider see **Follow-up work & shared reports**. For a new checkout, follow README.md first to install dependencies and seed the demo. No existing passwords, MFA settings, evidence, or decisions are reset.

## Reviewer responsibilities

| Action | Authorized role |
| --- | --- |
| Create findings, set priority with a reason, assign owners/deadlines | Authorized insurer reviewer, underwriter, or administrator |
| Respond with a fix, exact evidence versions, or a linked retest | Assigned owner, or an authorized insurer reviewer |
| Accept a response, request changes, close or reopen a finding | Authorized insurer reviewer, underwriter, or administrator |
| Approve/revoke an exception; record/supersede a decision | Authorized underwriter or insurer administrator |
| Draft a business report | Authorized insurer reviewer, underwriter, or administrator |
| Approve, withdraw, or share a report | Authorized underwriter or insurer administrator |
| Read a shared report | Named business owner with MFA and current case access |
| Open a linked renewal | Authorized underwriter or insurer administrator |

All existing tenant, case assignment, business consent, grant expiry, and MFA checks remain in force. A provider must already have an active case grant and assignment to the related evidence request before being assigned a finding. Assigning a finding does not silently grant evidence access. Providers see only their assigned findings on requests they can still access; business owners can see their case's follow-up work. Finding descriptions and review comments are intended for these participants. Internal underwriting rationale and draft reports remain insurer-only.

## Walk through a retest and a conditional decision

1. Complete the Phase 2 invitation/consent/provider assignment and the initial Phase 3 restore review. The original **420-minute** restore must show **Target missed** against a **240-minute** target.
2. Open **Findings, decisions & reports → Create finding**. Select the supervised restore evidence request, **Restore retest**, and the original restore test. Set the provider as owner, a deadline, a priority, and a concrete reason. Describe the required scope and evidence.
3. Sign in as the provider. Open the finding in **Follow-up work**. Use **Evidence & uploads** to upload the new report and **Record linked retest** to record **180 minutes** with the actual recovered data age and functional checks. Then **Respond / submit fix**, cite the exact new evidence version, and select that linked retest.
4. As the insurer reviewer, accept the evidence version and verify the retest through the Phase 3 evidence screens. Open the finding, inspect the response, and **Review or reopen → Accept response and close**. Closure requires current, accepted, scope-confirmed, fresh evidence. A retest finding additionally requires the latest linked test to have a passing review against current targets. Record the restore control assessment separately; closing a finding never fabricates a control result.
5. The missing access review stays missing. Create an **Evidence request** finding for it with an authorized owner and due date. Create corresponding findings for any other unresolved controls, such as backup or MFA coverage. Ordinary **Fix required** findings use the same response and reviewer-verification path.
6. If justified, the underwriter can approve an exception on an open finding, documenting the rationale, compensating controls, and UTC expiry. Expired exceptions immediately stop counting as active when a page or decision is evaluated. They remain visible in history and never close findings or change a failed restore to Passed.
7. Select **Prepare decision review**. Inspect the frozen checklist, evidence versions/checksums, acceptance and scan records, assessments, recovery measurements and targets, findings, owners, deadlines, and exceptions. Select **Approved with conditions**, explicitly choose the open findings that remain conditions, provide the internal rationale, confirm the review, and save.
8. Every unresolved control needs a condition or active exception, and every open finding needs explicit coverage. Conditional decisions require at least one selected condition with a currently authorized owner and a deadline today or later. Approval without conditions requires verified controls and no open findings. Deferral or decline can record why outstanding evidence prevents approval.

The latest decision is shown to authorized insurer staff on the case and in their queue. It is a recorded decision, not a claim that later evidence or work has remained unchanged.

## History and concurrent changes

Finding creation, assignment/priority edits, responses, review actions, closure, and reopening leave dated history. Closed findings must be reopened before editing. Response submissions do not close findings. Revision checks prevent a stale review or edit from overwriting newer work.

Decision preparation creates an immutable snapshot. Submission rechecks it against current evidence, policy and checklist data, findings, owner access, and exception status. If something changed or expired, prepare and review a fresh snapshot. A second pending review cannot silently supersede a newly recorded decision. Case row locks serialize application evidence/workflow writes with decision capture and submission; upload storage operations can therefore briefly delay other writes on the same case.

Saved decisions have sequential versions, an actor, timestamp, rationale, conditions, and the exact snapshot digest. A later decision appends a record. Later uploads, revised targets, rejected evidence, or closed conditions do not rewrite prior decisions. **Export decision & exact snapshot** is insurer-only JSON and includes the referenced evidence and rule versions. Snapshot hashes detect content changes; they are not digital signatures or a replacement for database administration and retention controls.

## Approved reports and secure sharing

1. Open a saved decision and choose **Draft report**. Write a summary intended for the business.
2. Inspect the rendered draft and report data. The report includes the decision outcome/date, conditions, exceptions, control and restore results, evidence IDs/checksums, checklist version, rule versions, and snapshot digest. An explicit field allowlist excludes internal case notes, private decision rationale, review comments, private object locations, and file contents.
3. The underwriter chooses **Approve this report** and confirms a rationale. Approval requires the latest decision and an unchanged decision snapshot. A changed case needs a fresh decision and report. Drafts cannot be shared or downloaded as approved reports.
4. Choose **Share with business owner**, select the currently authorized recipient, and set an expiry no later than their case access. Copy the generated link. No email or other message is sent automatically.
5. The named recipient signs in with MFA. Every view and JSON download checks the report's approval/withdrawal, share expiry/revocation, exact recipient, and current owner grant. The link alone gives no access. Revoking the share or withdrawing the report blocks future views and downloads. Already downloaded copies cannot be recalled.

Reports are readable HTML with print styling and an approved JSON download. Browser Print can save a PDF; server-generated PDF bundles and electronic signatures are not implemented. Shared reports clearly show that they describe the historical decision date. Newly expired conditions or exceptions do not silently change that report. Providers receive assigned follow-up work, not full case reports. Capacity-partner or other external-recipient sharing is a later commercial workflow.

## Renewal history

After recording a decision, choose **Open next renewal**, select a checklist for the same insurer/product, and create a fresh business-owner invitation. The new case links to its predecessor and reuses the applicant and service inventory. It initializes targets from the selected checklist and creates a fresh request list.

Evidence, reviews, findings, decisions, exceptions, sharing grants, and business consent are not copied into the new case. Previously accepted evidence must be collected/reviewed for the renewal. Only one direct successor is created per case; subsequent years extend the chain.

**Renewal history** opens prior decisions with their original snapshots. Access is checked independently for every prior case. Assignment to a renewal does not grant access to previous years, and revoked prior-case consent blocks insurer evidence history until authorization is renewed. Authorized staff can view history from the new case while awaiting its new authorization.

## Verify and boundaries

```sh
python -m pip install -r requirements-dev.txt
python manage.py test workspaces evidence underwriting --noinput
python manage.py check
python manage.py makemigrations --check --dry-run
```

Tests use a separate PostgreSQL database and temporary evidence files. Coverage includes the full restore/retest/conditional-decision/report/renewal journey; browser-form routes; stale submissions; exact snapshots; owner, role, and tenant permissions; exception/share expiry and revocation; private-report isolation; and earlier phases.

Expiry is evaluated on access and decision submission; automated reminder jobs and email delivery are not added. Priorities are selected by reviewers with required reasons, not predicted by AI. Decisions are prototype underwriting records, not policy issuance or binding coverage. No staging or production deployment is performed. The Phase 3 live S3 and external malware-scanner setup remains outstanding as described in PHASE3.md. Phase 5 adds authorized OCR/AI extraction and human-reviewed suggestions while preserving manual review.
