# Phase 3 evidence collection and recovery verification

Phase 3 adds private uploads, logical documents with immutable versions, service inventory, versioned recovery targets, evidence reviews, control assessments, separate backup-job observations, and supervised restore tests with linked retests. Existing Phase 2 cases and permissions are retained.

## Start the updated application

```sh
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/dev_db.py start
python manage.py migrate
python manage.py seed_phase2
python manage.py prepare_phase3_demo
python manage.py runserver 127.0.0.1:8000
```

Open http://127.0.0.1:8000/. Existing passwords and MFA settings remain unchanged. New development credentials, if needed, are in `.local/demo-credentials.txt`. Complete the Phase 2 business invitation, authorization, and technician assignment steps before working with evidence.

From a case, open **Service inventory & recovery targets** or **Open evidence & review** under a checklist item. The migrated primary service uses the case's existing recovery targets. Additional services can be added by the owner or an authorized insurer reviewer; only insurer reviewers can publish new target versions. Existing tests retain their original target version and service snapshot.

## Seven-hour restore and three-hour retest walkthrough

The demo preparation command writes fictional documents into `.local/phase3-samples/`. It does not grant access, fabricate reviews, or send messages. Existing files are preserved.

1. Assign the supervised restore request to `technician@example.test`. As the technician, open the request and submit `restore-initial.txt`. Enter its source, service scope, and actual observation time. File-check status must be clear before download or review acceptance.
2. As the underwriter, review that exact version. Confirm its scope and accept it with a rationale. Evidence acceptance establishes that the document is usable; it does not automatically verify the control or restore outcome.
3. As the technician, record a supervised restore for the same service with **420 minutes** elapsed, **35 minutes** recovered data age, functional checks **passed**, and a description of those checks. Cite the initial document version. Enter the actual test time, before now; a report's observation time may be later than the test it describes.
4. As the underwriter, open the test, confirm full scope, and record the review. The rule returns **Target missed** against the **240-minute** target. The original record stays unchanged.
5. Upload `restore-retest.txt` using **Upload next version** on the same document, and have the underwriter accept version 2. Record a **linked retest** with **180 minutes** elapsed, **25 minutes** recovered data age, passed functional checks, and a test time later than the initial test. Cite version 2.
6. Review the retest. It returns **Passed**. The first test still shows its seven-hour measurement and missed result. Its link to version 1 remains intact.
7. Record a control assessment citing the current accepted evidence version. The supervised restore control can be verified only when the latest test has a passing review against current targets. Return to the checklist: **Access review remains Missing**.

Upload `backup-job.txt` under the backup-coverage request to record a successful backup job separately. Backup-job success never generates a passing restore result. `mfa-coverage.txt` can be used for the ordinary control review path.

## Evidence and assessment rules

Uploads require an authorized case/request, source, scope, service, observation timestamp, and file. Evidence cannot be dated in the future. Supported formats are PDF, DOCX, UTF-8 TXT/CSV/JSON, PNG, and JPEG, up to 10 MB. Multipart parsing also limits file size before writing large temporary uploads. PDF forms/annotations, scripts, attachments, encrypted PDFs, DOCX macros, and embedded objects are rejected; provide flattened evidence where necessary.

Each version records the uploader, upload and observation times, SHA-256 checksum, byte length, content type, source, covered scope, and exact storage reference. Versions use unique object keys. On S3, the returned object version ID is also pinned. Resubmitting an identical submission token or the same bytes and metadata for the same document returns the existing version. It cannot silently overwrite a reviewed file.

Intake commits a quarantined version before file checks. Failed or unavailable scanners leave it unavailable for download, acceptance, and test use. Insurer reviewers can retry checks. A rejected evidence review concerns suitability; a rejected file check concerns file safety. Review records and prior file-check records are preserved separately.

Freshness uses observation time, not upload time: evidence remains fresh through `observed_at + freshness_days`, inclusive, in UTC. Evidence acceptance and freshness are separate. A control marked Verified requires the current document versions to be accepted, scope-confirmed, and fresh. New versions or changed reviews make a prior control result require review. The previous assessment retains its exact version IDs, review IDs, checksums, checklist version, and rule version.

Restore tests record service scope, supervisor, test time, elapsed minutes, recovered data age, functional result, check details, supporting evidence versions, and a pinned recovery-policy version. Measurements are nonnegative; zero means a measured zero, and a blank measurement means unknown. Retests must belong to the same service and request and occur later than their parent test.

