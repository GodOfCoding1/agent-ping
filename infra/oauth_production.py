"""Production OAuth cutover: backup, migrate, configure, private worker, private API, publish.

Every stage captures command output and preserves private state. Uses the approved
existing GCP project and Neon production branch, with no resident worker or min instances.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import certifi
import psycopg
from cryptography.fernet import Fernet
from production import (
    INVOKER,
    NUMBER,
    ORIGIN,
    PRIVATE,
    PROJECT,
    QUEUE,
    REGION,
    ROOT,
    RUNTIME,
    SERVICE,
    gcloud,
    google_api,
    read,
    save,
)
from psycopg import sql
from sqlalchemy import make_url

WORKER = "agentping-worker"
WORKER_ORIGIN = f"https://{WORKER}-{NUMBER}.{REGION}.run.app"
API_IDENTITY = f"agentping-api@{PROJECT}.iam.gserviceaccount.com"


def command(args: list[str], *, env: dict[str, str] | None = None, timeout: int = 120) -> None:
    result = subprocess.run(
        args,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=timeout,
    )
    if result.returncode:
        save("last-command-error.json", {"executable": args[0], "stderr": result.stderr})
        raise RuntimeError(f"{Path(args[0]).name} failed (exit {result.returncode})")


def db_url(raw: str) -> str:
    return (
        make_url(raw)
        .set(drivername="postgresql")
        .update_query_dict({"sslrootcert": certifi.where()})
        .render_as_string(hide_password=False)
    )


def backup() -> None:
    # API-verify the exact production endpoint before any backup/migration connection.
    from production import BRANCH, NEON_PROJECT, neon

    endpoints = neon(f"/projects/{NEON_PROJECT}/endpoints")["endpoints"]
    host = next(
        e["host"] for e in endpoints if e["branch_id"] == BRANCH and e["type"] == "read_write"
    )
    url = make_url(read("database.json")["migration_url"])
    if url.host != host or url.query.get("sslmode") != "verify-full":
        raise RuntimeError("Production endpoint or TLS verification mismatch")
    assert url.host and url.username and url.password and url.database
    dump = PRIVATE / "before-oauth.dump"
    dump.touch(mode=0o600, exist_ok=True)
    command(
        ["pg_dump", "--format=custom", "--no-owner", "--no-acl", f"--file={dump}"],
        env={
            "PGHOST": url.host,
            "PGPORT": str(url.port or 5432),
            "PGUSER": url.username,
            "PGPASSWORD": url.password,
            "PGDATABASE": url.database,
            "PGSSLMODE": "verify-full",
            "PGSSLROOTCERT": certifi.where(),
        },
    )
    # Restore in a disposable LOCAL database. Never downgrade or restore into production.
    values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.compose.db").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    scratch = "agentping_restore_" + secrets.token_hex(8)
    connect: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": int(os.environ.get("AGENTPING_TEST_DB_PORT", "55433")),
        "user": "agentping_migrator",
        "password": values["POSTGRES_PASSWORD"],
    }
    with psycopg.connect(**connect, dbname="postgres", autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
    try:
        command(
            [
                "pg_restore",
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                "--dbname=" + scratch,
                str(dump),
            ],
            env={
                "PGHOST": "127.0.0.1",
                "PGPORT": str(connect["port"]),
                "PGUSER": connect["user"],
                "PGPASSWORD": connect["password"],
                "PGDATABASE": scratch,
                "PGSSLMODE": "disable",
            },
        )
        with psycopg.connect(**connect, dbname=scratch) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            assert row is not None
            revision = row[0]
            row = connection.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
            ).fetchone()
            assert row is not None
            count = row[0]
        save(
            "oauth-backup-result.json",
            {
                "schema": revision,
                "tables": count,
                "restore_verified_locally": True,
                "bytes": dump.stat().st_size,
            },
        )
    finally:
        with psycopg.connect(**connect, dbname="postgres", autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(scratch))
            )
    print("Fresh production backup restored and verified locally.")


def migrate() -> None:
    if not read("oauth-backup-result.json")["restore_verified_locally"]:
        raise RuntimeError("Verified backup required")
    state = read("database.json")
    auth = read("auth0.json")
    env = {
        **os.environ,
        "APP_ENV": "production",
        "AUTH_MODE": "oidc",
        "SERVICE_MODE": "local",
        "PUBLIC_BASE_URL": ORIGIN,
        "OIDC_ISSUER": auth["issuer"],
        "OIDC_AUDIENCE": auth["audience"],
        "DATABASE_URL": state["runtime_url"],
        "MIGRATION_DATABASE_URL": state["migration_url"],
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT / "backend",
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        save("last-command-error.json", {"executable": "alembic", "stderr": result.stderr})
        raise RuntimeError("OAuth migration failed; details remain private")
    with psycopg.connect(db_url(state["runtime_url"])) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row is not None
        revision = row[0]
        for table in ("user_identities", "portal_logins", "portal_sessions", "user_quotas"):
            grants = connection.execute(
                "SELECT has_table_privilege(current_user,%s,'SELECT,INSERT,UPDATE,DELETE')",
                (table,),
            ).fetchone()
            if grants is None or not grants[0]:
                raise RuntimeError("New table runtime permissions missing")
        ddl = connection.execute(
            "SELECT has_schema_privilege(current_user,'public','CREATE')"
        ).fetchone()
        if ddl is None or ddl[0]:
            raise RuntimeError("Runtime role unexpectedly has DDL access")
    save(
        "oauth-migration-result.json",
        {"schema": revision, "runtime_ddl": False, "grants_verified": True},
    )
    print("OAuth schema migrated; runtime grants verified without DDL privileges.")


def configure() -> None:
    accounts = json.loads(gcloud("iam", "service-accounts", "list", "--format=json"))
    if not any(a["email"] == API_IDENTITY for a in accounts):
        gcloud(
            "iam",
            "service-accounts",
            "create",
            "agentping-api",
            "--display-name=AgentPing public API",
        )
    for identity in (API_IDENTITY, RUNTIME):
        gcloud(
            "tasks",
            "queues",
            "add-iam-policy-binding",
            QUEUE,
            f"--location={REGION}",
            f"--member=serviceAccount:{identity}",
            "--role=roles/cloudtasks.enqueuer",
        )
        gcloud(
            "iam",
            "service-accounts",
            "add-iam-policy-binding",
            INVOKER,
            f"--member=serviceAccount:{identity}",
            "--role=roles/iam.serviceAccountUser",
        )
    path = PRIVATE / "portal-key.json"
    if not path.exists():
        save("portal-key.json", {"key": Fernet.generate_key().decode()})
    secrets_ = json.loads(gcloud("secrets", "list", "--format=json"))
    for name, value in [
        ("agentping-prod-portal-client", read("auth0.json")["portal_client_secret"]),
        ("agentping-prod-portal-key", read("portal-key.json")["key"]),
    ]:
        if not any(s["name"].endswith("/secrets/" + name) for s in secrets_):
            gcloud(
                "secrets",
                "create",
                name,
                "--replication-policy=automatic",
                "--labels=app=agentping,environment=production",
            )
            gcloud("secrets", "versions", "add", name, "--data-file=-", input_=value)
    for name in ("database-url", "worker-token", "portal-client", "portal-key"):
        gcloud(
            "secrets",
            "add-iam-policy-binding",
            "agentping-prod-" + name,
            f"--member=serviceAccount:{API_IDENTITY}",
            "--role=roles/secretmanager.secretAccessor",
        )
    print(
        "Separate API identity and encrypted-session secrets configured; no paid minimum instances."
    )


def environment(mode: str) -> dict[str, str]:
    auth = read("auth0.json")
    result = {
        "APP_ENV": "production",
        "AUTH_MODE": "oidc",
        "SERVICE_MODE": mode,
        "APPLICATION_TOKEN_HEADER": "Authorization",
        "OIDC_ISSUER": auth["issuer"],
        "OIDC_AUDIENCE": auth["audience"],
        "OIDC_API_AUDIENCE": auth["api_audience"],
        "OIDC_SIGNUP_ENABLED": "true",
        "OIDC_AGENT_CLIENT_NAMES": json.dumps({auth["codex_client_id"]: "Codex"}),
        "OIDC_USER_CLIENT_IDS": json.dumps([auth["portal_client_id"]]),
        "PUBLIC_BASE_URL": ORIGIN,
        "WORKER_BASE_URL": WORKER_ORIGIN,
        "DEFAULT_TIMEZONE": "Asia/Kolkata",
        "PUSH_PROVIDER": "fcm" if mode == "worker" else "disabled",
        "FIREBASE_PROJECT_ID": PROJECT,
        "FCM_RESTRICTED_PACKAGE_NAME": "com.errorfactory.agentping",
        "EMBEDDED_WORKER": "false",
        "WORKER_LISTEN": "false",
        "WORKER_BATCH_SIZE": "5",
        "DATABASE_POOL_SIZE": "2",
        "DATABASE_MAX_OVERFLOW": "2",
        "CLOUD_TASKS_QUEUE": f"projects/{PROJECT}/locations/{REGION}/queues/{QUEUE}",
        "CLOUD_TASKS_INVOKER_EMAIL": INVOKER,
        "FORWARDED_ALLOW_IPS": "*",
    }
    if mode == "api":
        result["PORTAL_CLIENT_ID"] = auth["portal_client_id"]
    return result


def deploy(mode: str) -> None:
    service = WORKER if mode == "worker" else SERVICE
    identity = RUNTIME if mode == "worker" else API_IDENTITY
    already_public = False
    if mode == "api":
        prior = json.loads(
            gcloud(
                "run", "services", "get-iam-policy", service, f"--region={REGION}", "--format=json"
            )
        )
        already_public = any(
            "allUsers" in binding.get("members", [])
            for binding in prior.get("bindings", [])
            if binding["role"] == "roles/run.invoker"
        )
    save(f"{mode}-environment.json", environment(mode))
    secret_map = (
        "DATABASE_URL=agentping-prod-database-url:1,"
        "WORKER_TRIGGER_TOKEN=agentping-prod-worker-token:1"
    )
    if mode == "api":
        secret_map += (
            ",PORTAL_CLIENT_SECRET=agentping-prod-portal-client:1,"
            "PORTAL_ENCRYPTION_KEY=agentping-prod-portal-key:1"
        )
    output = gcloud(
        "run",
        "deploy",
        service,
        f"--image={read('image.json')['image']}",
        f"--region={REGION}",
        "--platform=managed",
        f"--service-account={identity}",
        "--allow-unauthenticated" if already_public else "--no-allow-unauthenticated",
        "--invoker-iam-check",
        "--min=0",
        "--max=1",
        "--min-instances=0",
        "--max-instances=1",
        "--cpu=1",
        "--memory=512Mi",
        "--concurrency=8",
        "--timeout=120s",
        "--cpu-throttling",
        "--no-cpu-boost",
        "--port=8000",
        f"--env-vars-file={PRIVATE / f'{mode}-environment.json'}",
        "--set-secrets=" + secret_map,
        "--startup-probe=httpGet.path=/health,httpGet.port=8000,initialDelaySeconds=0,periodSeconds=5,failureThreshold=12,timeoutSeconds=3",
        "--labels=app=agentping,environment=production",
        "--format=json",
        timeout=600,
    )
    data = json.loads(output)
    save(f"oauth-{mode}-service.json", data)
    # Worker invocation remains private. Human access is for operational verification only.
    for member in (f"user:{read('owner.json')['email']}", f"serviceAccount:{INVOKER}"):
        gcloud(
            "run",
            "services",
            "add-iam-policy-binding",
            service,
            f"--region={REGION}",
            f"--member={member}",
            "--role=roles/run.invoker",
        )
    policy = json.loads(
        gcloud("run", "services", "get-iam-policy", service, f"--region={REGION}", "--format=json")
    )
    if not already_public and any(
        m in ("allUsers", "allAuthenticatedUsers")
        for b in policy.get("bindings", [])
        for m in b["members"]
    ):
        raise RuntimeError("Unexpected public grant before cutover")
    print(
        json.dumps(
            {
                "service": service,
                "private": not already_public,
                "revision": data.get("status", {}).get("latestReadyRevisionName"),
            }
        )
    )


def scheduler() -> None:
    name = f"projects/{PROJECT}/locations/{REGION}/jobs/agentping-recovery"
    # Preserve the existing cadence and retries; change only its private worker target.
    job = google_api("GET", f"https://cloudscheduler.googleapis.com/v1/{name}")
    job["httpTarget"].update(
        uri=WORKER_ORIGIN + "/internal/worker/run",
        oidcToken={"serviceAccountEmail": INVOKER, "audience": WORKER_ORIGIN},
    )
    google_api(
        "PATCH",
        f"https://cloudscheduler.googleapis.com/v1/{name}?updateMask=httpTarget",
        {"name": name, "httpTarget": job["httpTarget"]},
    )
    print("Hourly recovery now targets the IAM-private worker.")


def publish() -> None:
    # Browser callbacks require a reachable HTTPS endpoint. Opening transport starts
    # live verification; it does not declare the public production release complete.
    proof = read("oauth-private-smoke.json")
    if not proof.get("infrastructure_passed") or not (PRIVATE / "google-oauth.json").exists():
        raise RuntimeError("Passing private boundary checks and own Google keys required")
    gcloud(
        "run",
        "services",
        "add-iam-policy-binding",
        SERVICE,
        f"--region={REGION}",
        "--member=allUsers",
        "--role=roles/run.invoker",
    )
    save(
        "oauth-public-verification.json",
        {"transport_open": True, "release_complete": False, "browser_acceptance_pending": True},
    )
    print("API transport opened for live OAuth verification; worker remains IAM-private.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=["backup", "migrate", "configure", "worker", "api", "scheduler", "publish"]
    )
    args = parser.parse_args()
    try:
        if args.stage in ("worker", "api"):
            deploy(args.stage)
        else:
            globals()[args.stage]()
    except Exception as error:
        save(
            "oauth-stage-error.json",
            {"stage": args.stage, "type": type(error).__name__, "message": str(error)},
        )
        print(f"OAuth stage {args.stage} failed ({type(error).__name__}); details remain private.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
