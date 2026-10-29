# Google Cloud hosting decision and setup

## Current status — 2026-10-29

The user subsequently authorized backend production deployment and selected Google
Cloud IAM first. The private Cloud Run backend, Google OIDC owner verification,
Neon production schema, Cloud Tasks and hourly recovery scheduler are now deployed.
Live REST/MCP/authentication/scheduled-dispatch checks pass. Android release is
deferred. See [production.md](production.md) for the current configuration, actual
verification results, cost assumptions and operational limits. The earlier setup
and unapproved-plan sections below are historical and are superseded by this status.

## Budget revision and current decision

The user set a deployment budget of **INR 500/month or less**. The previous
proposal for an always-on Cloud Run worker pool plus continuously active paid
Neon compute is **withdrawn**. It was never deployed. Do not treat the old
USD 50–65 planning estimate as a bill or an approved deployment budget.

A fresh read-only inventory on 2026-10-28 confirmed no Cloud Run services,
worker pools, jobs, Artifact Registry repositories or Secret Manager secrets in
the setup target. Neon remains on its Free plan. There is no paid AgentPing
compute to delete. Local tooling, Firebase registrations, IAM credentials and
free Neon development branches remain useful and were preserved.

No replacement hosting architecture has been approved for deployment yet.

The existing VM `automation-server` is a running e2-micro (1 GiB RAM) in
`us-central1-a`, with a 20 GiB standard persistent disk and an external IPv4
address. Its existing bill is separate from AgentPing. Only control-plane
metadata was inspected; actual free RAM/load was not measured. No VM workloads,
firewall settings, SSH metadata or service identities were changed.

## Completed configuration

- Authenticated Google Cloud CLI 590.0.1.
- Named local configuration `agentping`, project `personalvm-511016`, region
  `asia-southeast1`. Default gcloud configuration was not replaced.
- Existing project billing confirmed enabled; no billing account link/change.
- Enabled Cloud Run, Artifact Registry, Secret Manager, IAM, IAM Credentials,
  Firebase Management and FCM APIs.
- Added Firebase to this existing Google Cloud project after inspecting the
  existing Firebase projects. No new Google Cloud project was created.
- Registered Android apps `com.errorfactory.agentping` and its `.debug` variant.
- Created `agentping-dev@personalvm-511016.iam.gserviceaccount.com`.
- Created custom role `projects/personalvm-511016/roles/agentPingFcmSender` with
  `cloudmessaging.messages.create`, granted only to the development sender.
- Granted the signed-in human Token Creator on that service account and configured
  local impersonated ADC. No private key was created.
- Verified API-service and worker-pool listing in the selected region: both empty.

No Cloud Run service, worker pool, job, Artifact Registry repository/image,
Secret Manager secret, new VM, Cloud SQL database, or new public endpoint was
created. No Railway tooling or resource was set up.

## Low-cost alternatives to evaluate

### Request-driven Cloud Run with queued work

Planning target at light personal use: approximately INR 0–300/month additional
application charges, provided shared free allowances are available and the
workload lets Neon suspend. This is a usage-based estimate, not a spending cap.

- Run the API and a bounded work-processing HTTP handler with minimum instances 0
  and request-based billing. Do not create a continuous worker pool.
- Keep canonical reminder data and the transactional outbox in PostgreSQL.
- Use Cloud Tasks to trigger bounded, idempotent processing, with a low-frequency
  Cloud Scheduler recovery/reconciliation trigger. These services are only
  proposed: neither was enabled/provisioned by this budget revision.
- Handle the database-write/task-enqueue failure window through the durable
  outbox and recovery scan. Cloud Tasks is not an atomic extension of a Postgres
  transaction and must not be treated as one.
- Avoid a database query every minute while idle. A frequent recovery cron would
  keep Neon awake and defeat the Free-plan savings. Recovery cadence and next-job
  wake-up strategy require explicit latency/reliability decisions.
- Synchronize future alarms to Android in advance. AlarmManager still fires local
  scheduled alarms offline; server cold starts do not replace local scheduling.
- Expect API cold starts. Measure actual compute, storage, logs and egress costs.

Cloud Tasks includes the first 1 million operations/month free; Cloud Scheduler
includes three jobs per billing account. Their allowances are shared, and other
Google Cloud charges can still apply.

### Reuse the existing VM

Potentially near-zero additional compute cost if it has enough spare resources.
Keep its pre-existing VM/IP/network bill separate when evaluating the INR 500
limit. The current machine/disk match the type/region/size of Google's Compute
Free-tier offer, but account-wide free-quota availability and existing charges
must be checked before promising a total bill.

A read-only capacity assessment is required before deploying anything. A small
API/worker could use existing capacity, with systemd/container restart recovery.
Use idle-aware work acquisition so Neon can remain within Free compute limits.
Do not assume a cheap VM makes continuous Neon polling free. HTTPS and a suitable
Google service identity also need a reviewed setup; do not disrupt current VM
applications or change its identity/firewall without approval.

## Cost guardrails

- No paid Cloud Run worker pool, paid minimum instances, Neon plan upgrade,
  second hosted database, new VM or paid identity-provider tier is authorized.
- Neon Free includes 100 CU-hours/project/month. An idle compute suspends after
  five minutes. All development/test/production compute in the project shares
  the allowance; always-on polling at 0.25 CU exceeds it over a full month.
- FCM remains free. API enabling/IAM/app registration did not create an always-on
  application. The existing VM's prior charges continue independently.
- Set conservative request/instance/task-rate limits and log/image retention
  before deployment. Google billing alerts are notifications, not a hard cap.
- Obtain user approval for the final budgeted architecture before provisioning.
  A current free-tier estimate does not authorize later automatic paid upgrades.

Official pricing and behavior references consulted 2026-10-28:

- https://cloud.google.com/run/pricing
- https://cloud.google.com/tasks/pricing
- https://cloud.google.com/scheduler/pricing
- https://docs.cloud.google.com/free/docs/free-cloud-features
- https://neon.com/docs/introduction/plans
- https://firebase.google.com/products/cloud-messaging
