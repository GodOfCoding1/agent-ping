# AgentPing Android

AgentPing is a native Material 3 application with one-time pairing, Keystore-backed
device credentials, Firebase FID registration, immediate notifications, a Room
replica and offline action queue, AlarmManager delivery, bounded escalation,
reboot reconstruction and five functional screens. The signed production APK
uses the live production HTTPS backend; debug builds use local Compose by default.
Browser account login issues a one-time pairing code;
the app stores only its own Keystore-encrypted device credential.

## Build and run

From the repository root, with the existing SDK and Android Studio JDK 21:

```sh
./dev android-setup
./dev android-release
adb devices -l
./dev android-production-run <serial>
```

Open `android/` in Android Studio. Gradle wrapper 9.4.1, AGP 9.2.0, Kotlin/Compose
2.3.20, compile/target API 36, min API 26 and Java 17 bytecode remain pinned.
AGP supplies built-in Kotlin; do not apply `org.jetbrains.kotlin.android`.
Dependencies are in `gradle/libs.versions.toml`: Room 2.8.5/KSP 2.3.10,
WorkManager 2.12.0, DataStore 1.3.0, Retrofit 3.0.0, Compose BOM 2026.03.01 and
Firebase BOM 35.1.0. Two Gradle workers / 2 GiB heap suit the existing Mac.

Production APK: `android/app/build/outputs/apk/release/app-release.apk`.
Development APK: `android/app/build/outputs/apk/debug/app-debug.apk`.
IDs: `com.errorfactory.agentping`, debug `com.errorfactory.agentping.debug`.
No owner credential or `.env.local` is read by Android build commands.

## Production pairing

`./dev android-production-run <serial>` installs the signed release as **AgentPing**,
package `com.errorfactory.agentping`. It uses production HTTPS directly and works
without this Mac or USB after installation. It can coexist with **AgentPing Dev**,
which has the separate `.debug` package and local data. Pair the production app
once and make it primary in Devices.

The production FCM sender restricts delivery to `com.errorfactory.agentping`.
A debug APK can call the production API with an explicit URL override, but it
cannot receive that sender's pushes because its package is different. Use the
release APK for production phone delivery.

Signing reads ignored `android/signing.properties`, with `storeFile`, `storePassword`,
`keyAlias` and `keyPassword` entries. `storeFile` resolves relative to `android/`.
This machine's private key is in `.agentping/android-signing/agentping-release.jks`;
keep it and the properties file private and backed up to sign future app updates.
No backend secret or `.env.local` is loaded. Without signing properties, Gradle
can build an unsigned release; the production install helper requires signing.
Store publication is outside this local phone installation workflow.

