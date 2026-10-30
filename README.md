# AgentPing

MCP-controlled notifications, reminders and alarms for the user's Android phones.

The **backend** (FastAPI, Streamable HTTP MCP, PostgreSQL outbox worker, FCM) is
implemented and runs locally. The **Android app** implements pairing, Firebase
notifications, offline sync/actions, local alarms and Material 3 reminder management. See [Android development](android/README.md). Production
backend hosting uses Cloud Run with Auth0 OAuth, independent user accounts and a
separate IAM-private worker. Native Codex login, live refresh, MCP tools, portal access and revocation checks
have succeeded; a second live user is still needed to close the public launch gate.
The signed Android release uses that production backend and browser account pairing.
Local release signing is configured; store publication remains deferred. See [OAuth operations](docs/production-oauth-operations.md),
[next steps](docs/next-steps.md) and [production cost evidence](docs/production.md).
Deployment budget: INR 500/month or less.

- [API](docs/api.md)
- [MCP](docs/mcp.md)
- [Next steps: Codex connection and production Android](docs/next-steps.md)
- [Production OAuth and multi-user launch plan](docs/production-oauth-plan.md)
- [Scheduling](docs/scheduling.md)
- [Reliability / outbox](docs/reliability.md)
- [Infrastructure](docs/infrastructure.md)
- [Testing](docs/testing.md)
- [Android manual verification matrix](docs/android-manual-tests.md)
- Setup inventory: [environment](docs/environment.md), [configuration](docs/configuration.md),
  [Google Cloud](docs/google-cloud.md), [verification](docs/verification.md)

## Local stack (Docker Compose)

PostgreSQL, migrations, API and worker on the loopback interface:

```sh
python3 infra/compose_env.py          # once; writes ignored .env.compose*
docker compose up --build
./dev stack-smoke                     # REST + worker + MCP HTTP + stdio proxy
```

API: `http://127.0.0.1:8000`. Docs: `/docs` (disabled when `APP_ENV=production`).
The development bearer token lives in `.env.compose` / `.env.local`; do not print it.

Optional real FCM from the worker (impersonated ADC, no service-account key):

```sh
docker compose -f compose.yaml -f compose.fcm.yaml up --build
```

That overlay only proves provider acceptance. It is not device delivery.

## Commands

Run from the repository root. `./dev` loads host SDK paths and `.env.local` via uv.

```sh
./dev doctor              # host tools
./dev imports             # Python imports
./dev db-check            # TLS probe of verified Neon dev/test hosts
./dev firebase-check      # FCM validate-only (not device delivery)
./dev test-db             # start the isolated pytest database
./dev test                # pytest (needs test-db)
./dev lint
./dev migrate upgrade head
./dev api                 # uvicorn on API_HOST/API_PORT
./dev worker              # durable outbox worker
./dev worker --once
./dev admin create-agent --name cursor --output ~/.config/agentping/agent.token
./dev fcm-check           # validate-only; probe-token rejection means credentials work
./dev inspector           # MCP Inspector 2.12.0
./dev mcp-stdio           # stdio adapter; needs AGENTPING_MCP_URL and AGENTPING_TOKEN
```

Direct equivalents:

```sh
uv sync --project backend --locked
uv run --project backend --locked pytest -c backend/pyproject.toml -q
uv run --project backend --locked ruff check --config backend/pyproject.toml backend infra
uv run --project backend --locked mypy --config-file backend/pyproject.toml backend/src backend/tests
```

## Android development

```sh
./dev android-setup
./dev android-check
./dev android-emulator
adb devices -l
./dev android-run <serial>             # local development
./dev android-production-run <serial>  # signed production APK
./dev android-test <serial>
```

See [android/README.md](android/README.md) for Firebase configuration, backend
connectivity, physical phones, reports and the exact verified scope.

## Authentication (development)

`AUTH_MODE=development` accepts `DEV_AUTH_TOKEN` as the account owner. Create
agent tokens (`apa_…`) and device tokens (`apd_…`) through the API or
`agentping-admin`. Agents cannot change settings, manage agents, or register
devices. Critical alerts need `alerts:critical` and the user's opt-in.

Production OIDC uses Auth0 with separate MCP/account audiences and explicit delegated scopes.
The account portal handles browser login, pairing and revocation.

## MCP

Streamable HTTP at `/mcp` (JSON responses, DNS-rebinding protection). Clients that
only speak stdio should launch `agentping-mcp-stdio`. See [docs/mcp.md](docs/mcp.md).

## Deferred release work

- Android store publication
- Optional custom DNS (the backend already has managed HTTPS)
- Production scheduled alarm sound, offline actions and reboot verification
- Scheduled off-machine backups and longer operational observation
