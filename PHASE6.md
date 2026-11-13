# Phase 6 — staging preparation and insurer pilot measurements

The application now includes a portable Docker configuration, deployment checks, review timers, extraction/citation evaluations, first-response quality measurements, and encrypted backup/restore tooling. A real insurer pilot and hosted staging deployment still require an operator, hosting environment, private S3 bucket, provider accounts and representative cases. No real insurer pilot has been conducted by this implementation.

## Portable staging setup

Docker is not installed on the implementation machine. Compose and CI YAML were parsed, but container images and the full Docker deployment have not been built or run here.

Install Docker Engine/Desktop with Compose, then from the repository root:

```sh
cp deploy/staging.env.example .env.staging
chmod 600 .env.staging
```

Edit `.env.staging` privately. Configure a persistent random Django secret, different application/database-administrator passwords, SMTP settings and a private versioned S3 bucket with encryption and all public-access blocks enabled. The existing [Phase 3 storage guide](PHASE3.md) describes required storage permissions. Use workload roles where available. Do not put credentials into source files or Git.

The Compose stack contains PostgreSQL 17, Redis, Gunicorn, a Celery worker, one beat scheduler, ClamAV signature updates and an HTTPS Nginx proxy. The application database role cannot create databases or administer roles. Database and Redis ports are not published. The proxy binds only to `127.0.0.1:8443` by default; changing it to a public interface and choosing a domain is a separate deployment step.

Provide `server.crt` and `server.key` in `.local/staging/tls/`. For a local smoke test only, create a self-signed certificate:

```sh
mkdir -p .local/staging/tls
chmod 700 .local/staging .local/staging/tls
openssl req -x509 -newkey rsa:3072 -sha256 -days 30 -nodes \
  -keyout .local/staging/tls/server.key \
  -out .local/staging/tls/server.crt \
  -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
chmod 600 .local/staging/tls/server.key
docker compose --env-file .env.staging -f deploy/compose.yaml up --build -d
docker compose --env-file .env.staging -f deploy/compose.yaml logs init
docker compose --env-file .env.staging -f deploy/compose.yaml exec web python manage.py check_staging --connections
```

Use a trusted certificate for an insurer pilot. Open `https://localhost:8443/` after the local certificate is trusted. Initialization runs deployment checks, migrations and static collection. ClamAV must finish downloading its signature database before uploads can pass scanning; inspect `antivirus-update` logs. Scanner errors quarantine evidence rather than accepting it. A missing S3/scanner/provider configuration is a readiness issue, not a successful deployment.

`/health/live/` confirms the web process is responding; `/health/ready/` checks database connectivity. Neither claims worker, S3 or AI health. `check_staging --connections` additionally probes private S3 configuration, Redis and a responding worker. These checks do not establish that every external integration works.

Create the first administrator interactively, then verify their email out of band and grant insurer membership:

```sh
docker compose --env-file .env.staging -f deploy/compose.yaml exec web python manage.py createsuperuser
docker compose --env-file .env.staging -f deploy/compose.yaml exec web python manage.py bootstrap_insurer --email admin@your-domain.example --insurer 'Pilot insurer' --email-verified
```

MFA enrollment remains mandatory. The insurer must publish its agreed checklist through the existing `ChecklistVersion` model before creating cases; use a reviewed data migration or an operator-managed Django shell import of the agreed requirements and targets. Do not run fictional seed commands in staging. Case creation, invitation acceptance, business authorization and provider assignment then follow [Phase 2](PHASE2.md).

Set `STAGING_AI_PROVIDER=openai` in `.env.staging` only when configuring live AI, together with the Phase 5 settings and business authorization. Restart web/worker after configuration changes. Live monitoring is independently controlled by `MONITORING_LIVE_ENABLED` and Phase 7 authorizations.

Stop containers with `docker compose --env-file .env.staging -f deploy/compose.yaml down`. Omit `--volumes` to retain data. Back up the persistent Django secret separately: existing MFA secrets depend on it.

