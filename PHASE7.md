# Phase 7 — continuous monitoring

Phase 7 adds scoped assets, recurring authorized connections, versioned insurer rules, grouped observations, source reconciliation, internal follow-up findings, renewal comparisons and workspace reminders. Microsoft Entra ID and AWS Backup are the selected first integrations. Live access defaults to disabled; no external applicant systems were queried during implementation.

## Run the local workflow

Start PostgreSQL, Redis, Django and a Celery worker as described in [Phase 5](PHASE5.md). In a third terminal start exactly one scheduler:

```sh
source .venv/bin/activate
celery -A pilot beat --schedule=.local/celerybeat-schedule --loglevel=INFO
```

From an authorized case open **Continuous monitoring & authorizations**:

1. Register each known asset against a service and evidence request. Supported identifiers are Entra tenant UUIDs, AWS resource ARNs, public IPs and email domains. Asset identifiers and connection scope are immutable; use a new connection for a scope change.
2. As underwriter, publish monitoring rules: MFA registration target, maximum backup age, source freshness, restricted ports, poll interval and follow-up deadline. Automatic follow-up is optional and explicitly enabled. Rules retain their versions.
3. Prepare a connection. For a local demonstration choose **Fictional local observations** with `{"scenario":"risk"}` or `{"scenario":"healthy"}`. Use a fictional tenant UUID such as `11111111-1111-4111-8111-111111111111`, resource `arn:aws:ec2:us-east-1:123456789012:volume/vol-demo`, IP `192.0.2.10`, or domain `example.test` as appropriate. Documentation IPs are accepted only as demo inventory and cannot be polled live.
4. Sign in as business owner and authorize the displayed provider/assets/settings until an expiry within current case access. Confirm asset authority and the necessary provider license rights. No connector runs solely because an underwriter configured it.
5. As reviewer, select **Poll authorized source** and refresh. Beat checks connections every five minutes; each connection's policy controls its polling interval, with a minimum of 15 minutes. Repeated queue requests within a slot reuse the same run.
6. Inspect source, observation date, collection date, caveats and duplicate groups. If rules indicate follow-up and automatic work is enabled, a Phase 4 finding is created for the underwriter who published the policy. It is internal work; no message is sent. Reassign it through Phase 4 when appropriate.
7. Prepare two differently valued sources for the same asset to exercise **Review sources**. Prefer a current observation or request clarification, with a rationale. New source observations invalidate an old reconciliation. Reconciliation preserves all original records and does not approve a control.
8. Use **Compare at renewal** on a linked renewal. The reviewer must have current evidence access to both cases. Assets match by type and exact identifier; renewal assets and authorizations must be prepared separately. Prior observations are never copied as fresh evidence.

Repeated identical source events reuse an observation with another collection receipt. Repeated equal values remain in one source/asset group. Older events remain history and cannot replace newer observations or create new follow-ups. Existing work is not duplicated for each poll. If a risk returns after a different observation and the prior finding is closed, a new episode can create another finding.

Reminders cover case/renewal dates, evidence deadlines/freshness, finding deadlines and stale observations. Beat refreshes them hourly; reviewers can refresh manually. Acknowledging a reminder does not close its underlying work. No email or external notifications are sent.

## Configure live connections

Set `MONITORING_LIVE_ENABLED=true` in the private server environment and restart Django and Celery. Keep provider secrets in named environment variables such as `MONITOR_SECRET_ENTRA`; connection forms accept only the variable name. Live polling still requires business authorization. Provider/configuration changes require a new connection and authorization.

| Provider | Configuration | Meaning and limits |
| --- | --- | --- |
| Microsoft Entra ID | One tenant asset; `{"client_id":"application-uuid"}`; secret reference names the client secret. Grant the app `AuditLog.Read.All` with tenant administrator consent. | Reads all pages of authentication-method registration details, retaining aggregate counts only. Registration/capability does not prove enforced MFA or privileged-account coverage; disabled users are excluded. Missing report dates are labelled unknown. |
| AWS Backup | Resource ARNs must match `{"account_id":"123456789012","region":"us-east-1"}`. Optionally set `role_arn`; the secret reference may contain its external ID. | Uses the worker role/standard AWS credential chain or assumes the specified role. Verifies the resulting account with STS. Reads the latest backup job per exact resource in the 30-day window. Completed-with-issues remains partial, and a successful job never verifies restore capability. |
| Shodan InternetDB | Public IP assets; `{}`. Confirm a commercial license for insurer use. | Passive third-party observations of ports and potential CVEs. Does not scan the asset. Source timestamps are unavailable; collection time is explicitly labelled. A 404 is missing data, never a clean result. |
| Have I Been Pwned | Email-domain assets; `{}`; secret reference names a licensed API key. Domain must already be verified for the subscription. | Uses the stealer-log email-domain endpoint and retains only affected-alias/site counts. No passwords, account aliases or full breached records are stored. Counts indicate historical exposure, not proof of current compromise. |
| CISA KEV | Known IP/AWS assets; `{}`; record their observed CVE inventory and source first. | Matches exact inventoried CVE identifiers to the KEV catalog. It does not infer vulnerabilities from a product name, scan software or determine remediation. Freshness is based on the asset inventory observation. |

For AWS, grant only the necessary read permissions (`backup:ListBackupJobs`, STS identity and, if used, assume-role access). Provider permissions/licenses must be configured outside this application. HTTP adapters allow only fixed HTTPS provider hosts, reject redirects and unexpected pagination hosts, bound response sizes/pages, and fail incomplete polls rather than publishing partial “healthy” results.

These integrations follow the official [Microsoft Graph registration-report API](https://learn.microsoft.com/en-us/graph/api/authenticationmethodsroot-list-userregistrationdetails?view=graph-rest-1.0), [AWS Backup job API](https://docs.aws.amazon.com/boto3/latest/reference/services/backup/client/list_backup_jobs.html), [Shodan InternetDB terms and data description](https://internetdb.shodan.io/), [Have I Been Pwned licensed stealer-log API](https://haveibeenpwned.com/API/v3), and [CISA KEV schema](https://github.com/cisagov/kev-data/blob/develop/known_exploited_vulnerabilities_schema.json). Check account entitlements before live use.

## Access, failure handling and history

Every run pins its requester, connection, owner authorization and insurer rule version. Access and scope are checked before provider calls and again before observations/findings are saved. Revocation during a call discards returned results, although it cannot recall an already sent request. Previously saved observations remain local case records under existing evidence permissions.

Owners manage monitoring authorization; insurer reviewers inspect observations and reconcile sources; underwriters publish rules and configure connections. Technicians receive assigned Phase 4 work through their existing request permissions. Jobs, exports and comparison pages enforce case permissions and MFA. Business-owner consent does not give a technician access to insurer-only monitoring analysis.

Provider and queue failures are shown with sanitized error codes. Transient errors have at most three attempts; explicit retry preserves attempt history. Interrupted workers become eligible for retry after their five-minute lease. Changed policy or authorization blocks an old run; prepare/poll under the new authorization instead. Manual evidence review remains available.

Monitoring information is included in newly prepared underwriting snapshots when a case has monitored assets. Pending or conflicting observations never update accepted evidence reviews, control assessments, backup records or restore tests automatically. Internal follow-ups preserve source and rule provenance. A reconciliation records a review preference; it does not silently discard source observations or withdraw existing findings.

Live connector responses were tested with mocks, including pagination, incorrect AWS accounts, incomplete/missing data and privacy filtering. Real credentials, subscription rights and provider behavior still need staging verification. The fictional workflow can be exercised locally now.
