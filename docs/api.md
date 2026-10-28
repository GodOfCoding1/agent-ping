# REST API

Base path `/api/v1`. JSON error envelope:

```json
{"error": {"code": "not_found", "message": "...", "details": {}, "request_id": "..."}}
```

401 responses include `WWW-Authenticate`. 429 includes `Retry-After`. Every
response echoes `X-Request-ID`. Bearer tokens: development owner, agent (`apa_`),
or device (`apd_`).

Interactive docs: `GET /docs` and `GET /openapi.json` (disabled in production).

## System

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/healthz` | none | Process liveness; no database |
| GET | `/readyz` | none | 200 with `schema_revision`, or 503 |
| GET | `/api/v1/diagnostics` | `devices:read` | Worker heartbeats, outbox counts, push, device health |
| POST | `/internal/worker/run` | `X-Worker-Token` | Drains up to 50 due jobs. 404 if no token configured |

## Reminders and notifications

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/reminders` | 201. `Idempotency-Key` header or body field |
| GET | `/reminders` | Filters: `status`, `mode`, `device_id`, `source_agent_id`, `due_after`, `due_before`, `q`, `include_notifications`, `cursor`, `limit` |
| GET | `/reminders/{id}` | Detail, upcoming occurrences, recent deliveries |
| PATCH | `/reminders/{id}` | `expected_revision` and/or `If-Match: "N"` |
| POST | `/reminders/{id}/cancel` | |
| DELETE | `/reminders/{id}` | Same as cancel |
| GET | `/reminders/{id}/history` | Audit events for that reminder |
| POST | `/notifications` | 202 accepted / queued |

`queued` means durably stored. Provider acceptance and device receipt are separate.

## Occurrences

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/occurrences` | `state`, `from`, `to`, `reminder_id`, `cursor`, `limit` |
| GET | `/occurrences/{id}` | Includes per-device deliveries |
| POST | `/occurrences/{id}/acknowledge` | Body optional |
| POST | `/occurrences/{id}/snooze` | `{ "minutes": 15 }` optional |
| POST | `/occurrences/{id}/skip` | `{ "reason": "..." }` required |

Action HTTP 200 results: `applied`, `duplicate`, `rejected_stale`,
`rejected_invalid`. Cancelling a completed reminder is 409 `invalid_state_transition`.

## Devices and sync

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/devices/pairing-codes` | Owner only |
| POST | `/devices/register` | Pairing code (unauthenticated, IP rate-limited) or owner bearer |
| GET | `/devices` | `include_inactive` |
| GET/PATCH | `/devices/me` | Device token. PATCH may rotate the FCM FID |
| POST | `/devices/me/rotate-credential` | |
| POST | `/devices/me/receipts` | displayed / received / scheduled_locally / failed |
| PATCH/DELETE | `/devices/{id}` | Owner: rename, primary, deactivate |
| GET | `/sync` | `cursor`, `limit` ≤ 1000. Full snapshot when the cursor is stale |
| POST | `/sync/actions` | Offline ack/snooze/skip replay; device token required |

Register with `push_token` (Firebase Installation ID) and `push_token_kind`
(`fid`, default, or `registration_token` for legacy FCM tokens).

## Account

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/activity` | `category`, `reminder_id`, `device_id`, `agent_id`, `event_type`, `since`, `until`, `cursor`, `limit` |
| GET/PATCH | `/settings` | Timezone, quiet hours, critical opt-in, push content |
| POST | `/agents` | 201 + one-time token |
| GET | `/agents` | Hashes only; secrets are shown once |
| PATCH | `/agents/{id}` | Name, limits, scopes (grantable set only) |
| POST | `/agents/{id}/revoke` | |
| POST | `/agents/{id}/tokens` | `ttl_days` |
| DELETE | `/agents/tokens/{token_id}` | 204 |

Agents cannot call settings, agent-management or pairing endpoints (403).
