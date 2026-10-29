# Verification record

Final local application result (2026-10-29): **137 backend tests, 5 Android JVM
tests, 11 emulator instrumentation tests (no skips), and one separate real
Compose-editor/REST integration test pass**. Debug APK builds/lints and is installed
on the Samsung. Real FCM, physical audible local alarm, corrected offline reboot,
Doze notification, primary handoff and permission revocation have evidence below.
Watch delivery did not appear and further watch work was deferred by the user.
See [manual coverage](android-manual-tests.md) and [build instructions](../android/README.md).

The earlier sections are historical setup/slice results; the final checks below
supersede their counts and feature limitations.

## Historical Android development setup — 2026-10-29

Android now has a Gradle/Compose development shell, not application features.
SDK/JDK host configuration is reused; the official Android CLI skill was installed
with `android init`. The existing API 36 Google Play ARM64 emulator was used.
No physical phone was connected and no cloud infrastructure was provisioned.

| Command/check | Actual result |
| --- | --- |
| `./dev doctor` / `./dev imports` | Exit 0; Docker engine and host tools pass; OIDC deliberately pending |
| `./dev android-setup` (including repeat invocation) | Exit 0; SDK path ready; both ignored Firebase client configs validate |
| `./dev android-check` | Exit 0; debug APK builds; lint has 0 errors and 7 advisory version/target warnings |
| `./dev android-check -Pagentping.firebaseEnabled=false` | Exit 0; clean-checkout/CI build path works without Firebase configuration |
| `:app:testDebugUnitTest` | NO-SOURCE; no Android JVM unit tests claimed |
| `./dev android-test emulator-5554 -Pandroid.testInstrumentationRunnerArguments.backendHealthCheck=true` | Exit 0; **3 tests, 0 failures, 0 errors, 0 skipped**: Compose shell launch, Firebase client initialization, local backend `/readyz` over ADB reverse |
| `./dev test-db` / `./dev test -q` | Exit 0; **131 backend/setup tests passed** against disposable local PostgreSQL |
| `./dev stack-smoke` | Exit 0; local REST, MCP HTTP/stdio, worker log-provider dispatch, sync and acknowledgement |
| `./dev lint` and Ruff/format/mypy for `infra/android_env.py` | Exit 0 |
| `./dev db-check` | Exit 0; all guarded development/test TLS probes pass |
| `./dev firebase-check` | Exit 0; ADC and validate-only pass; **not device delivery** |
| Ignore/secret checks | SDK properties, Firebase variant files, APK/build output and Kotlin cache ignored; no actual local secret values in Git-eligible files |

The Android workflow is added but has not been executed on GitHub. Local equivalent
build tasks passed. API 36 and the compatible build/dependency baseline remain
pinned; lint's upgrade advisories are retained, not suppressed. The current SDK
repository did not offer `platforms/android-37`, so the template's attempted API 37
installation was not used. A first build fixed Gradle DSL and network-config lint
issues; subsequent builds passed.

Real FCM registration/delivery, device pairing, alarms, offline schedules, reboot,
Doze, multi-device and Wear OS remain future feature tests. Setup needs no phone
or new Firebase Console steps. Historical sections below describe earlier stages.


Backend application checks (2026-10-28) are in [testing.md](testing.md): local
pytest 131 passed, Compose stack smoke exit 0, Alembic `0002` on Neon dev/test,
FCM validate-only `credentials_ok_probe_token_rejected_as_expected` (not device
delivery). Setup-only results below are unchanged.

Budget update, 2026-10-28: the user set a deployment target of INR 500/month or less.
The earlier continuous Cloud Run worker-pool proposal was withdrawn. A fresh
inventory found no Cloud Run services, worker pools, jobs, Artifact Registry
repositories or Secret Manager secrets in the setup target. The historical checks
below remain valid; the hosting choice must be revised before deployment.

Executed 2026-10-28 on the local Apple Silicon Mac. Results below are actual
checks, not application implementation results. Redacted machine-readable probe
outputs are retained in ignored `.agentping/`.

