# Cyber underwriting platform

A local Django prototype for an insurer's cyber application and renewal workflow. Phases 1–5 include PostgreSQL, two workspaces, required MFA, case permissions, private evidence versions, control reviews, supervised restores, findings and fixes, saved decisions, approved reports, renewal history, and assisted evidence review with OCR and cited suggestions. All included cases and accounts are fictional.

The frontend uses Django templates and CSS. Next.js is not part of this implementation. The original Phase 1 restore comparison remains at `/demo/`.

## Run locally

Use Python 3.10 or newer and PostgreSQL 14 or newer. Phase 5 also uses Redis and Tesseract for queued processing and OCR (`brew install redis tesseract` on macOS if missing). From this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/dev_db.py start
python scripts/dev_queue.py start
python manage.py migrate
python manage.py seed_phase2
python manage.py prepare_phase3_demo
python manage.py prepare_phase5_demo
python manage.py runserver 127.0.0.1:8000
```

Open http://127.0.0.1:8000/.

For Phase 5 processing, keep a worker running in a second terminal from the project folder:

```sh
source .venv/bin/activate
celery -A pilot worker --pool=solo --concurrency=1 --loglevel=INFO
```

The default local demo parser uses no API key and sends no documents externally. Live OpenAI setup, case authorization, and the review walkthrough are in [PHASE5.md](PHASE5.md). Manual evidence review works without the queue or worker.

`dev_db.py` uses `initdb` and `pg_ctl` from your PATH. It creates a separate PostgreSQL cluster under `.local/postgres`, with a password-protected Unix socket under `.local/run` on port 55432 and no TCP listener. It does not modify a Homebrew database or service. It generates a private `.env` once and does not overwrite an existing configuration.

If you already have a database, or are using Windows, copy `.env.example` to `.env`, set a persistent random `DJANGO_SECRET_KEY` and your PostgreSQL connection settings, create the configured database, and omit `dev_db.py`. For tests, the database role needs permission to create a test database. On Windows activate with `.venv\Scripts\activate`.

Stop the web server and worker with Ctrl+C. Stop this project's queue and database with:

```sh
python scripts/dev_queue.py stop
python scripts/dev_db.py stop
```

Keep `.env` and its secret key across restarts: the key also protects stored MFA secrets. `.env`, `.local`, private samples, local mail, passwords, and the virtual environment are ignored by Git.

## Demo sign-in and MFA

The seed command writes random passwords into `.local/demo-credentials.txt`, readable only by your local user. It never resets an existing password. The four accounts are:

| Email | Role |
| --- | --- |
| `underwriter@example.test` | Underwriter at Harborline Specialty |
| `reviewer@example.test` | Technical reviewer; no cases until assigned |
| `owner@example.test` | Invited business owner |
| `technician@example.test` | Invited IT provider technician |

Each user must enroll their own authenticator app. Sign in, confirm your password when prompted, scan the QR code, enter the authenticator code, and save the recovery codes. Click **Overview** to continue; the first session may request an authenticator confirmation. Subsequent sign-ins require a password and authenticator code or a one-time recovery code. There is no demo MFA bypass.

Demo emails are already marked verified by the trusted seed command. New accounts can only be created through an invitation addressed to their email. Public signup is closed. MFA secrets are encrypted in PostgreSQL; rate-limit and TOTP replay data use a shared database cache.

If a previously opened sign-in page reports `Origin checking failed - null`, reload http://127.0.0.1:8000/accounts/login/ before trying again. Older pages used a `no-referrer` policy that could hide the origin of form submissions. The app now uses `same-origin`, with CSRF protection enabled. If using an embedded preview, open the URL directly in your browser. Restart `runserver` first if you started it with `--noreload`.

## Walk through Phase 2

1. Sign in as the underwriter. Open `DEMO-001`, or use **Open a case** to create an application or renewal with the `pilot-1` checklist.
2. Assign the case to the technical reviewer if desired. Only active reviewers from the same insurer are available. The creator retains access; an unassigned technical reviewer has none.
3. Select **Invite business owner**, enter `owner@example.test`, and copy the one-time displayed invitation link. Links expire after seven days. They are not automatically emailed.
4. Sign out, open the link, and sign in as the owner. Finish MFA setup, then **Accept case invitation**. Use **Overview** to return to a pending invitation after setup if necessary.
5. As the owner, explicitly authorize case access for a chosen duration. Then invite `technician@example.test` as the IT provider.
6. Sign in as the technician through that invitation and accept it. The portal shows the authorized case, initially with no assigned requests.
7. As the owner or underwriter, assign requests to the technician. The technician can now see only their assigned list. A legacy fictional restore brief on `DEMO-001` exercises Phase 2 downloads. Use **Open evidence & review** for Phase 3 uploads, versions, and assessments.
8. Try **Export list**, then revoke the technician's grant or revoke business authorization. New file and export requests must fail immediately. Internal insurer notes never appear in contributor pages or exports.

Use separate browser profiles or sign out between roles. Invitation links are shown once and stored only as hashes. Reissuing an invitation invalidates earlier unused links for that recipient and case.

## Walk through Phase 3

From a case, use **Open evidence & review** for each checklist item. Submit a document, have an insurer reviewer accept its exact version, then record the control assessment. Use **Service inventory & recovery targets** to view or add critical services and publish new target versions.

For the supervised restore request, record a 420-minute test against the 240-minute target, review it, then upload the retest document as version 2 and record a linked 180-minute retest. The original missed result remains in history and the missing access review stays open. Backup jobs have their own records and cannot imply a successful restore.

The complete role-by-role walkthrough and S3 setup are in [PHASE3.md](PHASE3.md). Fictional upload samples are in `.local/phase3-samples/`.

## Walk through Phase 4

Open an authorized case and select **Findings, decisions & reports**. Create owned findings with priorities, reasons, deadlines, and evidence/fix/retest requests. Providers respond with exact evidence versions and linked retests; reviewers verify responses before closing findings.

Use **Prepare decision review** to inspect a frozen snapshot and record the underwriter's outcome, rationale, and open conditions. Underwriters can approve expiring exceptions, approve business reports, and create revocable report links for named business owners. **Open next renewal** creates a fresh case linked to the original history, with new evidence requirements and authorization.

See [PHASE4.md](PHASE4.md) for the complete walkthrough, decision rules, report approval, sharing permissions, and renewal boundaries.

## Verify

With the project database running:

```sh
python manage.py check
python -m pip install -r requirements-dev.txt
python manage.py test workspaces evidence underwriting ai_review --noinput
python manage.py makemigrations --check --dry-run
```

Tests run against a separate PostgreSQL test database. They cover password and MFA flows, invitation binding and replay, cross-insurer isolation, expired/revoked access, contributor privacy, private evidence, restore verification, findings and fixes, decision snapshots, exceptions, approved report sharing, renewal history, CSRF, cited extraction, OCR, human review, retries, and the retained Phase 1 demonstration. With Redis running, prefix the test command with `AI_RUN_QUEUE_TEST=1` to include the real Celery worker test.

## Scope and next phase

Phase 3 adds private uploads, file checks and quarantine, document versions, service inventory, versioned recovery targets, evidence reviews, control assessments, backup jobs, and linked restore tests. Case exports include evidence provenance while preserving the Phase 2 authorization rules.

Development uploads persist privately in `.local/evidence`. The S3 integration is implemented and tested with an emulator; a live bucket has not been configured or verified. Development file checks validate structure, not malware safety. A real pilot needs private versioned S3 storage and a configured external malware scanner. [PHASE3.md](PHASE3.md) explains both.

Phase 4 is implemented locally. Reports are HTML with print styling and JSON downloads; named business-owner sharing requires sign-in, MFA, and current case access. Expiry is checked when records are accessed; email delivery and automatic reminder jobs are not implemented.

Phase 5 adds Celery jobs, OCR, validated citations, accept/correct/reject actions, discrepancy flags, and underwriter-approved follow-ups. Local demo parsing is runnable now. The OpenAI adapter has mocked integration tests; live model access and extraction quality remain unverified until configured and evaluated. Phase 6 is staging and the insurer pilot; monitoring remains later work.

See [Phase 5 assisted review](PHASE5.md), [Phase 4 workflow](PHASE4.md), [Phase 3 evidence setup](PHASE3.md), [Phase 2 access rules](PHASE2.md), and [the insurer workshop guide](PILOT.md).
