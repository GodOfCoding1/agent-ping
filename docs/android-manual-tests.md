# Android regression and manual verification

Use a dedicated emulator for reboot, force-stop, Doze and permission changes.
Always substitute an explicit ADB serial; never run disruptive commands against
an unspecified device. Use local Compose with FCM overlay for real delivery.
The development owner API remains loopback-only; connect by ADB reverse.

## Automated regression

```sh
./dev test-db
./dev test -q
./dev lint
./dev android-check
./dev android-test emulator-5554 \
  -Pandroid.testInstrumentationRunnerArguments.backendHealthCheck=true \
  -Pandroid.testInstrumentationRunnerArguments.alarmDeliveryCheck=true
```

The last flag intentionally grants notification/exact permission and rings a
short-lived test alarm. Do not run connected tests on a phone whose app data you
want to retain. Read counts, failures and skips in the reports.

## Acceptance matrix (2026-10-29)

| Scenario | Evidence / current result |
| --- | --- |
| One-time code pairing and encrypted device identity | Physical Samsung UI paired; Keystore/reopen test passes |
| Immediate MCP notification | Real MCP accepted + idempotent replay; FCM phone notification posted; receipts checked separately |
| Foreground/background notification | Real phone posts in both states; Android notification-manager evidence |
| Alarm scheduled in advance | Real synchronized Samsung alarm had scheduled_locally receipt before due |
| Alarm without local API | Samsung rang audibly with ADB reverse removed; user confirmed; zero FCM delivery attempts |
| Full internet disconnected alarm | Passed on emulator; detailed evidence in verification.md |
| Snooze / acknowledgement / dismiss | Real AlarmManager test checks offline snooze/dismiss; phone offline dismiss reached canonical skipped state |
| Activity attribution and receipts | Backend/API regressions plus live phone actions; acceptance, receipt and display are separate columns |
| Cancellation / stale receipt race | Backend regressions; local alarm removal tested on dismiss; cross-device procedure below |
| Reboot recovery | Passed on emulator; detailed evidence in verification.md |
| Exact access denied / revoked | Live emulator revocation reported false capability and cleared precise receipt |
| Notification permission/channel denial | Permission diagnostics and no-display handling; OS settings remain authoritative |
| Two-device policy | Live Samsung→emulator handoff cleared the old schedule and left only the new primary scheduled; simultaneous two physical phones remain unverified |
| Wear OS bridge | Attempted on user's paired watch; notification did not appear. User explicitly deferred further watch tests |
| Doze | Passed on emulator; detailed evidence in verification.md |
| Backend unavailable and reconnect | Physical alarm/dismiss worked without API; replay reached server after reconnect |
| Repeated MCP idempotency key | Live MCP replay returned idempotent_replay=true; backend tests cover create/send duplicates |
| Unauthorized writes | 401 live stack smoke and scope/owner/device regression tests |
| Provider acceptance differs from receipt | Backend regression tests and separate live timestamps; acceptance alone never claims delivery |
| Recurrence/DST/monthly/leap/timezone cases | Backend tests; Android schedules supplied UTC instances, JVM tests cover local lateness/quiet-hour policy |
| Manual UI create/edit/pause/resume/cancel | Real Compose editor save through Retrofit/REST passed and cleanup cancelled the reminder; other edit/pause/resume/cancel APIs have backend regressions |
| Loading/offline/auth error UI | Cached-navigation test with invalid credential; server errors retain editor values |

## Reproducible device exercises

Pair through the app using a one-time code from:

```sh
./dev compose exec -T api agentping-admin pairing-code
./dev android-device <serial>
```

Use the Devices screen to copy/identify the correct UUID through the authenticated
local API. The probe never embeds an owner token in the APK:

```sh
./dev android-mcp-check --device-id <UUID>
./dev android-mcp-check --device-id <UUID> --alarm-seconds 300
```

Inspect the specific occurrence's `deliveries`: provider_accepted_at,
device_received_at, scheduled_locally_at, displayed_at and push_attempts. A displayed
receipt means a permitted notification was posted, not that a person read it.
For audible ringing, confirm sound on the device in addition to service state.

For an offline alarm, first confirm scheduled_locally, remove reverse, disable
networking **only on the test emulator**, then wait for the due time:

```sh
adb -s emulator-5554 reverse --remove tcp:8000
adb -s emulator-5554 shell svc wifi disable
adb -s emulator-5554 shell svc data disable
# Restore after observing the alarm and queueing an action:
adb -s emulator-5554 shell svc wifi enable
adb -s emulator-5554 shell svc data enable
./dev android-device emulator-5554
```

For reboot, synchronize an alarm far enough ahead, disconnect the emulator, run
`adb -s emulator-5554 reboot`, wait for sys.boot_completed=1 and unlock. Do not open
the app before checking the due alarm; BOOT_COMPLETED must reconstruct it. The app
uses credential-protected storage, so delivery before first unlock is not claimed.

For Doze, synchronize a future persistent exact reminder, background the app and run.
An imminent setAlarmClock alarm can prevent deep-idle entry; record mState rather
than treating a refused force-idle command as a pass:

```sh
adb -s emulator-5554 shell dumpsys battery unplug
adb -s emulator-5554 shell dumpsys deviceidle force-idle
# Observe actual due delivery, then restore:
adb -s emulator-5554 shell dumpsys deviceidle unforce
adb -s emulator-5554 shell dumpsys battery reset
```

For exact-alarm revocation use Settings → Apps → Special access → Alarms and
reminders, or `adb -s emulator-5554 shell appops set com.errorfactory.agentping.debug
SCHEDULE_EXACT_ALARM deny`. Android can kill the process and remove exact alarms.
Reopen the app, verify degraded diagnostics/unscheduled receipts, then restore
access and verify reconstruction. Denied access must never be called precise.

For multi-device reconciliation create an alarm targeting primary, switch primary
before due and synchronize both. Check that the old primary removes the operation
and only the new primary rings. Acknowledge on one device and check the other
dismisses; repeat with one offline. Stale offline actions must not resurrect a
cancelled occurrence. A device offline during a primary switch can still ring its
previously downloaded alarm; this is an explicit distributed-system limitation.

For manual editing create one-time/daily/weekly/monthly/custom reminders in the UI,
change title/time/targets, pause/resume, snooze one occurrence and cancel. Verify
revision conflict messaging by changing the same reminder from another client.
Test TalkBack, larger font scales and OEM battery restrictions before treating a
private debug build as ready for daily reliance. These are not claimed complete.