## Passed checks

| Command / check | Actual result |
| --- | --- |
| `uv sync --project backend --python /opt/homebrew/bin/python3.13` | Created `.venv`, resolved lock and installed 76 packages |
| `./dev doctor` | Exit 0; host tools/paths and Docker engine pass; OIDC is intentionally pending |
| `./dev imports` | Exit 0; FastAPI, Uvicorn, official MCP, SQLAlchemy, psycopg, Alembic, Pydantic/settings, Firebase Admin, google-auth, pytest and HTTPX import |
| `uv run --project backend --locked ruff check --config backend/pyproject.toml infra` | All checks passed |
| `uv run --project backend --locked ruff format --check --config backend/pyproject.toml infra` | Two files already formatted |
| `uv run --project backend --locked mypy --config-file backend/pyproject.toml infra/smoke.py` | Success, no issues |
| `uv run --project backend --locked pytest -c backend/pyproject.toml -q` | **6 passed**, final run 0.08 s |
| `/bin/zsh -lic 'command -v sdkmanager; java -version; uv --version; psql --version'` | New interactive login shell discovers SDK/JDK/uv/libpq |
| `android sdk list` after sourcing host environment | Command-line Tools 23.0, platforms/build tools and API 36 Google Play ARM64 image detected |
| `emulator -accel-check` | Hypervisor.Framework available |
| `emulator -list-avds` | `AgentPing_API36` |
| `adb devices -l` | `emulator-5556` online; no physical phone present in this check |
| `adb -s emulator-5556 shell getprop sys.boot_completed` | `1` |
| `adb -s emulator-5556 shell getprop ro.build.version.sdk` | `36` |
| `adb -s emulator-5556 shell pm path com.google.android.gms` | Installed Google Play services APK found |
| `docker info --format '{{.ServerVersion}}'` | Engine 29.4.0 responds |
| `docker run --rm hello-world` | Successful native arm64 container execution, exit 0 |
| `docker run --rm --platform linux/amd64 python:3.13-slim python -c 'import platform; print(platform.system(), platform.machine()); assert platform.machine() == "x86_64"'` | Exit 0, `Linux x86_64`; AMD64 execution works on this ARM64 host |
| `neon projects list --org-id org-soft-field-09040394 --output json` | Expected project `empty-surf-03967309` accessible |
| `neon orgs list --output json` | Organization plan verified as Free |
| `neon branches list --project-id empty-surf-03967309 --output json` | Production plus isolated development and test branches verified |
| `./dev db-check` | All four runtime/migration URLs pass read-only `SELECT 1` with active client TLS and `verify-full` configuration |
| Migration/runtime privilege checks within `db-check` | Both migration roles can CREATE in public; both runtime roles cannot |
| `firebase login:list` and `firebase projects:list --json` | Authenticated; existing projects and selected project access verified |
| Android Firebase config validation | Both ignored configs match selected project and registered package IDs |
| `./dev firebase-check` | Impersonated ADC refresh and actual FCM `validate_only` request pass; `delivered=false` |
| `gcloud run worker-pools --help` | Cloud Run worker CLI loads successfully after runtime repair |
| `gcloud --configuration=agentping run services list --region=asia-southeast1` | Successful listing; no AgentPing service deployed |
| `gcloud --configuration=agentping run worker-pools list --region=asia-southeast1` | Successful listing; no worker pool deployed |
| `gcloud --configuration=agentping iam service-accounts keys list --iam-account=agentping-dev@personalvm-511016.iam.gserviceaccount.com --managed-by=user` | No user-managed keys |
| `gcloud --configuration=agentping storage buckets list` | No buckets returned; no unnecessary object storage created |
| `npx --yes @modelcontextprotocol/inspector@2.12.0 --version` | `2.12.0` |
| `MCP_AUTO_OPEN_ENABLED=false ./dev inspector` | Launched; UI at localhost port 6274 returned HTTP 200; auxiliary listener responded on 6275; Inspector stopped after check |
| Secret scan of Git-eligible files against actual local token/password/URL values | No matches; no credentials printed by scan |
| `git check-ignore` for env, Neon context, private diagnostics and Firebase configs | All ignored correctly |

