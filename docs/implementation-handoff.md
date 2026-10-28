# AgentPing implementation handoff

Environment setup is complete. This is the implementation phase; continue with
the main application specification using the existing setup. First read
`AGENTS.md`, `README.md`, and `docs/{environment,configuration,google-cloud,verification}.md`.
Inspect and reuse existing configuration; do not reinstall compatible tools or
provision duplicate services.

- **Backend:** Python 3.13.18 and uv; dependencies are installed and locked in
  `backend/uv.lock`, including FastAPI, official MCP SDK 2.3.0, SQLAlchemy 2.x,
  psycopg 3, Alembic, Pydantic v2, Firebase Admin, pytest, Ruff and mypy.
- **Android:** JDK 21, SDK/ADB/Android CLI and API 36 Google Play ARM64 emulator
  `AgentPing_API36` are ready. Application IDs are `com.errorfactory.agentping`
  and `com.errorfactory.agentping.debug`. Firebase configs are under ignored
  `android/config/firebase/{release,debug}/google-services.json`.
- **Database:** reuse Neon project `empty-surf-03967309`, organization
  `org-soft-field-09040394`. `agentping-dev` and `agentping-test` are isolated
  schema-only root branches. Private `.env.local` contains separate runtime and
  migration URLs for each. Use pooled runtime connections and direct migration
  connections; never mutate `production` without explicit approval.
- **Google Cloud:** project `personalvm-511016`, named gcloud configuration
  `agentping`, target region `asia-southeast1`. Relevant APIs and Firebase are
  configured. Deployment budget is **INR 500/month or less**. The previous
  continuous Cloud Run worker-pool proposal is withdrawn. Evaluate request-driven
  Cloud Run with event-triggered work or reuse of the existing VM after checking
  capacity. Avoid continuous paid compute and frequent idle Neon polling. Railway
  is excluded; no new hosting option is approved for deployment yet.
  Local ADC impersonates `agentping-dev` without service-account private keys.
  Leave existing VM `automation-server` untouched. Docker AMD64 execution works.
- **Authentication:** an isolated development token is configured. Managed
  OAuth/OIDC is deferred and required before production exposure.
- **Verified:** six setup safety tests, lint/type checks, four TLS database
  connections, FCM validate-only, emulator boot, Docker and MCP Inspector.
  Recheck with `./dev doctor`, `./dev imports`, `./dev db-check`, and
  `./dev firebase-check`. Never print secrets.

Only environment scaffolding exists: no application API, worker, domain schema,
migrations, Android Gradle project/APK, or deployment. Begin Stage A and build the
first working notification slice. Paid deployment and production changes still
require explicit approval.
