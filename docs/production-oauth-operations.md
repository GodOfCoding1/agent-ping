# Production OAuth operations

Updated 2026-10-30. Native Codex login, live refresh, portal login and revocation
checks pass. The second-live-user acceptance gate remains open because only one
Google account was available. Do not call that gate complete until the second
independent live sign-in and isolation check is recorded.

## Configuration

- API/MCP origin: `https://agentping-api-583092569828.asia-southeast1.run.app`.
- MCP resource/audience: origin + `/mcp`; account API audience: origin + `/api/v1`.
- Auth0 issuer: `https://dev-olgiqoud72gt2u4h.us.auth0.com/`.
- Account client: `w3iqnc43C8VkDXxY2sCtxscSzyekKqFe`, confidential web client,
  exact callback at origin + `/auth/callback`. Client secret is in Secret Manager.
- Codex client: `tpc_pg83CRJCHvixrfMfhaGF1Z`, public native client, no secret,
  callback `http://127.0.0.1:8743/callback`, PKCE required.
- Auth0 APIs use RS256 RFC 9068 access tokens with 600-second lifetimes. The portal
  client explicitly signs ID tokens with RS256 as well. Resource
  parameter compatibility and issuer identification are enabled. Refresh tokens
  rotate with finite lifetimes. Client credentials and password grants are absent.
- Independent accounts use issuer + subject, never email matching. Owner permissions
  require a trusted first-party client and the account API audience. MCP access is
  always an agent with explicit delegated scopes and a separately revocable grant.
- Default agent scopes: `activity:read`, `devices:read`, `notifications:send`,
  `reminders:read`, `reminders:write`. Critical alerts are excluded by default.
- Separate worker: `https://agentping-worker-583092569828.asia-southeast1.run.app`,
  Google IAM plus a trigger secret. The API excludes its execution routes entirely.
- Both services use request billing, min 0/max 1, 1 CPU/512 MiB; no continuous worker.
  API identity `agentping-api` has no FCM permission. Worker identity `agentping-prod`
  retains FCM and scoped queue access. Hourly Scheduler targets the private worker.

Own Google OAuth credentials are configured in Auth0. No Auth0 social developer
keys are used for these clients. Google sign-in has an external audience and
In production status. Email/password login is disabled when own Google credentials
are configured;
adding it later requires a production-supported email provider, rather than Auth0's
built-in testing email service.

Auth0 is in its signup trial. Automatic MCP client onboarding uses standard Dynamic
Client Registration (DCR), user consent and PKCE. No paid upgrade or Early Access
dynamic CIMD feature is required. The existing explicit Codex client remains valid.

## Automatic MCP client connection

Add `https://agentping-api-583092569828.asia-southeast1.run.app/mcp` in a client
that supports OAuth discovery and DCR. Choose OAuth and automatic/dynamic client
registration; leave manually supplied client credentials empty. Sign in with the
same Google account used to pair the phone, then approve the requested permissions.
New applications register themselves; an administrator does not create a client
or a per-application grant for each one.

Auth0 discovery advertises `/oidc/register`. Tenant setting
`flags.enable_dynamic_client_registration` is true and
`client_id_metadata_document_supported` is false. Advertising CIMD without automatic
onboarding caused ChatGPT's `Unknown client: https://chatgpt.com/oauth/client.json`
failure. For a connection that cached the previous CIMD flow, select DCR or recreate
the failed MCP connection so it discovers the updated settings. Existing working
Codex connections do not need recreation.

One default Auth0 grant (`default_for: third_party_clients`, `subject_type: user`)
allows new third-party applications to request only the five MCP scopes above for
the `/mcp` audience. Users still sign in and consent. There is no third-party default
grant for the account API or machine-to-machine access. APIs retain per-app grant
enforcement, RS256 RFC 9068 access tokens, 600-second access-token lifetimes and
mandatory consent. The backend creates each user/client agent grant on its first
authenticated request, and still enforces local revocation and owner/client isolation.

Configure or inspect this policy without rerunning provider bootstrap:

```sh
uv run --project backend --locked python infra/auth0_onboarding.py status
uv run --project backend --locked python infra/auth0_onboarding.py configure
uv run --project backend --locked python infra/oauth_dcr_smoke.py
```

`configure` validates the API/consent boundaries before writing, installs the limited
default grant before opening registration, and confirms the result by reading it
back. It is idempotent and preserves existing explicit clients and grants. Each
pre-change snapshot is saved privately with a timestamp. The general
`auth0_setup.py` bootstrap also reapplies this policy so it does not restore the
broken CIMD advertisement.

The live smoke registers one reusable public verification client anonymously and
checks strict third-party policy, absence of manual client grants, public discovery
and progression to browser login. It does not complete browser consent, exchange
codes or call tools as a user. Real ChatGPT login/tool use is a separate acceptance
check. Verification clients and receipts are kept in ignored private storage.

Verified on 2026-10-30: the provider policy read-back and anonymous DCR smoke passed.
Both OAuth and OIDC discovery advertise DCR. The new public client receives rotating,
expiring refresh tokens (30-day maximum, 15-day idle lifetime), requires no token
endpoint client authentication, and loads Google browser login without a manual
client grant. The local suite passes 190 tests, including new-client grant isolation,
revocation, idempotent configuration and rejection of unsafe default grants. This
change only updates Auth0 configuration; it does not require an API redeployment.