The `restore-v1` rule distinguishes **Passed**, **Target missed**, **Failed**, **Partial**, and **Inconclusive**. Failed functional checks produce Failed; incomplete confirmed scope produces Partial; missing measurements, unknown scope, stale evidence/test dates, or unaccepted evidence produce Inconclusive. A complete verified test must meet both targets. Later evidence review changes, expiry, or new targets are shown as a need for review, stale evidence, or a prior target version without overwriting historical review results.

Case exports now use schema version 2 and include authorized request evidence metadata, version references, control assessments, backup observations, and linked restore results. They are working case exports, not approved underwriting reports; report approval belongs to Phase 4.

## Private local development storage

The default is `EVIDENCE_STORAGE_BACKEND=local`. Files are stored under ignored `.local/evidence` and served only through authenticated, authorized Django downloads. There is no public media URL. Downloads verify the saved checksum and recheck permission after reading storage. Revocation prevents subsequent downloads; an already delivered file cannot be recalled.

`EVIDENCE_SCAN_MODE=basic` performs structural checks only and is permitted only with `DJANGO_DEBUG=true`. It is not malware scanning. The UI identifies this explicitly. Real applicant evidence should use the S3 and malware-scanner setup below. Local storage remains persistent across server restarts.

## Configure private Amazon S3

No AWS bucket or credentials were present during development. S3 behavior is covered with the Moto emulator; live AWS permissions and connectivity still need verification with your chosen bucket.

An optional CloudFormation template is provided in `infra/evidence-bucket.yaml`. It defines a retained bucket with versioning, blocked public access, encryption, ownership controls, and a policy denying unencrypted transport. No AWS resources are created automatically.

Configure the application environment:

```dotenv
EVIDENCE_STORAGE_BACKEND=s3
EVIDENCE_S3_BUCKET=your-private-versioned-bucket
AWS_REGION=us-east-1
# Optional: use a customer-managed KMS key instead of SSE-S3.
# EVIDENCE_S3_KMS_KEY_ID=your-key-arn
EVIDENCE_SCAN_MODE=external
EVIDENCE_SCAN_COMMAND=["clamscan","--no-summary"]
```

Use the standard AWS SDK credential chain, preferably a scoped workload IAM role. Never commit credentials. The application needs bucket-level `s3:GetBucketVersioning`, `s3:GetBucketPublicAccessBlock`, and `s3:GetEncryptionConfiguration`; and `s3:PutObject`, `s3:GetObjectVersion`, and `s3:DeleteObjectVersion` restricted to the bucket's `cases/*` prefix. Version deletion is used only to clean up failed intake before a database record commits. KMS mode additionally needs the corresponding KMS permissions. Set retention and deletion policies with the insurer before a live pilot.

Check the configured bucket without modifying it:

```sh
python manage.py check_evidence_storage
```

Uploads pass through Django, which authorizes the request, enforces limits, stores the object with a checksum and encryption, and verifies returned metadata. Downloads use the exact S3 version and return an attachment through Django; reusable presigned or public URLs are not exposed. A bucket without enabled versioning or all four public-access blocks is refused. Storage failures are visible and do not create accepted evidence records. If an external storage cleanup also fails, reconcile unreferenced objects before the pilot; storage and PostgreSQL are not a distributed transaction.

AWS references: [S3 object writes](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html), [S3 security practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html).

## Malware scanner contract

`EVIDENCE_SCAN_COMMAND` is a JSON array of a trusted scanner executable and fixed arguments. The application appends a temporary file path and executes without a shell. Exit 0 means clean, exit 1 means rejected, and any other exit, missing executable, or 30-second timeout leaves the version quarantined. Scanner output is not stored in application logs. Configure and update the scanner and signature database in the deployment environment.

Production deployment checks require S3 and an external scanner. The local prototype has no installed malware scanner. No real file is declared malware-free by the development validator.

## Validation and boundaries

```sh
python -m pip install -r requirements-dev.txt
python manage.py test workspaces evidence --noinput
python manage.py makemigrations --check --dry-run
python manage.py check
```

Tests use PostgreSQL and an in-process S3 emulator. They exercise the seven-hour result and linked three-hour retest, missing/negative measurements, failed functional checks, partial scope, freshness boundaries, version history, duplicate intake, quarantine, scope authorization, revoked access during storage operations, file tampering, and S3 version pinning.

The platform still uses Django templates and PostgreSQL. Phase 4 now adds decisions, conditions, findings/fix workflows, expiring exceptions, approved reports, and linked renewals; see [PHASE4.md](PHASE4.md). OCR/AI and live monitoring remain later phases. File checks are synchronous in Phase 3; retryable quarantined records provide the handoff for later worker processing. Application immutability is not a substitute for production database permissions, backups, or regulatory retention controls.