The safety tests cover a production hostname mislabeled as development, a
production branch ID, disabled TLS, production environment selection, password
redaction, and test credentials incorrectly pointed at the development endpoint.
These are setup guard tests, not AgentPing business-logic tests.

## Issues found and resolved

- Java launcher stub had no runtime registration: reused the executable Android
  Studio JDK through a space-free private symlink.
- SDK tools were missing from PATH and duplicated in inconsistent locations:
  preserved originals outside SDK discovery and installed native stable tools 23.0.
- Current SDK package management uses `android sdk list`, not
  `android sdk list --installed`; legacy `sdkmanager` emits a deprecation notice.
- Gcloud Cloud Run worker commands initially failed importing grpc: created and
  enabled gcloud's own managed virtual environment, without system pip changes.
- Neon Free disallowed explicitly changing the suspend interval: branch creation
  succeeded using the plan-managed timeout. No paid plan was enabled.
- New managed migration roles did not initially have public-schema CREATE:
  rotated owner credentials on only the two new branches, granted the needed
  schema privilege, and verified separation from runtime privileges.
- The first TLS probe used server `pg_stat_ssl`, which reflects Neon's internal
  proxy connection: corrected the probe to inspect psycopg/libpq client TLS and
  reran all four connections successfully.
- Initial lint/type findings and pytest loop-scope warning were fixed; final
  format/lint/type/test commands pass.
- The first Neon browser flow expired at its fixed 60-second timeout. A fresh
  flow succeeded and the credential is stored in OS Keychain.
- Firebase initially selected a remote-code flow in the agent environment:
  `firebase login --interactive` completed browser/localhost authentication.

Upstream npm dependency deprecation notices appeared during installation of the
official CLIs. No force-upgrades or unsupported dependency rewrites were applied.

## Not executed during the original setup (historical, 2026-10-28)

- Application migrations or creation of AgentPing domain tables.
- Android Gradle project initialization, APK build, Android unit/instrumentation
  tests, or APK installation. No Android application exists yet.
- Device registration, FCM token refresh/device receipt, actual notification
  delivery, alarm ringing, Doze/reboot recovery, multi-device or Wear OS tests.
- Full authenticated remote MCP server testing; no server implementation exists.
- API/worker deployment, public ingress, container publishing, Secret Manager
  secret versions or production credential provisioning.
- Production database mutation or modification of the existing VM.

## Remaining prerequisites

None block beginning application implementation locally. Managed OAuth/OIDC is
deferred by the user's explicit choice to use a local development token. Configure
it before any production exposure. A physical phone is needed later for meaningful
FCM, notification/alarm, offline, reboot and OEM power-management validation.
GitHub CLI login/remote and CI can be configured once the repository destination
is selected. Deployment requires a separately approved cost estimate.

The emulator and Docker engine are running after setup. Inspector was shut down.
Approximately 25 GiB disk space remained after installing the selected tools/image.

## Changed files and external configuration

Repository: root README, AGENTS.md, .gitignore, .dockerignore, .env.example and `dev`; backend
Python pin, pyproject and uv.lock; `infra/smoke.py` plus its safety tests; Android
README; environment/configuration/cloud/verification docs; official Neon skills
and `skills-lock.json`.

Ignored local files: `.env.local`, `.neon`, `.agentping/` probes/downloads/context,
`backend/.venv`, Android Firebase configs. User-managed configuration outside the
repo: `~/.config/agentping/env.sh` and backups, additions to `.zshrc`/`.zprofile`,
JDK symlink, SDK tools/image/AVD, gcloud named configuration and impersonated ADC,
Neon Keychain login, Firebase CLI login. Cloud resources and IAM changes are listed
in `google-cloud.md`; Neon changes are limited to the new development/test roots,
their dedicated roles, grants and owner-password isolation.

