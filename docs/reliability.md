# Reliability and the outbox

All side effects that must survive a crash go through `outbox_jobs` in the same
PostgreSQL transaction as the domain write (transactional outbox).

## Worker

`agentping-worker` (or `EMBEDDED_WORKER=true` inside the API process):

1. Claims due rows with `FOR UPDATE SKIP LOCKED` and a lease.
2. Runs the handler (`occurrence.due`, `push.deliver`, `push.dismiss`,
   `push.sync`, `maintenance.tick`, …).
3. Succeeds, retries with backoff, or marks the job `dead`.

A pending-only partial unique index on `dedupe_key` prevents duplicate in-flight
work. Crashing / unknown kinds go `dead`; `on_dead` writes `worker.job_failed`.

**Push provider rejection after retries is not a dead job.** The delivery is
marked `provider_failed`, an audit event is stored, and the job succeeds so it
does not block the queue.

## Waking the worker without polling Neon to death

On a local or direct Postgres connection the worker `LISTEN`s and writers
`NOTIFY`. PgBouncer transaction pooling cannot keep `LISTEN`, so
`WORKER_LISTEN_DATABASE_URL` must be the **direct** endpoint.

When LISTEN is unavailable (typical Neon pooled URL):

- Local compose uses a direct URL, so LISTEN works.
- Hosted deployments should call `POST /internal/worker/run` with
  `X-Worker-Token` from Cloud Scheduler / an event, rather than an always-on
  poller. Idle max sleep is `WORKER_IDLE_MAX_SECONDS` (default 300).
- Do not run a continuous paid worker pool. Do not poll Neon every few seconds
  while idle; that burns the Free compute allowance.

## Push contract

Payloads are data-only FCM messages: `type=deliver|dismiss|sync`. `event_id` is
per alert and shared across devices. A successful send means the **provider
accepted** the message. Device receipt/display comes from
`POST /devices/me/receipts`.

Targets are Firebase Installation IDs (`push_token_kind=fid`) by default.
Legacy registration tokens are still stored and sent via the deprecated `token`
field when `push_token_kind=registration_token`.

## Audit

`audit_events` is append-only via a trigger. Runtime cannot UPDATE/DELETE rows
or CREATE TABLE. Purging requires `SET agentping.allow_audit_purge='on'` **and**
membership of the table-owner role (`agentping_migrator`).
