# Next steps for AgentPing

Updated 2026-10-29. The backend is deployed with Auth0 OAuth and user accounts.
Codex is configured and its native browser login succeeded with refresh permission.
See [production evidence/costs](production.md) and [OAuth operations](production-oauth-operations.md).

1. Start a new Codex chat so it loads the `agentping` MCP server. Try
   `get_schedule_context` and `list_devices`. Reminders can be stored now; no
   production phone is paired, so phone notification/alarm delivery is not ready.
2. Use the [account portal](https://agentping-api-583092569828.asia-southeast1.run.app/account)
   to pair a phone, inspect grants, revoke an agent or explicitly allow a new login.
   Reapproval requires a fresh token; old tokens stay invalid. Account closure
   cancels active reminders and revokes access, but retains records/activity.
3. Have a second independent Google account complete login and verify isolation.
   Automated two-user tests pass, but this live acceptance gate remains open
   because only one Google account was available during deployment.
4. Move Android to the production endpoint/account flow, configure release signing
   and Firebase identity, then prove Codex → MCP → worker → FCM → the correct phone.
   Verify device receipt, display, sound, offline actions and reboot recovery separately.
5. Arrange regular encrypted off-machine backups and restore drills, monitor queue
   failures and measure billing before increasing usage.

Verified: 180 tests, real OAuth/PKCE, strict access-token validation, account portal,
MCP read/write/idempotency, rotating refresh/reconnect, revocation and explicit
reapproval, synthetic pairing/sync, real scheduled work, separate private worker
and restored backups. Production schema is `0004`.

Light-use estimate: INR 0–300/month before taxes, conditional on shared free
allowances; this is not a final invoice or a hard INR 500 cap. Both Cloud Run
services scale to zero, Neon remains Free and four application secret versions
are enabled. Auth0 is in its signup trial; no paid upgrade was performed.
The provider configuration readbacks and published pricing basis are documented
in [production.md](production.md#cost-and-verification-basis).
