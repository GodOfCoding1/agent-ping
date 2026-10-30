# Next steps for AgentPing

Updated 2026-10-30. The backend is deployed with Auth0 OAuth and user accounts.
Codex is configured and its native browser login succeeded with refresh permission.
See [production evidence/costs](production.md) and [OAuth operations](production-oauth-operations.md).

1. Codex MCP and the signed Samsung production app are connected. A live test
   passed worker/FCM delivery, device receipt and display on 2026-10-30.
   Use `get_schedule_context`, `list_devices` and the notification/reminder tools.
2. Use the [account portal](https://agentping-api-583092569828.asia-southeast1.run.app/account)
   to pair a phone, inspect grants, revoke an agent or explicitly allow a new login.
   Reapproval requires a fresh token; old tokens stay invalid. Account closure
   cancels active reminders and revokes access, but retains records/activity.
3. Have a second independent Google account complete login and verify isolation.
   Automated two-user tests pass, but this live acceptance gate remains open
   because only one Google account was available during deployment.
4. The signed Android release uses production and browser account pairing.
   Install updates with `./dev android-production-run <serial>`. Local release
   signing is supported; preserve the private key for future updates. Store
   publication remains deferred. Physical scheduled alarm sound, offline actions
   and reboot recovery against production still need separate verification.
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