## Android slice A — 2026-10-29

Independently reran the baseline: doctor/imports, guarded db-check and Firebase
validate-only exit 0; 131 backend tests and 3 emulator environment tests passed.
After implementation: 132 backend tests, backend Ruff/format/mypy and Android
assemble/lint pass. Six emulator instrumentation tests pass (environment plus
Keystore credential encryption/reopen, durable dedupe/receipt queue, channels).

Samsung SM-S926B connected as `RZCXA1Y6HZZ`, app installed with `./dev android-run`.
Paired through real UI using a one-time code, notification permission granted,
FID registered using onRegistered. Local Compose worker uses existing ADC via
compose.fcm.yaml. A real send produced provider=fcm acceptance, device_received_at
and displayed_at independently; Android NotificationManager held the matching
notification. No cloud resources or Firebase Console changes were needed.
Displayed means the app posted a permitted notification, not proof the user read it.
The CLI layout helper failed on the phone; ADB uiautomator inspection worked.

A backend regression now prevents stale-version receipts from modifying newer
snoozes and rejects early/terminal display claims. No alarm delivery is claimed yet.

## Android slices B/C — 2026-10-29

B: 133 backend tests and backend lint/type checks pass; Android build/lint and
8 device tests pass, including cursor rollback and offline action overlays.
C: 4 JVM policy tests pass. All 9 emulator tests pass with no skips when explicitly
run with `-Pandroid.testInstrumentationRunnerArguments.alarmDeliveryCheck=true`.
That test grants notification/exact permissions on the specified test device and
executes a real AlarmManager alarm, ringing service, dedupe, offline snooze and dismiss.
The earlier default-permission run skipped the alarm test; it is not counted as
alarm verification. C build and lint pass.

Physical Samsung: synchronized a one-time alarm, confirmed scheduled_locally
receipt, removed ADB reverse to disconnect the local API, and backgrounded the app.
At due time Android reported RingingService foreground and the user explicitly
confirmed audible ringing. ADB reverse restored afterward. This proves local alarm
delivery without the API; the phone's wider internet connectivity was unchanged.
Full airplane-mode, actual reboot, Doze, two-device and watch checks remain pending.

## Reboot failure found and corrected — 2026-10-29

The first actual offline reboot exercise posted the notification but crashed when
Android rejected `mediaPlayback` promotion from a process classified as boot-started.
A transient service record was insufficient evidence of successful ringing. This
run is a failure, not a reboot pass. The implementation now selects Android's
`systemExempted` foreground type only when exact-alarm access is granted (an
explicit documented eligibility condition), with mediaPlayback fallback otherwise,
and catches promotion denial inside the service to retain the notification
without crashing. No permissions or OS safety controls were bypassed.

The replacement run synchronized a new MCP alarm, removed ADB reverse, rebooted,
unlocked without opening AgentPing, disabled emulator Wi-Fi/mobile data, and
observed boot-reconstructed AlarmManager work. At the due time RingingService was
foreground, MediaPlayer showed the AgentPing package with looping=1/paused=0,
and AndroidRuntime contained no app crash. Notification acknowledgement was
pressed while offline, then networking/ADB reverse were restored for replay.
This is actual emulator reboot/offline playback evidence. Physical audible alarm
confirmation remains the separate Samsung result above.

Forcing deep idle with this imminent setAlarmClock alarm was refused by Android
(`stopped at INACTIVE`); this is not counted as a Doze pass. A separate persistent
exact-alarm Doze exercise is required. The earlier boot test's premature forced
idle delayed boot broadcast processing; the corrected sequence waits for actual
reconstruction before attempting idle.

## Current application verification — 2026-10-29

