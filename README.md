# AgentPing development environment

The development environment is configured. **AgentPing itself has not been
implemented.** Starting implementation requires a separate user instruction.

AgentPing will use Python/FastAPI + official MCP SDK, Neon PostgreSQL, a durable
worker, FCM, and Kotlin/Compose Android. Deployment must fit INR 500/month or less;
the always-on Cloud Run worker-pool proposal has been withdrawn. Evaluate
event-driven Cloud Run or existing-VM reuse before deployment. Railway is excluded.
See [environment inventory](docs/environment.md),
[verification results](docs/verification.md), [cloud setup](docs/google-cloud.md),
and [security/configuration](docs/configuration.md).

## Commands available now

Run from the repository root:

```sh
./dev doctor
./dev imports
./dev db-check
./dev firebase-check
uv sync --project backend --locked
uv run --project backend --locked pytest -c backend/pyproject.toml -q
uv run --project backend --locked ruff check --config backend/pyproject.toml infra
uv run --project backend --locked ruff format --check --config backend/pyproject.toml infra
uv run --project backend --locked mypy --config-file backend/pyproject.toml infra/smoke.py
./dev inspector --version
./dev inspector
```

`firebase-check` refreshes impersonated credentials and calls FCM with
`dry_run=True`. It never delivers a notification. `db-check` only opens verified
development/test hosts and runs read-only queries. Neither provisions resources.

The `dev` wrapper loads the secret-free host tooling fragment if present, then
uses uv with the locked backend environment and ignored `.env.local`.
Inspector is version-pinned and launched through npx; its per-launch local
authentication token must remain private.

## Android emulator

One Google Play ARM64 API 36 AVD, `AgentPing_API36`, is configured. A headless
instance was booted and tested on serial `emulator-5556` during setup.

```sh
. "$HOME/.config/agentping/env.sh"
adb devices -l
emulator -avd AgentPing_API36 -port 5556 -no-window -no-audio -no-snapshot-save
adb -s emulator-5556 shell getprop sys.boot_completed
adb -s emulator-5556 logcat
```

Do not start a second instance of the same AVD while it is running. For a visible
emulator, omit `-no-window`. A connected physical phone needs USB debugging and
authorization on the phone. Real notification/alarm behavior still needs testing
on a physical phone during implementation.

## Cloud access

```sh
gcloud --configuration=agentping config list
gcloud --configuration=agentping run services list --region=asia-southeast1
gcloud --configuration=agentping run worker-pools list --region=asia-southeast1
firebase projects:list
neon branches list --project-id empty-surf-03967309
```

The named gcloud configuration is separate from the default configuration.
Neon is locally linked to `agentping-dev`. Never run `neon env pull` against
production, and avoid bare env pulls that may request unrelated service tokens.
No production database URL has been retrieved or stored by setup.

## Next implementation stage

Once authorized, initialize the actual backend package, Gradle wrapper/Android
project, first Alembic migration, shared service layer, CI and backend image.
Use an async psycopg/SQLAlchemy engine for request handling and a synchronous
direct engine for Alembic. Use the configured runtime and migration credentials
for their intended purposes.

Only after those modules exist will API/worker start commands, application tests,
APK builds, device registration and end-to-end MCP/FCM checks become available.
No command above claims that those application components currently exist.
