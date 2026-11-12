# Phase 5 — assisted evidence review

Phase 5 adds background document preparation, OCR, structured extraction with source passages, human review, and draft follow-ups connected to the Phase 4 workspace. The frontend remains Django templates and CSS.

The default **local demo parser is not live AI**. It recognizes the sample log format without an API key or external document transmission. The OpenAI adapter is implemented and tested with mocked responses; a live model call still requires your configuration and case authorization.

## Start locally

Use the existing virtual environment and PostgreSQL configuration. Redis and Tesseract must be on your PATH. On macOS, if missing, install them with `brew install redis tesseract`. Tesseract is needed for image/scanned evidence; native text can be processed without it.

From the project folder, run:

```sh
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/dev_db.py start
python scripts/dev_queue.py start
python manage.py migrate
python manage.py prepare_phase3_demo
python manage.py prepare_phase5_demo
python manage.py check_ai_review --queue
python manage.py runserver 127.0.0.1:8000
```

In a second terminal, from the same folder:

```sh
source .venv/bin/activate
celery -A pilot worker --pool=solo --concurrency=1 --loglevel=INFO
```

Keep both terminals running. Open http://127.0.0.1:8000/ and use your existing accounts and MFA. `seed_phase2` can prepare accounts on a fresh installation; it does not reset existing passwords. See [README.md](README.md).

`dev_queue.py` starts a project-only Redis instance using `.local/run/redis.sock`, with no TCP listener. Its files are private under `.local/redis`. It does not change Homebrew's Redis service. For a separately managed broker, set `CELERY_BROKER_URL` in `.env` and omit this helper. Use a private broker; evidence jobs must not share an untrusted queue.

Stop the worker and web server with Ctrl+C, then:

```sh
python scripts/dev_queue.py stop
python scripts/dev_db.py stop
```

## Walk through the seven-hour restore

Use an authorized case with a business owner and a technician assigned to its supervised restore request. Keep the service recovery target at **240 minutes**, with a **60-minute** recovered-data-age target.

1. As the technician or business owner, open the restore request and upload `.local/phase5-samples/restore-seven-hours-scanned.pdf` as a new document. Choose the correct service, a recent observation date, and the test scope. The PNG in the same folder exercises image OCR. The sample service is “Order processing”; a different selected service name will intentionally create a discrepancy flag.
2. Record the original supervised restore against this exact evidence version: **420 minutes**, **35 minutes** recovered data age, and successful functional checks. Include the actual test date, supervisor and service scope. This manual record provides the baseline to which a retest request can link.
3. As the insurer reviewer or underwriter, accept the evidence version and verify the restore scope using the Phase 3 screens. The restore should show **Target missed**. These are separate actions from extraction.
4. On the document's current version, choose **Extract facts**, confirm, and wait for the worker. The page polls status for one minute; you can also refresh or continue manual review. The local demo label stays visible.
5. Inspect the suggested `420` minutes and its quoted source passage. The candidate panel uses Django's insurer rule to show the missed four-hour target. Open the original file and compare the source passage, especially when OCR was used.
6. Choose **Accept, correct, or reject** for each fact. Each action requires a rationale and confirmation. Corrections require a supported value and an exact quote from a selected passage. Until reviewed, suggestions do not contribute to the human-reviewed panel.
7. Select **Prepare reviewed follow-up**. A missed restore linked to the recorded baseline produces a **Supervised retest** draft. No assigned work is created yet. Without a baseline restore record, the draft requests evidence clarification instead.
8. As an underwriter or insurer administrator, check the final wording, priority and reason, select an authorized owner and deadline, and confirm **Send to assigned workspace**. A technical reviewer can prepare or reject drafts but cannot send them. Sending creates a Phase 4 finding; it does not send email.
9. As the assigned technician, open the finding in **Findings, decisions & reports**. Respond with a separate linked three-hour retest and its evidence. Continue Phase 4 review and decision recording normally. The original seven-hour test stays in history, and the missing access review remains open.

The existing Phase 3 text samples also work. The demo parser recognizes labelled lines such as `Service:`, `Scope:`, `Test date: YYYY-MM-DD`, `Total recovery time: 7 hours (420 minutes).`, `Recovered data age: 35 minutes.`, and `Functional checks: passed`. It is deliberately limited; arbitrary prose needs live AI or manual review.

## Review discrepancies and failures

- Upload `conflicting-restore.txt` to see two conflicting elapsed times. If both are accepted, the system blocks follow-up drafting until the conflicting suggestion is corrected or rejected. Separate tests should have separate evidence and linked restore records.
- Discrepancy flags compare the service name with the service inventory, measurements with restore records referencing the same file, and reviewed facts from other current documents at the same observation time. Different observation times are not automatically conflicts.
- A new document version or recovery-policy version blocks new reviews and follow-ups from an older extraction. Historical results remain available to authorized reviewers. Start a new extraction for the current version/targets.
- A change to the reviewed facts, evidence review, scan or baseline makes an existing draft stale. Prepare another draft. Sent findings retain the reviewed context and exact source version; later edits remain in Phase 4 finding history.
- Queue/storage/provider failures display a safe error and leave all manual evidence and restore actions usable. Retry from the job screen after resolving the cause. Transient processing failures get at most three attempts per retry cycle; explicit manual retries preserve prior attempt history.
- If a worker stops while processing, its five-minute lease eventually allows a manual retry. Live workers cannot be restarted through the retry action. Cancelled or unauthorized jobs discard returned results. A queued job with no worker stays queued until a worker starts or a reviewer cancels it.

