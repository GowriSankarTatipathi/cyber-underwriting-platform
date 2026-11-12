# Cyber underwriting platform

A local Django prototype for an insurer's cyber application and renewal workflow. Phase 2 adds PostgreSQL, an underwriter workspace, an evidence portal, invitation-based accounts, required authenticator MFA, case assignments, and expiring sharing grants. All included cases and accounts are fictional.

The frontend uses Django templates and CSS. Next.js is not part of this implementation. The original Phase 1 restore comparison remains at `/demo/`.

## Run locally

Use Python 3.10 or newer and PostgreSQL 14 or newer. From this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/dev_db.py start
python manage.py migrate
python manage.py seed_phase2
python manage.py runserver 127.0.0.1:8000
```

Open http://127.0.0.1:8000/.

`dev_db.py` uses `initdb` and `pg_ctl` from your PATH. It creates a separate PostgreSQL cluster under `.local/postgres`, with a password-protected Unix socket under `.local/run` on port 55432 and no TCP listener. It does not modify a Homebrew database or service. It generates a private `.env` once and does not overwrite an existing configuration.

If you already have a database, or are using Windows, copy `.env.example` to `.env`, set a persistent random `DJANGO_SECRET_KEY` and your PostgreSQL connection settings, create the configured database, and omit `dev_db.py`. For tests, the database role needs permission to create a test database. On Windows activate with `.venv\Scripts\activate`.

Stop the web server with Ctrl+C. Stop this project's database with:

```sh
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

## Walk through Phase 2

1. Sign in as the underwriter. Open `DEMO-001`, or use **Open a case** to create an application or renewal with the `pilot-1` checklist.
2. Assign the case to the technical reviewer if desired. Only active reviewers from the same insurer are available. The creator retains access; an unassigned technical reviewer has none.
3. Select **Invite business owner**, enter `owner@example.test`, and copy the one-time displayed invitation link. Links expire after seven days. They are not automatically emailed.
4. Sign out, open the link, and sign in as the owner. Finish MFA setup, then **Accept case invitation**. Use **Overview** to return to a pending invitation after setup if necessary.
5. As the owner, explicitly authorize case access for a chosen duration. Then invite `technician@example.test` as the IT provider.
6. Sign in as the technician through that invitation and accept it. The portal shows the authorized case, initially with no assigned requests.
7. As the owner or underwriter, assign requests to the technician. The technician can now see only their assigned list. A fictional restore brief on `DEMO-001` exercises private downloads; no real upload processing exists yet.
8. Try **Export list**, then revoke the technician's grant or revoke business authorization. New file and export requests must fail immediately. Internal insurer notes never appear in contributor pages or exports.

Use separate browser profiles or sign out between roles. Invitation links are shown once and stored only as hashes. Reissuing an invitation invalidates earlier unused links for that recipient and case.

## Verify

With the project database running:

```sh
python manage.py check
python manage.py test workspaces --noinput
python manage.py makemigrations --check --dry-run
```

Tests run against a separate PostgreSQL test database. They cover real password and MFA flows, invitation email binding and replay, case creation and assignments, cross-insurer isolation, expired/revoked access, contributor privacy, private files, exports, worker authorization, CSRF, and the retained Phase 1 demonstration.

## Scope and next phase

Phase 2 provides saved cases, pinned checklist versions, request assignments, MFA, access grants, invitation acceptance/revocation, business consent, and access audit events. Insurer administrators can access their insurer's cases; underwriters access created or assigned cases; technical reviewers access assigned cases. Platform superuser status alone does not grant case access.

Case files currently use private local fictional fixtures to exercise permissions. The job model and authorization service establish a boundary for future workers; no Celery processing is running. PostgreSQL is the persistent record store. Public document serving is not configured.

Phase 3 adds private S3 upload intake, document versions, service inventory, reviewer evidence actions, calculated freshness, and persistent supervised restore/retest records. The request list's freshness values are requirements, not calculated evidence evaluations. Underwriting decisions, report approval, renewal history, AI, external connectors, and production deployment remain later phases.

See [Phase 2 implementation and access rules](PHASE2.md) and [the Phase 1 insurer workshop guide](PILOT.md).
