# AgentPing implementation handoff

Current update, 2026-10-29: the user subsequently authorized production backend
deployment with Google Cloud IAM first. The private backend is deployed and live
REST/MCP/scheduled-dispatch verification passed. See [production.md](production.md)
for the deployed state, access and cost assumptions. Android release remains
deferred. The development-only scope statements below describe the earlier phase.

Full Android implementation and supporting backend fixes are authorized. Work is
**development-only**: local Docker Compose API/PostgreSQL/worker, existing Firebase
and USB/emulator. No hosting/deployment automation, paid resources, production
writes or automation-server changes are authorized. OIDC remains deferred.

## Implemented functional slices

| Slice | Implementation |
| --- | --- |
| A | One-time-code pairing UI; Android Keystore AES-GCM device credential; FID registration/refresh; real immediate FCM handling; channels and durable receipts |
| B | Transactional Room replica + cursor; full/incremental sync; pending local action overlays; idempotent offline replay; WorkManager retry and six-hour reconciliation; shared event suppression |
| C | Server-expanded once/RRULE occurrences; bounded seven-day/64-operation AlarmManager horizon; offline snooze/ack/dismiss; two-minute ringing service; full-screen/heads-up UI; reboot/package/time/permission recovery |
| D | Normal/persistent/alarm/critical channels; bounded sequential escalation; critical opt-in; quiet hours; permission diagnostics; primary/per-mode policies; terminal/version reconciliation and lock-screen privacy |
| E | Material 3 Today, searchable/filterable Reminders, recurrence/target/policy editor, detail/edit/pause/resume/cancel, paginated Activity, Devices controls and comprehensive Settings; loading/offline/auth/conflict states |
| F | Shared watch-compatible actions/stable notification identity and bridge boundary; automated regression, local MCP-device probe and full manual checklist. Watch delivery did not appear and user explicitly deferred further watch work |

Android implementation: `android/app/src/main/kotlin/com/errorfactory/agentping/`.
`data/` contains Retrofit, Keystore, Room, replica and repository. AlarmScheduler,
receivers, Alerts and RingingService own platform integration. Screens are split by
purpose. DataStore holds local theme/onboarding state; user policies remain on the
server. No owner, agent, database or Firebase-admin secret is embedded in Android.

## Backend fixes supporting Android

- Stale/early/terminal receipt guards prevent old pushes mutating a snooze/cancel.
- Current delivery evidence is scoped to occurrence version; an earlier display
  cannot suppress a later snooze or falsely report the new version displayed.
- Device/settings changes force a full target snapshot after a primary switch.
- Queued push retries recheck current critical opt-in and device targets.
- Scheduled quiet-hour deferral can deliver after the original late-grace window
  while still respecting expiry.

All reuse the existing shared services, schemas and state machine. No migration
or infrastructure replacement was required; schema remains Alembic `0002`.

## Verification and commands

Final results: 137 backend tests, 5 JVM policy tests, 11 emulator tests with no
skips, plus a separate real editor/REST journey pass. APK build/lint and backend
lint/type checks pass; final APK installed on Samsung. Offline reboot playback,
Doze, primary handoff and exact-access revocation were exercised on the emulator.

Read [verification.md](verification.md) for actual results, including failed/repaired
runs and final counts, and [android-manual-tests.md](android-manual-tests.md) for
coverage and remaining manual work. Physical Samsung pairing, FID, real FCM
notification and audibly confirmed local alarm worked. An offline dismissal
replayed to canonical skipped state; the alarm had zero push attempts.

```sh
./dev test-db
./dev test -q
./dev lint
./dev compose -f compose.fcm.yaml up -d --build
./dev android-check
./dev android-run <serial>
./dev android-test emulator-5554 \
  -Pandroid.testInstrumentationRunnerArguments.backendHealthCheck=true \
  -Pandroid.testInstrumentationRunnerArguments.alarmDeliveryCheck=true
./dev android-mcp-check --device-id <UUID>
```

`android/README.md` documents pairing against Compose, configuration, APK location,
reports, USB setup and reproducible offline/Doze checks. Android commands do not
load `.env.local`. Connected tests should use a dedicated emulator; they can alter
app data/permissions. The MCP probe leaves a test alert for interaction and revokes
its temporary agent. Probe exit 0 proves acceptance/replay; check delivery fields
before claiming device receipt/display.

## Boundaries and limitations

- Exact access/notifications/channel/OEM restrictions can block or delay alerts.
  Force-stop requires reopening. Before-first-unlock alarms are not implemented.
- The offline horizon is bounded; six-hour reconciliation is best effort and
  requires connectivity. Floating timezone changes need server reconciliation.
- A disconnected device cannot learn a remote cancellation/primary switch until
  it reconnects. Terminal state is never resurrected at the server.
- Watch appearance/action and daily OEM battery behavior remain manual platform
  work. There is no separate Wear app or independent watch schedule.
- Notification posting is not user observation. SQLite and Android posting cannot
  share an atomic commit; durable claims and active-notification identity checks
  limit the crash window, without promising exactly-once physical alerting.
- Managed OIDC, production HTTPS and deployment remain outside this phase.

## Protected existing configuration

Neon project `empty-surf-03967309`; dev `br-curly-wave-a1642imn`, test
`br-divine-resonance-a143e0cp`; production `br-sweet-mouse-a167349g` untouched.
Use guarded probes and separate runtime/migrator roles. Firebase/GCP development
project `personalvm-511016`, ADC impersonates `agentping-dev`; no private keys.
Leave `automation-server` untouched. Hosting budget remains INR 500/month and any
hosting selection needs a separately approved plan. Railway is excluded.
