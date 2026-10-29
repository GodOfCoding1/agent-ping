# Production backend

The user authorized backend production deployment on 2026-10-29, then selected
Google Cloud IAM access first. Android release work remains deferred.

## Verified result — 2026-10-29

Live revision `agentping-api-00003-5tk` passed:

- HTTPS liveness and production database readiness at schema `0002`.
- HTTP 403 without IAM credentials; application and worker reject invalid tokens.
- Google OIDC owner authentication and agent scope restrictions.
- One-time device pairing and device credential sync using a synthetic device.
- MCP initialization, 12 tools, reminder creation and idempotent replay.
- A real scheduled Cloud Tasks dispatch, observed about **2.3 seconds after due**,
  without manually invoking the worker. No dead jobs; FCM provider configured;
  continuous embedded worker disabled.
- Test reminder cancelled, temporary agent revoked and synthetic device deactivated.
- The separately invoked Cloud Scheduler recovery job completed with HTTP 200;
  its schedule remains enabled hourly. No ERROR logs on the final revision during
  verification. Source scan: 187 Git-eligible files, zero actual credential matches;
  production state/credentials are ignored by Git.

Local checks: **144 backend tests passed, no skips**; backend Ruff/format/mypy pass;
production helper Ruff/mypy pass; Linux/amd64 container build and publish pass;
doctor exit 0. The initial database export was restored locally at schema `0002`
with 16 application tables. Production phone receipt/display and Android release
are deliberately not claimed.

## Infrastructure

- Google Cloud project: `personalvm-511016`, region `asia-southeast1`.
- Cloud Run service: `agentping-api`, request-based billing, CPU throttling,
  1 vCPU / 512 MiB, concurrency 8, minimum instances 0, maximum instances 1.
- HTTPS endpoint: https://agentping-api-583092569828.asia-southeast1.run.app
- Cloud Run IAM invocation is enabled. Explicit invokers are the signed-in owner
  and `agentping-task-invoker`; no `allUsers` / `allAuthenticatedUsers` grant.
  Existing project administrators retain their existing administrative powers.
- Runtime identity: `agentping-prod`. Its grants are limited to the two application
  secrets, task enqueue on the application queue, act-as on the task invoker, and
  the existing custom FCM send role. No service-account private keys.
- Artifact Registry: `agentping`, with cleanup of versions older than seven days
  while retaining the latest two. Automatic paid scanning is not enabled.
- Cloud Tasks queue: `agentping-production`, one concurrent dispatch, one dispatch
  per second, eight attempts, one-hour retry duration.
- Cloud Scheduler: `agentping-recovery`, hourly at minute zero UTC, authenticated
  as the task invoker. The worker also checks a separate secret trigger header.
- Neon project remains **Free**. Only the existing production branch
  `br-sweet-mouse-a167349g` was initialized; it now has Alembic schema `0002`.
  Its compute is bounded to 0.25 CU and retains the Free-plan suspend behavior.
- Runtime and migration roles/passwords are separate. Application tables belong
  to `agentping_migrator`; `agentping_runtime` has DML access and cannot CREATE in
  public or assume the migrator role. Connections use certificate and hostname
  verification with the runtime's maintained CA bundle.
- The existing `automation-server` VM was not modified.

## Authentication and access

`APP_ENV=production`, `AUTH_MODE=oidc`. Owner authentication verifies Google-issued
JWT signatures, issuer, audience, expiry and an explicit subject allowlist. The
current owner client is the existing Google Cloud CLI identity, not a completed
Android OAuth login flow.

For this private deployment, send the Cloud Run IAM credential using
`Authorization` or `X-Serverless-Authorization`. Send the separate application
credential as `X-AgentPing-Authorization: Bearer ...`. Google modifies the IAM
token signature in transit; using a separate application header preserves the
independently verifiable application JWT. Agent/device opaque credentials use
that same application header. Both authorization layers must pass.

Default local development still uses `Authorization`. No development owner token
was uploaded. Public OpenAPI/Swagger endpoints are disabled. `/health` is the
hosted liveness path; Cloud Run's frontend intercepts `/healthz`, although the
container can still use the legacy path locally.

## Scheduled work and reliability

The transactional PostgreSQL outbox remains canonical. After a transaction that
enqueues work commits, the API asks Cloud Tasks to wake the bounded worker at the
earliest pending job or expired lease. A worker request drains due jobs and queues
the next actual due time, rather than waking every five minutes. Maintenance is
initialized by the worker and remains six-hourly.

Task IDs deduplicate concurrent scheduling. Changed attempt/lease state and an
hourly recovery bucket allow failed wake-ups to be recreated. If task enqueue
fails after a successful database commit, the write remains successful and the
hourly recovery trigger repairs the gap. In that failure mode, work can be delayed
until recovery; this budget configuration does not promise zero-delay recovery.
Failure to schedule the next wake-up from a worker request returns an error for
Cloud Tasks retry. Worker requests are bounded to 85 seconds with durable leases
covering cancellation/restart recovery.

No resident worker, paid minimum instance, background LISTEN connection or
minute-by-minute idle database polling is deployed. Cloud cold starts and provider
delivery delays still apply; this is not an emergency-alert SLA.

## Operations

Run these from the repository root. They deliberately do not load `.env.local`.

```sh
uv run --project backend --locked python infra/production_smoke.py
```

The smoke check creates a named synthetic device without a push token, temporary
agent and scheduled reminder, tests REST/MCP/sync/idempotency and real task
dispatch, then cancels/deactivates/revokes them. It never claims phone delivery.

Explicit deployment stages are in `infra/production.py`: `identity`, `database`,
`infrastructure`, `build`, `deploy`. These stages mutate production and should only
be run for authorized production maintenance. Output is secret-safe; private
credentials, deployment receipts and diagnostics remain in ignored
`.agentping/production/` with owner-only permissions. Only the runtime database
credential and worker token are in Secret Manager; the migrator password stays local.

A baseline custom-format `pg_dump` was saved privately and restored into an
isolated disposable local PostgreSQL database: schema `0002`, 16 tables. The
disposable restore database was removed afterward. This is a verified initial
backup, not a scheduled offsite backup service. Neon Free's short restore window
and this local baseline are the current recovery limits. Make a fresh encrypted,
off-machine export before storing valuable production data and before migrations.

For application rollback, select a previously verified Cloud Run revision and
restore traffic to it. Check schema compatibility first; never blindly downgrade
production migrations or restore over live data. Early revisions used to diagnose
health/authentication are not verified rollback targets.

## Cost and verification basis

Planning estimate remains **INR 0–300/month additional application charges before
applicable taxes**, for light personal use with shared free allowances available.
The user budget is INR 500/month. This is not a fixed quote or a hard billing cap.

Confirmed on 2026-10-29: Neon organization plan `free`; Cloud Run minimum 0,
maximum 1 with request-based billing; one hourly scheduler job; two active secret
versions; one rate-limited queue; a roughly 89 MB initial container repository;
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
