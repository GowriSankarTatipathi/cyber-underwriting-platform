# Fictional full-case walkthrough

This demo uses three separate cases at **Walkthrough Assurance (fictional)** and dedicated accounts. Your original case, accounts, notes, passwords and grants are unchanged. The prepared demo grants and approvals are fictional fixtures, not assertions that a real business authorized processing. No email, live AI request, cloud upload or monitoring call is sent.

## Start and sign in

The website and project services are stopped after implementation. To try the prepared demo:

```sh
source .venv/bin/activate
python scripts/dev_db.py start
python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/`. Passwords are saved privately in `.local/walkthrough-credentials.txt` and are ignored by Git. They are not shown in the browser or written to logs.

| Account | What it can do |
| --- | --- |
| `walkthrough-underwriter@example.test` | Review all three fictional cases, inspect findings, release approved submissions |
| `walkthrough-owner@example.test` | Review case sharing, approve or revoke partner submissions and portfolio permission |
| `walkthrough-technician@example.test` | See assigned requests, upload evidence, record retests and respond to follow-up work |
| `walkthrough-partner@example.test` | Read and respond to released summaries; no case or raw-evidence access |

Use separate browser profiles or sign out between accounts. Existing local password-only testing follows your current `.env` setting; production always requires MFA. No authenticator secrets are pre-seeded.

On a fresh checkout, run migrations and `python manage.py prepare_walkthrough_demo` first. Seeding requires `DJANGO_DEBUG=true`, local evidence storage and basic development scanning. Repeating it preserves existing demo passwords, cases and your edits. It does not renew expired demo grants or reset revoked approvals; create fresh fixtures in a separate development database when starting a new workshop. All dates are relative to the initial seed time: grants last 90 days, portfolio/relationship permissions 30 days and submissions seven days.

## 1. Missed recovery target — WALK-01

As the underwriter, open **Demo walkthrough → WALK-01**. The restore record is 420 minutes against a 240-minute target, with a reviewed **Target missed** result. The access review and other required evidence remain missing.

Open **Follow-up & decisions** to inspect the owned restore-retest finding and other requests. The technician can open its assigned work, upload a later test document and link a retest to the original record. The evidence reviewer must verify it before closing the finding. The initial failed result remains in history. The original complete retest/decision/renewal journey also has automated coverage in the underwriting test suite.

## 2. Passing retest with open conditions — WALK-02

Open **WALK-02** as the underwriter. Its second restore is 180 minutes and is linked to the original 420-minute test. The restore control is verified. **Access review, privileged MFA and backup coverage remain missing.** The conditional decision records those three outstanding evidence requirements with owners and deadlines.

Select **Portfolio & partners**. You can see the shared fictional cloud provider and a prepared capacity submission. The submission shows readable control results and conditions; its exact JSON and fingerprint are available under expandable details. The decision and report are already approved fictional fixtures, but sharing approval is intentionally pending:

1. Sign in as the owner, open WALK-02's submission and review the named recipient, expiry and exact content. Select **Review and approve sharing** and confirm.
2. Sign in as the underwriter and select **Review and release**. Record a release rationale. No message is sent.
3. Sign in as the partner. The home page opens **Capacity partner inbox**. Read the released report, download its JSON and record a clarification request or indicative response.
4. Return as the underwriter to inspect the response. It does not approve evidence, close findings or bind coverage.
5. As the owner, revoke the submission. A fresh partner page or download request must be denied, and the submission disappears from the active inbox. A previously downloaded copy cannot be recalled.

If evidence, rules or the decision change before release, create and approve a new current report and submission. The old snapshot cannot be silently re-released as if it were current.

## 3. Ready-to-review partner submission — WALK-03

WALK-03 includes the same recovery comparison and open-condition pattern, plus a fictional owner approval and underwriter release. It is immediately available to the demo partner after sign-in. Use it to explore the inbox without completing the approval steps first. Opening its case URL as the partner must return an unavailable page.

All three cases share one provider key. The underwriter's portfolio shows **three distinct cases** for that provider. Multiple services would not multiply the case count. No historical loss outcomes are manufactured, so research evaluations should show that more historical data is needed.

## Stop

Stop the web server with Ctrl+C, then stop the project's database:

```sh
python scripts/dev_db.py stop
```

This walkthrough does not need Redis or a worker. If you separately started the queue, worker or scheduler for AI/monitoring, stop the worker and scheduler with Ctrl+C and run `python scripts/dev_queue.py stop`. These commands leave saved data intact.

## Automated verification

```sh
LOCAL_PASSWORD_ONLY=false python manage.py test workspaces.test_walkthrough workspaces.test_hardening portfolio.test_hardening operations.test_deployment --noinput
```

Tests run in a separate database. They verify seed idempotency, preservation of edits/passwords, restore results, missing evidence after retest, owner approval, release, partner response/download, revocation, role-sensitive navigation, expired states, invitation concurrency, and staging prerequisite checks. The full suite additionally exercises private uploads, OCR, queue failures, monitoring, decision history, renewals and encrypted recovery.
