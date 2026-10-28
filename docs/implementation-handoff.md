# AgentPing implementation handoff

The **backend is implemented**. Android is not. Paid deployment and production
writes still need explicit approval.

First read `AGENTS.md`, `README.md`, and
`docs/{api,mcp,scheduling,reliability,infrastructure,testing,environment,configuration,google-cloud,verification}.md`.

- **Backend:** Python 3.13, FastAPI, MCP SDK 2.3 Streamable HTTP at `/mcp`,
  SQLAlchemy 2 / psycopg 3, Alembic (revision `0002`), Pydantic v2, Firebase
  Admin. Local Compose stack and Neon dev/test schema are migrated. Commands:
  `./dev test`, `./dev api`, `./dev worker`, `./dev stack-smoke`.
- **Android:** JDK 21, SDK, AVD `AgentPing_API36` ready. No Gradle project/APK.
  Application IDs: `com.errorfactory.agentping` and `.debug`. When the app is
  built, upload the Firebase Installation ID (`onRegistered()`), not a legacy
  registration token, as `push_token` with `push_token_kind=fid`.
- **Database:** Neon project `empty-surf-03967309`. Dev
  `br-curly-wave-a1642imn` and test `br-divine-resonance-a143e0cp` are at
  Alembic `0002`. Production `br-sweet-mouse-a167349g` was not migrated.
- **Google Cloud:** project `personalvm-511016`, config `agentping`, region
  `asia-southeast1`. Budget INR 500/month. No Cloud Run services. Local ADC
  impersonates `agentping-dev`. Leave `automation-server` untouched. Railway
  excluded.
- **Auth:** development token + agent/device tokens. OIDC deferred; required
  before production exposure.

Never print `.env.local`, database URLs, tokens, or Firebase material.