## Enable live OpenAI extraction

Configure these values privately in the existing `.env`; do not replace your database configuration or secret key:

```dotenv
AI_REVIEW_PROVIDER=openai
AI_ALLOW_EXTERNAL=true
OPENAI_API_KEY=your-own-key
OPENAI_MODEL=your-enabled-model-with-structured-outputs
```

Choose an available model for your account that supports the Responses API and structured outputs. There is no hard-coded model default. Restart **both Django and Celery**, then run `python manage.py check_ai_review --queue`. This command checks configuration and queue connectivity; it does not test API credentials, model access, pricing or extraction quality.

The business owner must then open **Assisted evidence review & processing authorization** on the case and authorize external processing with an expiry within their current case authorization. An insurer reviewer can then select specific documents for extraction. Only prepared text and passage identifiers/locations go to OpenAI; the application does not provide tools, storage URLs, database credentials, or entire case exports to the model. The request uses `store=False`. This setting is not a promise of zero provider retention; apply your insurer's provider/data-handling arrangements before using real evidence.

The adapter uses the SDK's typed Responses parser and validates the response again locally. It treats source text as untrusted input and gives the model no authority to approve evidence, apply insurer rules, or send work. See the official [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).

Revoking external processing stops future authorized calls and prevents a running job from publishing its results when its next access check fails. It cannot recall text already sent. Completed results remain local case records under normal reviewer permissions. Revoking case access also prevents reading those results.

To return to local demo mode, set `AI_REVIEW_PROVIDER=demo` and `AI_ALLOW_EXTERNAL=false`, then restart both processes. Demo mode requires `DJANGO_DEBUG=true`; use `AI_REVIEW_PROVIDER=off` to disable new processing.

## Data, access and processing limits

Every job pins the source document version and hash, requester, provider/model, processing authorization where applicable, and recovery-policy version. Results preserve prepared passages, locators, passage hashes, validated citations, prompt/schema/preparation versions, provider response identity, warnings, and an attempt history. Fact reviews and draft dispositions are append-only through the application services.

Citation validation checks exact matches against the prepared text, types, measurement units, supported values and simple contradictions. A citation match does not establish that the document or OCR is correct. The reviewer must inspect the original. Extracted facts never create an accepted evidence review, verified control, restore test or underwriting decision automatically.

The broker receives job IDs, while private source text and results stay in PostgreSQL and private evidence storage. Parsing runs in a bounded subprocess with no application credentials passed in its environment; temporary files are removed afterwards. This is process isolation, not a hardened container sandbox. A staging deployment should run parser/worker processes with restricted OS/network permissions and the Phase 3 malware-scanning configuration.

Current limits are 10 MB per evidence file, 25 PDF pages or DOCX embedded images, 100,000 prepared characters, 50 extracted facts, a 90-second preparation deadline, and a 60-second provider timeout. Excess input fails visibly rather than being silently truncated. OCR uses Tesseract English and supports PNG, JPEG, scanned PDF pages and readable DOCX images. Native PDF/DOCX text, UTF-8 TXT, CSV and JSON are supported. Encrypted, malformed, oversized or unsupported content may require a simpler document or manual review. Cross-document reconciliation is intentionally limited to the comparisons described above.

All AI pages require sign-in, MFA and current case permission. Processing and review are restricted to insurer reviewers; owners manage external authorization; technicians see only sent findings assigned within their existing access. Authorization and file checks are repeated before storage reads, before an external call, and before publication. Existing S3, scan/quarantine and file-version checks still apply.

## Verification

With PostgreSQL running:

```sh
python -m pip install -r requirements-dev.txt
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test workspaces evidence underwriting ai_review --noinput
```

To include the actual Redis/Celery integration test, start the project queue and run:

```sh
python scripts/dev_queue.py start
AI_RUN_QUEUE_TEST=1 python manage.py test workspaces evidence underwriting ai_review --noinput
```

The worker test uses a unique queue and Django's separate test database. It removes only its own queue and never processes the development queue. The standard suite skips this test unless enabled; OCR integration tests require Tesseract.

Coverage includes cited extraction and OCR, the seven-hour/four-hour comparison, human corrections and rejections, discrepancy handling, approved follow-up assignment, exact provenance, cross-role access, MFA/CSRF, revocation before and during processing, cancellation, retries, stale evidence/targets, queue failures and manual continuation. OpenAI responses are mocked: live extraction quality and citation accuracy on representative insurer documents still need a configured pilot evaluation. Browser automation was unavailable during implementation; route/form rendering and submissions were checked through Django tests.

Implementation verification on 2026-11-12: **161 tests passed**, including the real Redis/Celery worker and Tesseract image/scanned-PDF tests. Django checks, migration consistency, dependency checks and whitespace checks also passed. The Phase 5 migration is applied to the local development database.

Phase 6 remains staging deployment and insurer pilot evaluation, including live S3/scanner/provider verification, platform restore testing, operational monitoring and measured review/extraction quality.
