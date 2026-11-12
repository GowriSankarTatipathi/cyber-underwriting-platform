# Phase 2 workspaces and case access

The application retains the fictional Phase 1 restore demonstration and introduces persistent application and renewal cases. Business authorization is separate from authentication, insurer membership, and a contributor's case-specific sharing grant.

## Access rules

| Actor | Case visibility | Permitted work |
| --- | --- | --- |
| Insurer administrator | Cases belonging to their active insurer membership | Create, assign, invite business owners, view internal notes, assign requests |
| Underwriter | Cases they created or are assigned, within an active insurer membership | Create, assign, invite business owners, edit internal notes, assign requests |
| Technical reviewer | Assigned cases within an active insurer membership | View case and internal notes; evidence requires active business authorization |
| Business owner | Cases with an accepted, active owner grant | Authorize/revoke insurer access, invite/revoke technicians, assign requests, view contributor evidence |
| IT technician | Cases with an accepted, active technician grant and active business authorization | View assigned requests and their files; export only assigned requests |
| Platform superuser | No automatic case access | Explicit insurer membership or case grant is still required |

The underwriter can see administrative case details, assignments, internal notes, and audit history before authorization or after revocation. Evidence request content, downloads, and exports are withheld until business authorization is active. Owners retain their business access while their own grant remains active. Technicians lose case visibility when business authorization is revoked or expires.

The same policy scopes case queues, searches, detail pages, downloads, exports, and job status. `authorize_job_execution()` rechecks the requesting user's current status, grant, membership, and consent. Future workers must call it before processing and again before external transmission or publishing results. No AI service or background processor is enabled in Phase 2.

## Invitations and account security

Invitations use cryptographically random tokens stored as SHA-256 hashes. They expire after seven days, bind an exact email address and case role, and can only be accepted once using POST. The issuing actor's authority is checked again at acceptance. Replacement invitations invalidate prior unused links for the same case and email. New users choose a password through their addressed invitation; existing users must sign in as the invited account.

Invitation links are displayed once for manual delivery. No messages are sent externally. Password reset and security notifications use the local file email backend under `.local/mail`. Before any external pilot, configure an approved email service and mask invitation-token paths in HTTP access logs.

Django-allauth provides password login, TOTP authentication, one-time recovery codes, reauthentication, and rate limiting. Application middleware requires enrolled TOTP and an MFA authentication record bound to the current user before workspace or invitation acceptance access. MFA deactivation is disabled. Account recovery beyond saved recovery codes requires a separately documented operator identity-verification procedure; there is no browser support bypass.

The secret key must be stable and kept with the platform's recovery material, because it protects MFA secrets as well as sessions. Live deployments must supply a secrets manager value, HTTPS, approved hosts, and `DJANGO_DEBUG=false`. The local development web server is not a deployment target.

## Data and history

Cases reference an insurer-specific, immutable checklist version. New versions are separate records. Creation copies requirements into the case request list. Assignments are restricted to active members of the case insurer or active invited technicians, depending on the action. Internal notes remain insurer-only.

Audit records cover case creation, invitations, acceptance, consent, assignments, revocations, internal-note updates, downloads, and exports. Phase 2 does not claim a tamper-proof audit store or regulatory retention policy; those operational decisions remain part of the insurer pilot.

Historical Phase 2 boundary: the local restore brief is explicitly fictional. Files are served only through a case-scoped view, with no public media route, path containment checks, attachment disposition, and no-store caching. Phase 2 had no upload endpoint. Phase 3 now adds the evidence intake and versioned review workflow described in [PHASE3.md](PHASE3.md); this original fixture remains a separate fictional sample.

## Operator setup

`seed_phase2` creates the fictional insurer, checklist, renewal, accounts, and sample brief idempotently. It does not authorize consent or accept invitations on anyone's behalf. To provision an additional verified insurer reviewer:

```sh
python manage.py provision_reviewer person@example.com INSURER_UUID --role reviewer
```

The command prompts for a password without echoing it for new users. The operator must verify the identity and insurer affiliation before provisioning; it grants no automatic business consent. Existing users' passwords are not reset.

## Acceptance walkthrough

An underwriter creates and assigns a case, issues a business-owner invitation, and observes business authorization. The owner invites a technician and assigns requests. The technician can open the assigned request list and its fictional fixture, while a second insurer and an unassigned reviewer cannot. Revoking or expiring access blocks subsequent pages, downloads, exports, and worker authorization. All state-changing browser actions require CSRF-protected POST requests.

Phase 1 insurer agreement is still a business prerequisite: the sample requirements, reviewer responsibilities, recovery targets, and exception authority remain fictional workshop defaults.
