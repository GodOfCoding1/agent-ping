# AgentPing implementation handoff

Updated 2026-10-30 after the production Android cutover and Play Store readiness
review. **The app works against production on the owner's phone. It is not yet
ready for public Play Store release.** Follow [next steps](next-steps.md) for the
ordered launch work and [verification](verification.md) for actual evidence.

## Current state

| Area | Verified state |
| --- | --- |
| Backend | Auth0/Google browser OAuth, independent account records, scoped REST/MCP, refresh/reconnect and grant revocation/reapproval |
| Deployment | Cloud Run API `agentping-api-00008-xs9`, IAM-private worker `agentping-worker-00004-btl`, Neon schema `0004`; request billing, min 0/max 1 each |
| Production origin | `https://agentping-api-583092569828.asia-southeast1.run.app` |
| Account flow | Browser account portal creates one-time pairing codes; Android stores its own Keystore-encrypted device credential |
| Production Android | Signed/minified release 0.2.0, versionCode 2, `com.errorfactory.agentping`, installed as **AgentPing** on Samsung SM-S926B |
| Phone at cutover | Paired, primary, valid FID, fresh sync, notifications/exact alarms/full-screen access allowed; battery optimization active |
| Delivery | Real Codex MCP → API → event-triggered worker → FCM → release phone returned provider acceptance, device receipt and display evidence |
| Store status | No AAB built/verified, Console eligibility inspected, Play App Signing enrollment verified, or Play upload/publication performed in this work |

Existing production deployment was authorized in earlier work. The old local-only
handoff and deferred-OIDC statements are superseded. This does not make production
a general write sandbox or authorize a public launch. The user's latest request
was to update handoff/next-steps documentation.

## Changes completed in this chat

- Release has the fixed production HTTPS endpoint. Debug retains localhost and
  package `com.errorfactory.agentping.debug`, labelled **AgentPing Dev**.
- Pairing opens the account portal and explains Google/browser login. Successful
  pairing starts Firebase registration and queues synchronization. Settings no
  longer says production authentication is deferred.
- `BackendScope` binds credential preferences, installation IDs, Room replicas
  and pending actions to the full backend URL. Legacy unscoped local files are
  retained and never imported into production. Android separately isolates the
  debug and release packages.
- Added private local release signing, `./dev android-release` and
  `./dev android-production-run <serial>`. Runtime use needs neither this Mac,
  ADB reverse nor a local backend.
- The first push to the development APK failed with FCM `INVALID_ARGUMENT`:
  production restricts delivery to `com.errorfactory.agentping`, not `.debug`.
  The correct release package fixed delivery without weakening that restriction
  or redeploying the backend. The failed probe was cancelled. Final active-device
  readback contained the release phone as primary.
- The user paired the release through their Google account and enabled
  notifications. Exact alarm access was subsequently verified allowed.
- Added backend identity/credential/replica isolation regression tests. The optional
  hosted health check now uses `/health`; Cloud Run returned 404 for `/healthz`.

The successful notification at 09:45:33 Asia/Kolkata had provider acceptance at
`04:15:33.521990Z`, device receipt at `04:15:33.972803Z` and display at
`04:15:33.997927Z`. A displayed receipt proves the app posted a notification;
physical sound/user observation was not independently verified in that run.

## Architecture and implemented features

Python 3.13/FastAPI, official Python MCP SDK, SQLAlchemy 2/psycopg 3/Alembic,
PostgreSQL durable work, FCM and Kotlin/Compose Android remain the architecture.
Cloud Tasks wakes the private worker; hourly recovery avoids continuous polling.
Database-backed per-account quotas already exist; assess them rather than
incorrectly describing all public quota controls as unimplemented.

| Slice | Implementation |
| --- | --- |
| Pairing/delivery | One-time code, Keystore AES-GCM credential, FID registration/refresh, FCM data handling, channels and durable receipts |
| Offline replica | Transactional Room/cursor, incremental/full sync, local action overlays, idempotent replay, WorkManager retry and six-hour reconciliation |
| Alarms | Server-expanded once/RRULE schedules; seven-day/64-operation horizon; snooze/ack/dismiss; bounded ringing; full-screen/heads-up fallback and recovery receivers |
| Policies | Four alert modes, bounded escalation, critical opt-in, quiet hours, primary/target selection, occurrence-version/terminal reconciliation and lock-screen privacy |
| UI | Today, Reminders/editor/details, Activity, Devices and Settings with cached/offline/error states |
| Watch boundary | Notification mirroring/actions only; earlier watch delivery failed and further testing was explicitly deferred; no Wear OS app |

