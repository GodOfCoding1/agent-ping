# Environment readiness verification

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

## Deliberately not executed

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
