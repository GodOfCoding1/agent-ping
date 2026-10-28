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

## Limitations

- No Android app, so there is no local-schedule, exact-alarm, or on-device FCM test.
- Neon Free adds ~1 s per query; the full pytest suite is meant for local
  `test-db`, not the shared test branch.
- `PUSH_PROVIDER=log` records payloads and never talks to Google.
- MCP Inspector's per-launch auth URL must stay private.
- Production branch is out of scope.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `Database URL is not configured` / guard error | `.env.local` present; host is local or API-verified Neon dev/test; TLS on remote |
| `/readyz` 503 | `./dev migrate upgrade head`; runtime role can SELECT `alembic_version` |
| Worker idle while jobs pending | LISTEN needs the direct URL; or POST `/internal/worker/run` |
| 401 on `/mcp` | Bearer token; `PUBLIC_BASE_URL` host allowed |
| FCM `UNAUTHENTICATED` | `gcloud auth application-default login` then impersonation of `agentping-dev` |
| Image contains `.env` | Build from repo root so `.dockerignore` applies |
