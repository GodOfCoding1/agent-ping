# Scheduling

Occurrences are materialized in PostgreSQL. The Android app (when it exists) is
expected to hold a local schedule for the current reminder `version` and fire
offline. The server push at due time is a **fallback** for devices that do not
have a valid local schedule for that version.

## Timezones and DST

- Recurring rules use IANA timezones (`Europe/Berlin`, `Asia/Kolkata`, …).
- Spring-forward gaps skip the missing local time.
- Fall-back repeats use the first (daylight) offset unless the rule says otherwise.
- Stable occurrence IDs are hashed from reminder id + local civil time so DST
  reshuffles do not duplicate rows.
- `timezone_source=primary_device` follows the primary phone; floating reminders
  are rebuilt when that timezone changes.

Invalid RRULEs, intervals below `MIN_RECURRENCE_INTERVAL_SECONDS` (default 5
minutes), and naive `at` values without an offset are rejected
(`ambiguous_datetime` / `validation_failed`).

## Reminder `version` vs occurrence state

`version` increments only for changes that affect what the phone should schedule
(title/body/mode/schedule/targets/policies). Marking an occurrence **fired**
does **not** bump `version`, so a local-schedule receipt still matches.

Due handling:

1. If the occurrence is still `scheduled` (or already `fired` from a displayed
   receipt), mark it fired without changing `version`.
2. Push `type=deliver` only to target devices that lack a current local schedule.
3. Escalation, quiet hours, expiry and dismiss (`type=dismiss`) are separate jobs.

## Actions

Snooze / skip / acknowledge apply to **one** occurrence. Repeating a terminal
action returns `duplicate`. Acting on cancelled or superseded rows returns
`rejected_stale`. Hitting the snooze limit or skipping without a reason returns
`rejected_invalid` with HTTP 200.

Quiet hours: `respect_quiet_hours` plus user preferences can defer or silence a
fire. Critical mode requires `alerts:critical` and `critical_alerts_enabled`.

## Sync

`GET /sync` returns a cursor (`users.sync_seq`, gap-free under the per-user write
lock). Devices replay offline actions at `POST /sync/actions` with
`client_action_id` for idempotency. Concurrent acks serialize: one `applied`,
the rest `duplicate`.