| Check | Actual result |
| --- | --- |
| `./dev test -q` | Exit 0, **137 backend tests passed** |
| `./dev lint` | Exit 0, Ruff/format/mypy pass |
| `./dev android-check` | Exit 0, debug APK + **5 JVM tests** + lint; advisory dependency/target warnings remain |
| Firebase-free `android-check -Pagentping.firebaseEnabled=false` | Exit 0; configured build restored afterward |
| Connected emulator suite with health/alarm flags | Exit 0, **11 tests, 0 failures, 0 skips** after the reboot foreground-type repair |
| Local stack smoke (log provider) | Exit 0: REST, auth failure, worker, MCP HTTP/stdio, sync/action and cleanup; dead jobs 0 |
| Final configured phone MCP probe | Accepted + idempotent replay; FCM accepted at 09:56:16.962 UTC, device received at 09:56:17.383, notification posted at 09:56:17.414 |
| Offline reboot acknowledgement replay | Canonical acknowledged; device displayed_at recorded; push_attempts=0 |
| Source secret scan | 182 Git-eligible files checked; no actual local token/password matches |
| Added MCP-device probe lint/type checks | Ruff and mypy pass |

The final UI was visually inspected on the Samsung. Connected tests found and
repaired a Firebase registration call on the main thread and two test selectors
that confused cached content/search fields. Registration now runs in the app IO
scope. A stack-smoke attempt immediately after API recreation hit a readiness
race; waiting for /readyz and using its expected log provider produced the passing
run above. The real phone tests separately used FCM. Existing untouched infra
files have format differences under the backend's 100-column config; backend
format and the added probe's format checks pass.

Watch test: phone notification posted, but it did not appear on the paired watch.
The user explicitly requested skipping further watch verification. Bridge identity
and notification actions are implemented; actual watch delivery is not claimed.

Doze exercise: a separate persistent reminder used setExactAndAllowWhileIdle.
Its local schedule was acknowledged before disconnecting Wi-Fi/mobile data and
ADB reverse. `dumpsys deviceidle force-idle` entered deep IDLE; after the due time
NotificationManager contained “AgentPing Doze exact check” while mState remained
IDLE. Network, reverse mapping and battery/idle overrides were restored afterward.
This verifies a local exact notification in Doze; it does not erase Android's
allow-while-idle quotas or imply every OEM behaves identically.

Live multi-device policy: an alarm first had a precise local receipt from the
Samsung. After switching primary to the emulator, the Samsung cleared its local
schedule and only the emulator retained scheduled_locally. Exact access was then
revoked on that emulator: capabilities reported false and its precise receipt
cleared. Access was restored, test reminders cancelled and the Samsung restored
as primary. This verifies real phone+emulator handoff and permission reconciliation;
it is not a simultaneous test on two physical phones.

The optional real editor journey passed: **1 device test**, using an already-paired
emulator's own device credential, created a reminder by filling and saving the
Compose form, observed it in the real server-synchronized list, and cancelled it
in cleanup. This exercised the actual Retrofit/REST path, not a mock.

Two final backend regressions recheck current critical opt-in and primary targets
before retrying an already-queued push. The first versions of those tests counted
sync hints as delivery attempts; narrowing assertions to deliver messages produced
**137 passing backend tests**. Pending retries can no longer bypass an opt-out or
send to a former primary solely because it was selected before the retry.


Final post-repair commands: `android-check` exit 0; connected suite with health and
alarm flags exit 0 (**11 passed, no skips**, 1m 7s); `android-run RZCXA1Y6HZZ`
exit 0, final APK installed. Backend regression **137 passed**, lint/type exit 0.
Real editor integration separately **1 passed**. New probe Ruff/format/mypy and
`git diff --check` pass. Generated test reminders were cancelled, temporary agents
revoked, stale emulator registrations removed, and Samsung restored as the only
active/primary device. Emulator battery/network/idle overrides were restored.
The local Compose stack is running with the FCM overlay; production and VM untouched.

Remaining manual coverage: watch delivery/actions (explicitly deferred), two physical
phones ringing together, OEM long-idle/battery variations, physical phone reboot,
large-font/TalkBack audit, and prolonged horizon exhaustion/force-stop behavior.
These are documented limitations/checks, not invented passes. No managed OIDC or
production deployment is claimed.