Provider references: [Auth0 DCR](https://auth0.com/docs/get-started/applications/dynamic-client-registration),
[default third-party permissions](https://auth0.com/docs/get-started/applications/third-party-applications/configure-third-party-applications#default-permissions-for-all-third-party-applications),
[OpenAI client registration](https://developers.openai.com/plugins/build/auth#client-registration).

## Native Codex connection

The current machine's Codex config has a public OAuth client entry. On another
machine, add this to `~/.codex/config.toml` (IDs here are public):

```toml
[mcp_servers.agentping]
url = "https://agentping-api-583092569828.asia-southeast1.run.app/mcp"
scopes = ["offline_access", "activity:read", "devices:read", "notifications:send", "reminders:read", "reminders:write"]

[mcp_servers.agentping.oauth]
client_id = "tpc_pg83CRJCHvixrfMfhaGF1Z"
callback_url = "http://127.0.0.1:8743/callback"
callback_port = 8743
```

Run `codex mcp login agentping`, complete the browser sign-in and consent, then
start a new Codex chat. No Google Cloud role, gcloud helper or pasted token is part
of the customer flow. Keep port 8743 available during login. This configuration
matches the installed Codex version; older clients may need an update.

Portal HTML uses `Referrer-Policy: same-origin` so native browser form POSTs send
the origin required by the CSRF guard; middleware preserves that policy. OAuth
login/callback redirects retain `no-referrer`. Null and foreign origins remain rejected.

Use the account portal at origin + `/account` for browser login, phone pairing,
agent/device revocation and account closure. Revocation is checked on every new
request, including freshly refreshed JWTs. A revoked grant stays revoked until
the owner explicitly allows a new login.
Reapproval records a token-issuance cutoff so old JWTs remain invalid. Account
closure revokes credentials, unpairs devices and cancels active reminders; it is
not physical data erasure. Existing records/activity are retained, as disclosed in
`/privacy`. Do not claim an erasure policy that has not been implemented.

## Verification and deployment

- Local: 180 tests passed, including the final CLI bootstrap guard. Ruff, formatting and mypy passed.
- Fresh production dumps restored into disposable local databases at schemas
  `0002` and `0003` before the respective migrations. Scratch databases were removed.
  Production is now
  `0004`; runtime grants on all new tables were checked; DDL remains unavailable.
- Private deployment checks passed: PKCE redirect/cookie settings, protected-resource
  metadata/challenge, invalid/missing bearer rejection, API worker route absence,
  private-worker denial and authenticated worker execution, correct Scheduler and
  Cloud Tasks targets. These checks deliberately do not claim live browser OAuth.
- Public anonymous checks: health/account/metadata 200, protected REST 401, no worker
  route on the public API. The private worker still requires IAM.
- Native browser OAuth, tools, refresh/reconnect, owner portal, synthetic pairing/sync
  and live JWT revocation/reapproval now pass. A second independent live user's isolation
  is the remaining OAuth release gate.

Deployment scripts are explicit administrative operations:

```sh
uv run --project backend --locked python infra/auth0_setup.py
uv run --project backend --locked python infra/oauth_production.py backup
uv run --project backend --locked python infra/oauth_production.py migrate
uv run --project backend --locked python infra/oauth_production.py configure
uv run --project backend --locked python infra/production.py build
uv run --project backend --locked python infra/oauth_production.py worker
uv run --project backend --locked python infra/oauth_production.py api
uv run --project backend --locked python infra/oauth_production.py scheduler
```

`infra/oauth_smoke.py` checks the private pre-publication deployment; it intentionally
fails its private-IAM assertions after transport is opened. Its receipt distinguishes
infrastructure checks from the live OAuth release gate. `publish` opens the HTTPS
transport for live callbacks after boundary checks; it does not declare release
acceptance complete. The old `production_smoke.py` checks the historical Google CLI
bootstrap deployment and is not valid for the Auth0 deployment.

Private configuration/receipts/backups stay in ignored `.agentping/production/`
with owner-only permissions. Never paste provider credentials or token files into
docs. The original VM and development Firebase/Neon environments are unchanged.

Rollback must preserve the Auth0 identity/account schema. Prefer reverting to an
Auth0-compatible revision. Do not restore the old Google CLI singleton as public
customer authentication. If a security failure is found, remove the API's
`allUsers` invoker grant first to close transport; keep the worker private. Never
blindly downgrade account migrations, merge users by email or overwrite live production data.

## Cost evidence

The prior light-use estimate remains INR 0–300/month before taxes, within the
INR 500 target only while usage and shared free allowances permit it. This is not
an invoice or hard cap. Read back both service configurations, queue/Scheduler
settings, active secret-version counts and Neon plan; compare them with the linked
provider pricing in [production.md](production.md#cost-and-verification-basis).
Auth0 advertises a [Free tier](https://auth0.com/pricing) up to 25,000 external
active users. Its current trial is not proof of a paid subscription or that paid
trial features will remain available. No upgrade was authorized or performed.
