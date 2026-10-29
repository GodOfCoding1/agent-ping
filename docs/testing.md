# Testing and manual checks

## Automated

Needs the compose test database:

```sh
./dev test-db
./dev test                 # uv run --project backend --locked pytest -c backend/pyproject.toml
./dev lint
```

Coverage (local compose Postgres): unit scheduling/domain/push, reminder API,
delivery/outbox, device sync/races, agent scopes, MCP over ASGI, health/diagnostics,
runtime-role audit/DDL denial, infra database-guard tests.

A displayed receipt marks the occurrence `fired` without bumping `version`. Due
handling still runs afterwards so expiry can be scheduled. Exhausted FCM retries
become `provider_failed` (job succeeds). Snooze-limit and missing skip reason are
HTTP 200 `rejected_invalid`.

## Live local stack

```sh
python3 infra/compose_env.py
docker compose up --build
./dev stack-smoke
```

`stack_smoke.py` pairs a throwaway device, sends a notification, waits for the
worker to mark `provider_accepted` (log provider), drives MCP over HTTP and the
stdio proxy, syncs/acks from the device token, then revokes the agent and device.
It never prints tokens.

## FCM

```sh
./dev fcm-check
```

`verdict=credentials_ok_probe_token_rejected_as_expected` means ADC + project
access work. It is **not** device delivery. A real FID can be passed with
`--token-file` / `--token-kind fid` once an Android client exists.

## Android application verification

See [Android README](../android/README.md), [manual matrix](android-manual-tests.md)
and the dated [verification record](verification.md). `./dev android-check` runs
APK assembly, five JVM policy tests and lint. Connected tests exercise Keystore,
Room/cursor rollback, durable actions, expired-action rules, channels, real
AlarmManager/ringing/deduplication/snooze/dismiss and all five UI destinations.
Use a dedicated emulator and the explicit alarmDeliveryCheck argument to grant
permissions; without it the actual alarm test may skip.

Neon Free adds latency; run the regression suite against local `test-db`.
`PUSH_PROVIDER=log` is suitable for stack-smoke but never contacts Google. Real
MCP → phone probes use `compose.fcm.yaml` and report provider acceptance separately
from device receipt/display. Production and the existing VM remain outside scope.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `Database URL is not configured` / guard error | `.env.local` present; host is local or API-verified Neon dev/test; TLS on remote |
| `/readyz` 503 | `./dev migrate upgrade head`; runtime role can SELECT `alembic_version` |
| Worker idle while jobs pending | LISTEN needs the direct URL; or POST `/internal/worker/run` |
| 401 on `/mcp` | Bearer token; `PUBLIC_BASE_URL` host allowed |
| FCM `UNAUTHENTICATED` | `gcloud auth application-default login` then impersonation of `agentping-dev` |
| Image contains `.env` | Build from repo root so `.dockerignore` applies |

## Android commands

```sh
./dev android-check
./dev android-test emulator-5554 \
  -Pandroid.testInstrumentationRunnerArguments.backendHealthCheck=true \
  -Pandroid.testInstrumentationRunnerArguments.alarmDeliveryCheck=true
```

The backend health test requires local Compose and ADB reverse. Keep actual test
counts, failures and skips in verification.md. Pairing codes can be generated with
`./dev compose exec -T api agentping-admin pairing-code`; Android stores only its
issued device credential, never the owner token.