The configuration follows Django's [deployment checklist](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/). CI is prepared in `.github/workflows/checks.yml`; it has not been run on GitHub during this task.

## Measure the insurer pilot

On a case, open **Pilot review time & quality metrics** as an authorized insurer reviewer.

1. Start the review timer when beginning work and stop it when pausing. Only completed sessions contribute to elapsed review minutes; running sessions are excluded. This measures explicit sessions, not inferred user activity.
2. Independently label the source document's expected facts. Evaluate the original extraction, counting correctly extracted facts and citations that both match the source and support the claim. A correct fact is counted once. Record the labels and rationale in the assessment.
3. Record whether the **first** response to each follow-up supplied the requested evidence. Later responses cannot replace the first response in this metric. Evaluation does not accept evidence or close a finding.
4. Export the measurements as JSON. Fact precision is correct/extracted; recall is correct/expected; citation accuracy is correct/submitted citations. First-response success uses only assessed first responses. A missing denominator is `null`, not 100%. Later evaluations supersede earlier ones in totals while preserving history.

Older Phase 5 results that did not retain the submitted citation count are excluded from citation accuracy. Invalid citations in new results remain in the denominator. Demo-parser performance must not be presented as live-model performance; evaluate separate cases for each provider/model and document the sample size and selection method.

Run representative insurer cases covering a seven-hour restore, linked three-hour retest, missing access review, stale evidence, failed functional checks, ambiguous scans, conflicting measurements and legitimate exceptions. Also exercise duplicate uploads/polls, revoked access during processing, queue downtime and provider errors. Record completion, review time, accuracy denominators and whether the first follow-up worked. Have the insurer agree acceptance criteria before assessing results.

## Verify recovery of platform records and evidence

The recovery tool exports a consistent PostgreSQL snapshot and the exact bytes of every referenced evidence version, including pinned S3 objects and legacy sample files. It encrypts the bundle in authenticated, ordered chunks. The key and bundle must be stored separately in protected locations; `.local` is ignored by Git.

With PostgreSQL running and matching `pg_dump`/`pg_restore` clients installed:

```sh
python manage.py recovery_bundle key --key-file .local/recovery/platform.key
python manage.py recovery_bundle create --key-file .local/recovery/platform.key --bundle .local/recovery/pilot.bundle
python manage.py recovery_bundle verify --key-file .local/recovery/platform.key --bundle .local/recovery/pilot.bundle
```

Use a new bundle filename for each backup. Keys and existing bundles are never overwritten. Verification restores into a randomly named temporary database, compares every table's records, restores evidence to temporary private files, and compares file hashes, sizes and database references. It deletes the temporary database and records a successful drill. It never accepts a working database name as a restore target.

The operator running a drill needs `CREATEDB` permission. The Docker web application's restricted role intentionally lacks it; use a separately controlled backup/restore operator role for staging drills. The local development role supports the drill. The pilot tool has an 8 GB archive limit and a five-minute timeout per PostgreSQL utility; larger deployments need managed backup/PITR tooling.

A successful local drill does not test cloud failover, restoring an S3 bucket in another region, SMTP or external provider recovery. Preserve the encryption key, Django secret, deployment configuration and external-storage recovery procedures separately. Do not use an untrusted third-party database dump as a recovery bundle.

## Verification performed

The 195-test regression suite passed, including the existing real Redis/Celery and OCR tests. Two additional integration tests passed for exact recovery of separate document versions and monitoring through a real Celery worker: **197 tests passed in total**. The development database recovery drill verified 76 tables and its one existing legacy evidence file. Django checks and migration consistency also passed.

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
AI_RUN_QUEUE_TEST=1 RECOVERY_RUN_TEST=1 python manage.py test workspaces evidence underwriting ai_review operations monitoring --noinput
```

The optional recovery test requires PostgreSQL client tools and database creation permission. Live provider calls, Docker execution, hosted deployment and insurer acceptance remain unverified. Browser automation was unavailable; routes and forms were exercised using Django tests.
