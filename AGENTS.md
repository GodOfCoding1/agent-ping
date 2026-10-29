# AgentPing agent instructions

## Scope and architecture

- Full Android application implementation and necessary backend additions/fixes
  are authorized. Extend the existing backend and Android shell through pairing,
  notifications, offline sync, reminders/alarms, complete UI and watch bridging.
- Development is local-only: Docker Compose PostgreSQL/API/worker, emulator or
  USB-connected phone, and existing development Firebase configuration. Do not
  add hosting/deployment automation or provision cloud infrastructure in this phase.
- Respect later explicit user instructions over these setup-stage constraints.
- Architecture: Python 3.13, FastAPI, official Python MCP SDK, SQLAlchemy 2.x,
  psycopg 3, Alembic, Neon PostgreSQL, FCM, Kotlin/Compose Android, Google Cloud.
- Deployment budget: INR 500/month or less. The previous always-on Cloud Run
  worker-pool proposal is withdrawn. Do not provision a continuous paid worker,
  upgrade Neon, or enable paid minimum instances. Evaluate request-driven Cloud
  Run with event-triggered work, or reuse of the existing VM after a capacity
  assessment. Hosting selection/deployment needs an approved plan within budget.
- Keep PostgreSQL-backed durable work and offline Android alarms, but avoid
  frequent idle database polling that exhausts Neon's Free compute allowance.
- Do not install Railway tooling or create Railway resources.
- Android ID: `com.errorfactory.agentping`; debug ID adds `.debug`. Do not use
  personal names for package identifiers.
- Keep scoped development authentication for now. Auth0/OIDC is intentionally
  deferred by the user; it is required before production exposure.
- Neon skills contain broad product suggestions. The architecture above and
  the user's instructions take precedence: do not add Next.js, Neon Functions,
  Object Storage, Auth, AI Gateway or Data API merely because skills suggest them.

## Commands and verification

- Use `./dev doctor`, `./dev imports`, `./dev db-check`, and `./dev firebase-check`.
- Use uv and `backend/uv.lock`; keep dependencies project-local.
- For setup tests: `uv run --project backend --locked pytest -c backend/pyproject.toml -q`.
- Lint/format/type commands are in README. Use the Gradle wrapper after an Android
  project exists; never substitute npm application builds.
- Android: `./dev android-setup`, `./dev android-check`, `./dev android-emulator`,
  `./dev android-run <serial>`, and `./dev android-test <serial>`. Always specify
  a test device serial. See `android/README.md` for the optional backend health test.
- Read the installed `android-cli` skill for Android CLI operations. Use its docs
  search for current Android guidance. AGP 9 uses built-in Kotlin; do not apply
  `org.jetbrains.kotlin.android`. Add Room/KSP/WorkManager when features need them.
- Android build tooling must not load `.env.local` or embed the owner development
  token. Pairing/device credentials belong only in secure Android runtime storage.
- Report actual exit results. An emulator boot is not an APK build, and FCM
  validate-only success is not device delivery.

## Safety and credentials

- Never print `.env.local`, full database URLs, access/refresh tokens, Firebase
  service-account material, or Inspector authentication URLs/tokens.
- Keep `.env.local`, `.agentping/`, `.neon`, credentials and Firebase config ignored.
- Use subprocess output capture for secret-bearing CLI commands. Redact exceptions;
  database connection errors can include credentials.
- Neon project `empty-surf-03967309`, org `org-soft-field-09040394`.
- Production branch `br-sweet-mouse-a167349g` is outside the authorized write scope.
- Development `br-curly-wave-a1642imn`; test `br-divine-resonance-a143e0cp`.
- These are schema-only root branches. Do not assume parent reset is available.
- `.agentping/neon-context.json` records API-verified endpoint identities. Database
  probes must refuse unverified or production hosts and require TLS.
- Runtime credentials are DML-only for objects created by `agentping_migrator` in
  `public`; migrations use a separate direct connection. Recheck grants when adding
  schemas or changing table owners. Never migrate with the runtime role.
- Use Google ADC impersonation of `agentping-dev` locally. Do not generate private
  service-account keys. Gcloud and Neon administrative logins are powerful setup
  credentials; their existence does not authorize production mutations.
- The existing `automation-server` VM and its workloads must remain untouched.
- New paid deployments, production writes, destructive operations, broad IAM grants,
  and disabling safety controls require explicit user authorization.
- No unrestricted Neon MCP server was installed. Prefer the scoped SQL probes and
  official project skills; a project-wide cloud key does not isolate production.
