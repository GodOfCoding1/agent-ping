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

Production now uses Cloud Tasks at the actual next due time, plus hourly recovery;
see [production.md](production.md). It has no continuous worker or LISTEN connection.
The following direct-connection notes describe the local/long-running worker option.

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

## Android replica and offline actions

Room stores server entity snapshots, the sync cursor, pending actions/receipts and
unique event claims. Each sync page commits all entities and its cursor in one
transaction. A full snapshot replaces the replica while retaining pending local
action overlays. Offline actions persist a UUID idempotency key and local state
in the same transaction; replay uses `/sync/actions` before downloading changes.
Rejected/stale actions resolve to canonical server state and remain in local activity.
Retries use network-constrained WorkManager with exponential backoff; background
reconciliation runs every six hours, and app open/push hints request immediate sync.
Device or settings changes force a server full snapshot to recalculate targets.
These are local development flows; no polling or hosting changes are introduced.

New sync signals replace a backed-off pending one-shot WorkManager request so a
new FCM event does not wait behind a long offline retry chain. Durable action IDs
and receipt records make cancellation/replay safe. An event claimed before a
process crash can resume; an already-posted notification with the same event
identity is recognized before retrying. Alarm ringing shares the foreground
notification ID, preventing a second mirrored phone/watch notification.

Current device targets and critical opt-in are checked again when a push retry
actually dispatches. A queued job does not retain authority to alert a former
primary or ignore a later user opt-out.
