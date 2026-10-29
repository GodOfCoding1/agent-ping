# Production backend

Updated 2026-10-29. The backend is live with Auth0 browser OAuth and independent
user accounts. **The two-live-user public launch gate remains open** because only
one Google account is available. Automated account-isolation tests pass; do not
represent those as two independent live Google sign-ins. Android release remains deferred.

## Verified deployment

- API/MCP: `agentping-api-00008-xs9`,
  [HTTPS endpoint](https://agentping-api-583092569828.asia-southeast1.run.app).
- Worker: `agentping-worker-00004-btl`, IAM-private, on a separate service.
- Database: schema `0004`; separate migrator/runtime roles, verified DML grants,
  no runtime DDL. Neon remains Free with 0.25-CU compute and managed suspension.
- Both services: request-based billing, min 0/max 1 each, 1 CPU/512 MiB,
  concurrency 8, CPU throttling, no CPU boost or continuous worker.
- Queue: one concurrent dispatch/one per second. Hourly recovery targets the
  private worker; an actual Scheduler execution returned HTTP 200. API identity has no FCM grant; worker uses the existing scoped FCM role.
- Four application secret versions are enabled: runtime DB URL, worker trigger,
  portal client secret and portal encryption key. No service-account private keys.
- Existing `automation-server` VM and development configuration were not changed.

## Evidence

- **180 tests passed**, no skips. Backend Ruff/format/mypy and deployment-helper
  Ruff/mypy pass. Linux/amd64 image built locally and deployed successfully.
- Native `codex mcp login agentping` completed through the real browser flow,
  including explicit `offline_access`. No customer IAM helper or pasted token.
- Real Auth0 signatures, MCP audience/client/scopes, all 12 tools, reminder write
  and idempotency passed over the public endpoint.
- Rotating refresh, identity binding and a fresh MCP connection passed. Real
  scheduled work ran through Cloud Tasks without a manual worker trigger; its
  verification reminder was cancelled afterward.
- Account portal login passed after explicitly configuring RS256 ID-token signing.
  Owner API access, one-time pairing and synthetic device sync passed. The synthetic
  device was deactivated; phone delivery/display/sound are not claimed.
- Existing and freshly refreshed JWTs failed after grant revocation. Explicit owner
  reapproval continued rejecting old JWTs; a new token worked. Codex's grant is active.
- Native Chrome pairing-form submission passed after correcting the portal referrer
  policy. Portal forms use `same-origin`; OAuth redirects retain `no-referrer`.
  CSRF tokens and rejection of null/foreign origins remain enforced.
- Public protected APIs return 401 without credentials; the API has no worker route.
  The worker requires IAM and its trigger secret. Metadata and liveness are public.
- Fresh pre-migration production exports restored successfully into disposable local
  databases at schemas `0002` and `0003`. The scratch databases were removed.
- OAuth callback query strings are excluded from new platform request-log storage;
  sanitized application path/status/request-ID logs remain available. The named
  Log Router exclusion is `agentping-oauth-callback-queries`.

See [OAuth operations](production-oauth-operations.md) for public client IDs,
Codex configuration, explicit deployment commands and rollback. The
[historical private bootstrap](production-bootstrap-history.md) is an audit record,
not current access instructions. Private receipts and token material remain in
ignored `.agentping/production/`; no credentials belong in documentation.

## Remaining checks

A second independent live user must verify login and account isolation before
closing the public launch gate. Android needs the production endpoint/account
flow, release signing and real-device delivery/alarm checks. Regular encrypted
off-machine backups and measured billing observation are still operational work.

## Cost and verification basis

The verified configuration supports a planning estimate of **INR 0–300/month additional application charges before
applicable taxes**, for light personal use with shared free allowances available.
The user budget is INR 500/month. This is not a fixed quote or a hard billing cap.

Read back from provider APIs/CLI on 2026-10-29: Neon organization plan `free`; both Cloud Run services have minimum 0,
maximum 1 each with request-based billing; one hourly scheduler job; four active application secret
versions after the OAuth cutover; one rate-limited queue; Artifact Registry storage of 450,029,546 bytes (about 0.419 GiB) with active cleanup policies;
no new VM, paid Neon tier, load balancer, VPC connector or Cloud Build pipeline.
Images are built locally for Linux/amd64, avoiding a paid build pipeline.

Published price checks:

| Item | Published allowance / charging basis |
| --- | --- |
| [Cloud Run](https://cloud.google.com/run/pricing) | Request-based free allowance: 180,000 vCPU-seconds, 360,000 GiB-seconds, 2 million requests; region pricing and billing-account sharing apply |
| [Neon Free](https://neon.com/pricing) | $0; 100 CU-hours / project / month and 1 GB storage shared by project branches |
| [Cloud Tasks](https://cloud.google.com/tasks/pricing) | First 1 million operations monthly free; task creation and dispatch attempts both count |
| [Cloud Scheduler](https://cloud.google.com/scheduler/pricing) | Three jobs per billing account free; otherwise $0.10/job/month |
| [Secret Manager](https://cloud.google.com/secret-manager/pricing) | Six active versions and 10,000 access operations monthly free across the billing account |
| [Artifact Registry](https://cloud.google.com/artifact-registry/pricing) | First 0.5 GiB-month free across the billing account; storage/transfer beyond allowances are metered |
| [FCM](https://firebase.google.com/products/cloud-messaging) | No charge |
| [Auth0](https://auth0.com/pricing) | Advertised Free tier up to 25,000 external active users; current tenant is in its signup trial; no upgrade performed |

Example capacity check, not measured production usage: 10,000 requests with an
average one billed second at 1 vCPU / 0.5 GiB consume about 10,000 vCPU-seconds and
5,000 GiB-seconds before startup, worker and overlapping-request adjustments.
An otherwise idle hourly recovery wake that keeps 0.25-CU Neon awake for five
minutes consumes about 15.5 CU-hours in a 31-day month. User traffic, other branches,
maintenance and retries consume additional compute. This illustrates why hourly
recovery is affordable while continuous polling would exceed the Free allowance.

An invoice total, remaining billing-account-wide free allowances, taxes, network
egress and measured month-long usage have not been verified. Instance/task limits
reduce exposure but are not a hard INR 500 cap. Existing VM charges are separate.
