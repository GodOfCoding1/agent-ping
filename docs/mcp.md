> Production OAuth is now implemented and undergoing live browser acceptance.
> Use [production OAuth operations](production-oauth-operations.md) for the Auth0
> client, native Codex configuration and current release status. Google CLI helper
> and private-IAM instructions below describe the earlier bootstrap only.

# MCP

For the **public multi-user Codex connection**, use
[production-oauth-operations.md](production-oauth-operations.md). Production uses
Auth0 browser OAuth with PKCE and standard bearer access tokens. Customer access
requires no Google Cloud role or IAM helper. The worker remains IAM-private.
The token examples below also support local development and explicitly issued
scoped agent credentials.

AgentPing exposes tools over the [official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)
using Streamable HTTP at `/mcp` (`stateless_http`, JSON responses). DNS-rebinding
protection is always on. Allowed hosts/origins come from `MCP_ALLOWED_HOSTS` /
`MCP_ALLOWED_ORIGINS`, or are derived from `PUBLIC_BASE_URL`.

Unauthenticated calls return 401 with `WWW-Authenticate` pointing at
`/.well-known/oauth-protected-resource/mcp`. Development auth is a bearer token
(owner or agent); production uses the managed OAuth flow.

## Tools

Required: `send_notification`, `create_reminder`, `update_reminder`,
`cancel_reminder`, `list_reminders`, `get_reminder`, `snooze_occurrence`,
`acknowledge_occurrence`, `skip_occurrence`, `list_devices`, `get_activity`.

Extra: `get_schedule_context` (server time, user timezone, quiet hours).

`send_notification` is for **now**. `create_reminder` is for **later or repeating**.
Times: ISO-8601. One-shot `at` needs a UTC offset; `local_datetime` is naive plus
an IANA `timezone`. Recurring uses `start_local` + RFC 5545 `rrule`.

Tool failures are MCP `isError` results with `[code] message`, not HTTP 4xx inside
an established session.

## HTTP client (Cursor, Inspector, custom)

Point the client at `{PUBLIC_BASE_URL}/mcp` with `Authorization: Bearer <token>`.

Inspector (do not print the inspector auth URL):

```sh
./dev inspector
```

Configure the Inspector's Streamable HTTP URL to `http://127.0.0.1:8000/mcp` and
paste the agent token.

## stdio adapter

For clients that can only spawn a local process:

```sh
export AGENTPING_MCP_URL=http://127.0.0.1:8000/mcp
export AGENTPING_TOKEN_FILE="$HOME/.config/agentping/agent.token"
./dev mcp-stdio
```

The adapter holds no database credentials. It forwards `list_tools` / `call_tool`
to the HTTP server.

Example Cursor MCP config (token via file, not inlined):

```json
{
  "mcpServers": {
    "agentping": {
      "command": "/path/to/agent-ping/backend/.venv/bin/agentping-mcp-stdio",
      "env": {
        "AGENTPING_MCP_URL": "http://127.0.0.1:8000/mcp",
        "AGENTPING_TOKEN_FILE": "/Users/you/.config/agentping/agent.token"
      }
    }
  }
}
```

## Local Android end-to-end probe

After pairing a phone through the Android app, use its device UUID:

```sh
./dev android-mcp-check --device-id <UUID>
./dev android-mcp-check --device-id <UUID> --alarm-seconds 300
```

The probe uses the official MCP client over the existing local Streamable HTTP
endpoint, creates/revokes a temporary scoped agent, sends once and replays the
same idempotency key. Its output separates MCP acceptance, provider acceptance,
local scheduling, device receipt and notification posting. It leaves the test
alert for interaction; cancel it through the app after verification. Exit 0
alone does not prove device delivery. No public endpoint or hosting is provisioned.
