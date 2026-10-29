# Scheduling

Occurrences are materialized in PostgreSQL. The Android app holds a local schedule for the current occurrence `version` and fire
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

## Android local delivery

Android consumes authoritative UTC occurrence instants from sync; it does not
expand RRULEs or add 24 hours. Room retains schedules across process death.
AlarmManager holds at most 64 upcoming operations within seven days. Real alarms
use setAlarmClock when exact access is granted; persistent/critical use exact
allow-while-idle; normal and denied-exact fallbacks are inexact. Only precise,
notification-capable schedules receive a `scheduled` receipt that suppresses
server fallback. Local and remote delivery use the same occurrence/version/time/
escalation dedupe key. Each occurrence has at most one pending local operation;
escalation replenishes sequentially and remains bounded by server policy.

Boot (after unlock), package replacement, time/timezone and exact-access broadcasts
reconstruct the horizon from Room before requesting network reconciliation.
Offline snooze/ack/dismiss update Room and AlarmManager before network replay.
A cancelled, superseded, stale-version or expired event cannot ring locally.
Ringing is a foreground service (systemExempted when exact-alarm access is granted; mediaPlayback fallback otherwise), bounded to two minutes; Android
background-start denial leaves a visible notification and a failure receipt.
Full-screen access is optional; heads-up notifications remain the fallback.
Force-stop, uninstall, revoked permissions and OEM power controls can prevent
alarms. Open the app after a force-stop to reconstruct schedules.