1. Tap **Open account & get pairing code**. Sign in to your account at
   [the production portal](https://agentping-api-583092569828.asia-southeast1.run.app/account)
   and create a pairing code. Return to the app and enter the code with a device name.
2. Allow notifications and enable exact alarm access in Settings → Device health.
   The production package needs its own permissions; future updates preserve them.
3. Pairing immediately starts Firebase registration and sync. The production device
   should appear in the portal and the connected MCP client's `list_devices`.

Credentials, installation IDs, pending actions and Room replicas are scoped by the
full backend URL. The old local-only files are retained but are never imported
into production; a local pairing cannot authorize a production account. Changing
the URL needs a separate pairing. Within one package, theme preferences and OS
permissions are shared; Android isolates the release and debug packages.

## Local development pairing and permissions

Build explicitly against Compose:

```sh
./dev compose-env
./dev compose -f compose.fcm.yaml up -d --build
./dev android-run <serial> -Pagentping.backendBaseUrl=http://127.0.0.1:8000/
```

1. Start the local Compose stack. Real FCM requires the overlay above and existing
   ADC impersonation; no Firebase service-account key is needed.
2. Generate a one-time code against **Compose**, not the host's Neon configuration:

   ```sh
   ./dev compose exec -T api agentping-admin pairing-code
   ```

   Enter that short-lived code and a device name in the app. Keep the code private.
   Never paste an owner/agent bearer token into Android. The backend issues a
   separate device credential, encrypted with an Android Keystore AES-GCM key.
3. Use the onboarding card to allow notifications. Firebase registers via
   `FirebaseMessaging.register()` / `onRegistered(FID)` and uploads `push_token_kind=fid`.
4. Settings → Device health → Manage exact alarm access enables precise alarms.
   Full-screen access is optional; heads-up alerts work without it. User channel
   sound, vibration, DND and battery settings remain authoritative.
5. Settings contains critical-alert opt-in, quiet hours, timezone, privacy,
   snooze defaults, agent scope overview and diagnostics.

Today supports local acknowledge/snooze/dismiss. Reminders supports search, state/
type/source filters, details, manual creation/editing, recurrence, explicit device
selection, pause/resume and cancellation. Activity loads server history with
filters/pagination. Devices supports naming, primary selection, per-mode switches
and removal. Cached schedules remain usable while disconnected; definition edits
and settings changes require the server and retain their form on an error.

## USB phone / emulator connectivity

Enable Developer options (tap Build number seven times), enable USB debugging,
connect by USB, unlock and approve this Mac. `adb devices -l` must say `device`.
Always provide a serial to `./dev android-run`, `android-device` and `android-test`.
Development helpers establish `adb reverse tcp:8000 tcp:8000` for the default
`http://127.0.0.1:8000/`; no LAN exposure is required. The production install helper
uses the fixed HTTPS endpoint and needs no reverse tunnel.

For emulator direct access use the same property for build/run/test:

```sh
./dev android-run emulator-5554 -Pagentping.backendBaseUrl=http://10.0.2.2:8000/
```

The URL must end in `/` and contain no credentials/query/fragment. Debug cleartext
is allowed only on localhost, 127.0.0.1 and 10.0.2.2. Remote endpoints require
HTTPS. Release uses the fixed production origin and disallows cleartext. Private
local release signing is supported; store publication is not configured.

## Tests

```sh
./dev android-check
./dev android-test <serial> -Pandroid.testInstrumentationRunnerArguments.backendHealthCheck=true
# Dedicated test emulator only: grants notification/exact access and exercises ringing.
./dev android-test emulator-5554 \
  -Pandroid.testInstrumentationRunnerArguments.backendHealthCheck=true \
  -Pandroid.testInstrumentationRunnerArguments.alarmDeliveryCheck=true
```

JVM tests cover backend identity isolation, lateness/expiry/horizon, terminal/target
policies, finite escalation, dedupe and quiet hours/DST. Instrumentation tests
cover backend credential/replica isolation, environment,
Keystore, Room persistence, atomic replica/cursor rollback, offline overlays,
real AlarmManager delivery/ringing/snooze/dismiss and screen navigation/editor.
To test production reachability on the dedicated emulator, also pass
`-Pagentping.backendBaseUrl=https://agentping-api-583092569828.asia-southeast1.run.app/`.
The health opt-in checks public `/health`; protected `/readyz` needs credentials.
Without permissions or the explicit alarm test flag, the alarm test may skip;
always inspect test counts. Connected tests install/remove the test app, so use a
dedicated emulator rather than a phone containing wanted app state.

Reports: `app/build/reports/{tests,androidTests,lint-results-debug.html}`.
Actual executed results and manual coverage: [verification](../docs/verification.md)
and [manual matrix](../docs/android-manual-tests.md).

Real MCP → Android probe (targets one paired device, leaves the alert for interaction):

```sh
./dev android-mcp-check --device-id <UUID>
./dev android-mcp-check --device-id <UUID> --alarm-seconds 300
```

It reads only local Compose credentials, revokes its temporary agent, checks an
idempotent replay, and reports provider/receipt/scheduling/display evidence separately.
Exit 0 means MCP acceptance and idempotent replay, **not** confirmed device display.

## Offline behavior and limits

The server expands RRULEs with IANA timezones and stable occurrence IDs. Android
schedules at most 64 operations within seven days from the downloaded horizon.
Sync, firing, reboot (after unlock), package/time/permission changes replenish it.
An exact alarm uses `setAlarmClock`; denied access falls back to inexact delivery
with diagnostics and keeps server fallback enabled. WorkManager handles sync and
retries, never exact timing. Ringing stops after at most two minutes or expiry.
An alarm respects its Android channel sound (including silence).

Local/push attempts share an occurrence/version/time/escalation key. One tagged
notification per occurrence and one foreground ringing notification avoid phone/
watch duplicates. Watch actions are ordinary Android notification PendingIntents;
Android does not reliably identify their remote origin. There is no Wear OS app.
Watch delivery was attempted and did not appear on the user's watch; further
watch testing was explicitly deferred.

Force-stop, denied/revoked permissions, OEM power restrictions, an exhausted
scheduling horizon or a disconnected phone can prevent alerts. Open the app after
force-stop. Floating timezone changes require server reconciliation; fixed UTC
instants already downloaded continue offline. A cancelled/acknowledged event on
another offline phone can still ring until reconciliation; the backend never
resurrects terminal state. Exactly-once notification posting cannot be made atomic
with SQLite across an OS/process failure; stable identities, durable claims and
active-notification checks narrow that crash window.

Firebase client configuration lives in ignored `config/firebase/{debug,release}`
and is copied by android-setup. To verify a clean/CI checkout without it:

```sh
./dev android-check -Pagentping.firebaseEnabled=false
```

That build cannot receive FCM. Rebuild with the existing config before device tests.

## References

- [Room releases](https://developer.android.com/jetpack/androidx/releases/room)
- [AlarmManager guidance](https://developer.android.com/develop/background-work/services/alarms)
- [Foreground-service types](https://developer.android.com/develop/background-work/services/fgs/service-types)
- [FCM registration and callbacks](https://firebase.google.com/docs/cloud-messaging/android/get-started)
- [FCM reception](https://firebase.google.com/docs/cloud-messaging/android/receive-messages)


Optional real editor integration on an already-paired dedicated emulator:

```sh
./dev android-test emulator-5554 \
  -Pandroid.testInstrumentationRunnerArguments.class=com.errorfactory.agentping.ApplicationUiTest \
  -Pandroid.testInstrumentationRunnerArguments.manualApiCheck=true
```

This creates a real short-future reminder through the Compose editor and cancels
it in cleanup. It uses only the installed device credential. Its passing result
is recorded separately from the regular eleven-test regression suite.
