# Configuration and secrets

## Local development

Tracked `.env.example` contains names and non-secret identifiers. Ignored
`.env.local` is mode 0600 and contains unique development credentials. `./dev`
loads it through uv, not through shell evaluation.

| Variable | Purpose |
| --- | --- |
| APP_ENV | `development` for local work; probes reject production |
| DATABASE_URL | Development pooled endpoint, `agentping_runtime` |
| MIGRATION_DATABASE_URL | Development direct endpoint, `agentping_migrator` |
| TEST_DATABASE_URL | Test pooled endpoint, independently generated runtime credential |
| TEST_MIGRATION_DATABASE_URL | Test direct endpoint, separate migration credential |
| NEON_PROJECT_ID / NEON_BRANCH | Existing Neon project / `agentping-dev` |
| GOOGLE_CLOUD_PROJECT | `personalvm-511016` |
| CLOUDSDK_ACTIVE_CONFIG_NAME | Project-local selection of named `agentping` configuration |
| FIREBASE_PROJECT_ID | `personalvm-511016` |
| ANDROID_APPLICATION_ID | `com.errorfactory.agentping` |
| AUTH_MODE / DEV_AUTH_TOKEN | Isolated local development authentication seed |
| OIDC_* | Intentionally blank until managed sign-in is configured |

The development token has been generated, but no application exists to enforce
it yet. The application must bind it to explicit local scopes/agent identity and
refuse this mode in production. It is not a production authentication system.

SQLAlchemy URLs must explicitly select psycopg (`postgresql+psycopg`). Raw libpq
URLs in the environment are normalized by the setup probes. Both pooled and direct
connections use `sslmode=verify-full` and the local certifi CA bundle.

Each new Neon branch has its own `agentping_migrator` credential. SQL-created
`agentping_runtime` has no schema CREATE privilege. Default table DML and sequence
USAGE/SELECT grants apply to future objects created by that migration role in
`public`. Recheck this when adding schemas or alternate object creators.
Only new development/test branch owner passwords were rotated to remove inherited
credential reuse; production credentials were never fetched or changed.

`.agentping/neon-context.json` records API-verified branch IDs/hostnames without
passwords. Probes require matching hosts and reject the production branch.
They also verify the client TLS state: Neon terminates client TLS at its proxy,
so the server's `pg_stat_ssl` is not evidence of the client-to-proxy connection.

## Firebase credentials

- Firebase CLI login is a human project-management credential.
- Python uses standard Google ADC, impersonating `agentping-dev` with short-lived
  credentials. CLI login alone does not configure Python ADC.
- ADC is stored at `~/.config/gcloud/application_default_credentials.json`.
- No service-account private key was generated or downloaded.
- `GOOGLE_APPLICATION_CREDENTIALS` is intentionally unset; if set later it must
  be a file path, not a JSON payload.
- The development sender has one custom permission: `cloudmessaging.messages.create`.
- The human user can impersonate this one service account via a binding on that
  service account, not a project-wide Token Creator grant.

Ignored Android Firebase configs are under `android/config/firebase/{debug,release}`.
These contain public app/project identifiers and a client API key, not backend
credentials. They are expected in the future APK, but are kept out of this repo
to avoid accidental environment mix-ups. Never put database credentials, the local
development token or service-account material into Android resources.

## Production preparation

No production environment or deployment exists. Future deployment must use:

- A deployment plan within INR 500/month, approved before provisioning. The earlier
  continuous paid Cloud Run worker-pool proposal is withdrawn. No paid Neon
  upgrade, always-on worker pool or paid minimum-instance setting is authorized.

- Separate Neon production runtime/migration credentials, explicitly authorized.
- Separate Cloud Run API and worker service identities; FCM sending belongs only
  to the worker identity. Do not reuse the development impersonation identity.
- Secret Manager access granted per required secret, not across the project.
- An attached service account for Google API access, avoiding JSON keys.
- A managed OAuth/OIDC provider, PKCE, audience validation, scopes, per-agent
  attribution and revocation. Auth0 is the proposed provider; the user deferred it.
- Public Cloud Run ingress, if needed by arbitrary MCP clients, only after the
  application enforces production authentication. Cloud Run Invoker IAM and the
  application's OAuth access token are separate authorization layers.

Do not log full database URLs, private payloads, access tokens, FCM registration
tokens, or Inspector session URLs. Administrative CLI sessions remain powerful
human setup credentials and must not be treated as blanket production approval.

The root `.dockerignore` also excludes credentials, environment files, private
diagnostics, virtual environments, Android configuration and generated output
from future root-context container builds. Preserve these protections when adding
the real backend Dockerfile and any alternative build contexts.