Android sources: `android/app/src/main/kotlin/com/errorfactory/agentping/`.
`data/` owns Retrofit, secure storage, Room and reconciliation; AlarmScheduler,
receivers, Alerts and RingingService handle platform delivery. DataStore holds
local theme/onboarding preferences. Shared backend services preserve stale-receipt,
terminal-state, targeting, retry and quiet-hour guards. Historical Android changes
needed no migration; subsequent OAuth deployment brought production to `0004`.

## Verification baseline

| Check | Actual result and scope |
| --- | --- |
| Prior backend OAuth baseline | 180 tests, Ruff/format/mypy pass on 2026-10-29; not rerun for this Android-only cutover |
| Host/import/DB/Firebase probes | `doctor`, `imports`, `db-check`, `firebase-check` exit 0; guarded dev/test TLS and FCM validate-only are separate from device delivery |
| Debug | `./dev android-check` exit 0; 7 JVM tests; lint 0 errors |
| Release | `./dev android-release` exit 0; minified signed APK, lint 0 errors/29 advisories; APK signature v2 verified |
| Emulator | 12 instrumentation tests, 0 failures/errors/skips; production URL override, health/alarm flags; isolation, HTTPS health and offline local ringing/snooze/dismiss |
| Physical release phone | Install, browser pairing, FID, primary status, sync, permissions and actual production immediate push receipt/display pass |
| Still unproved by cutover | Physical production scheduled alarm sound, production offline actions/reboot, second independent live user, multi-OEM daily reliability and Play-generated APK behavior |

Earlier development evidence includes physical audible local alarms, offline
dismissal replay, emulator reboot/Doze and primary handoff. Keep those labelled as
local/emulator results, not completed production release acceptance. The second
independent live-user gate remains open despite automated isolation tests.

```sh
./dev android-setup
./dev android-check
./dev android-release
./dev android-production-run <phone-serial>
# Dedicated emulator only; alarmDeliveryCheck grants permissions and rings an alarm.
./dev android-test emulator-5554 \
  -Pagentping.backendBaseUrl=https://agentping-api-583092569828.asia-southeast1.run.app/ \
  -Pandroid.testInstrumentationRunnerArguments.backendHealthCheck=true \
  -Pandroid.testInstrumentationRunnerArguments.alarmDeliveryCheck=true
```

APK: `android/app/build/outputs/apk/release/app-release.apk`.
`android-run` is the local debug workflow. `android-mcp-check` is a local Compose
probe, not a hosted-delivery check. Connected tests can alter data/permissions;
use a dedicated explicit serial, not the owner's paired phone. In this environment,
emulator launch and tests needed the same shell command to survive CLI cleanup.

## First work for the next agent

1. Implement account and associated-data deletion plus Settings/web entry points
   and accurate retention disclosures. `services/accounts.py:close_account`
   currently cancels reminders/revokes access but retains records/activity;
   `status="deleted"` is not erasure. Test disposable accounts, never the owner's.
2. Establish actual Play Console eligibility and the signing/update strategy.
   Produce and validate an AAB; the current signed APK is not Play enrollment.
3. Close the production/two-user acceptance gaps, prepare reviewer access and
   declarations, and progress internal test → closed beta → approved public release
   using the gates in [next steps](next-steps.md).

The working browser portal/pairing flow does not require a replacement native
Android OAuth implementation. Custom DNS and a separate watch app are not launch
prerequisites for the currently implemented phone product.

## Signing material and protected configuration

- Release key: `.agentping/android-signing/agentping-release.jks`; ignored
  `android/signing.properties` references it and holds passwords. Preserve and
  privately back up both. No off-machine signing backup is verified. Never print,
  commit or silently replace them. Play app signing versus upload key choice is pending.
- Android builds must not load `.env.local` or embed owner/agent/database/admin
  credentials. Existing ignored Firebase client configs cover release/debug.
- Neon project `empty-surf-03967309`: dev `br-curly-wave-a1642imn`, test
  `br-divine-resonance-a143e0cp`, production `br-sweet-mouse-a167349g`.
  Use verified-host TLS probes and separate migrator/runtime roles. New production
  changes/destructive operations still require appropriate explicit authorization.
- Keep INR 500/month, Neon Free and request-driven scale-to-zero. The prior
  INR 0–300 light-use estimate is not a hard cap or arbitrary public-scale capacity.
  Verify Auth0 behavior after trial expiry and measure beta usage before expansion.
- Leave `automation-server` untouched. No Railway, continuous paid worker, paid
  minimum instances, broad IAM grants or service-account private keys.

## Known product limits

Force-stop requires reopening. Before-first-unlock alarms are not implemented.
The local horizon is bounded; offline devices cannot learn cancellation/primary
changes until reconnection. Floating timezone changes need server reconciliation.
Permissions, channels, DND and OEM battery controls can block/delay alerts.
Do not promise guaranteed delivery, DND bypass, exactly-once physical ringing or
unverified watch support. See [manual coverage](android-manual-tests.md).
