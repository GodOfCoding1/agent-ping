"""Explicit, secret-safe operations for the approved AgentPing production backend.

Run with: uv run --project backend --locked python infra/production.py <stage>
Secrets and command output are captured; private state stays in .agentping/production.
This never reads .env.local, touches the existing VM, or changes the Neon plan.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import certifi
import httpx
import psycopg
from psycopg import sql
from sqlalchemy import URL, make_url

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".agentping" / "production"
PROJECT = "personalvm-511016"
NUMBER = "583092569828"
REGION = "asia-southeast1"
NEON_PROJECT = "empty-surf-03967309"
BRANCH = "br-sweet-mouse-a167349g"
SERVICE = "agentping-api"
REPOSITORY = "agentping"
QUEUE = "agentping-production"
RUNTIME = f"agentping-prod@{PROJECT}.iam.gserviceaccount.com"
INVOKER = f"agentping-task-invoker@{PROJECT}.iam.gserviceaccount.com"
ORIGIN = f"https://{SERVICE}-{NUMBER}.{REGION}.run.app"


def save(name: str, data: Any) -> None:
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = PRIVATE / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)


def read(name: str) -> Any:
    return json.loads((PRIVATE / name).read_text())


def run(args: list[str], *, input_: str | None = None, timeout: int = 120) -> str:
    result = subprocess.run(
        args, input=input_, text=True, capture_output=True, cwd=ROOT, timeout=timeout
    )
    if result.returncode:
        # Never echo a secret-bearing command, exception, stdout or stderr.
        save("last-command-error.json", {"executable": args[0], "stderr": result.stderr})
        raise RuntimeError(f"{args[0]} command failed (exit {result.returncode})")
    return result.stdout.strip()


def gcloud(*args: str, input_: str | None = None, timeout: int = 120) -> str:
    return run(
        ["gcloud", "--configuration=agentping", f"--project={PROJECT}", "--quiet", *args],
        input_=input_,
        timeout=timeout,
    )


def neon(path: str, *, body: dict[str, Any] | None = None) -> Any:
    args = ["neon", "api", path, "-o", "json"]
    if body is not None:
        args += ["--method", "PATCH", "--data=" + json.dumps(body)]
    return json.loads(run(args))


def google_api(method: str, url: str, body: Any = None, *, accepted: tuple[int, ...] = ()) -> Any:
    token = gcloud("auth", "print-access-token")
    response = httpx.request(
        method, url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=90
    )
    if response.is_error and response.status_code not in accepted:
        raise RuntimeError(f"Google API failed (HTTP {response.status_code})")
    return response.json() if response.content else {}


def owner_claims() -> dict[str, Any]:
    raw = gcloud("auth", "print-identity-token")
    claims = json.loads(base64.urlsafe_b64decode(raw.split(".")[1] + "=="))
    if claims.get("iss") != "https://accounts.google.com" or not claims.get("email"):
        raise RuntimeError("Expected the signed-in human's Google identity")
    owner = {key: claims.get(key) for key in ("iss", "aud", "azp", "sub", "email")}
    save("owner.json", owner)
    return owner


def environment() -> dict[str, str]:
    owner = read("owner.json")
    return {
        "APP_ENV": "production",
        "AUTH_MODE": "oidc",
        "APPLICATION_TOKEN_HEADER": "X-AgentPing-Authorization",
        "OIDC_ISSUER": owner["iss"],
        "OIDC_AUDIENCE": owner["aud"],
        "OIDC_ANDROID_CLIENT_ID": owner["azp"] or owner["aud"],
        "OIDC_ALLOWED_SUBJECTS": json.dumps([owner["sub"]]),
        "PUBLIC_BASE_URL": ORIGIN,
        "DEFAULT_TIMEZONE": "Asia/Kolkata",
        "PUSH_PROVIDER": "fcm",
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


def database() -> None:
    branch = neon(f"/projects/{NEON_PROJECT}/branches/{BRANCH}")["branch"]
    if branch["id"] != BRANCH or branch["name"] != "production":
        raise RuntimeError("Production branch verification failed")
    endpoints = neon(f"/projects/{NEON_PROJECT}/endpoints")["endpoints"]
    endpoint = next(e for e in endpoints if e["branch_id"] == BRANCH and e["type"] == "read_write")
    host = endpoint["host"]
    if not host.endswith(".ap-southeast-1.aws.neon.tech"):
        raise RuntimeError("Unexpected production database region")
    # Limit Free-plan compute, without changing the plan-managed suspend timeout.
    neon(
        f"/projects/{NEON_PROJECT}/endpoints/{endpoint['id']}",
        body={"endpoint": {"autoscaling_limit_min_cu": 0.25, "autoscaling_limit_max_cu": 0.25}},
    )
    raw = run(
        [
            "neon",
            "connection-string",
            BRANCH,
            "--project-id",
            NEON_PROJECT,
            "--role-name",
            "neondb_owner",
            "--database-name",
            "neondb",
            "--ssl",
            "verify-full",
        ]
    )
    owner_url = make_url(raw)
    if owner_url.host != host:
        raise RuntimeError("API-verified production host does not match connection")
    raw = owner_url.update_query_dict({"sslrootcert": certifi.where()}).render_as_string(
        hide_password=False
    )
    credentials_file = PRIVATE / "database.json"
    if credentials_file.exists():
        credentials = read("database.json")
        if credentials["host"] != host or credentials["branch_id"] != BRANCH:
            raise RuntimeError("Stored production identity does not match verified endpoint")
    else:
        credentials = {
            "host": host,
            "branch_id": BRANCH,
            "migrator_password": secrets.token_urlsafe(36),
            "runtime_password": secrets.token_urlsafe(36),
            "worker_token": secrets.token_urlsafe(48),
        }
        save("database.json", credentials)
    with psycopg.connect(raw, connect_timeout=20) as connection:
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall()
        if tables and not (PRIVATE / "database-initialized.json").exists():
            raise RuntimeError("Production is not empty; refusing an unreviewed schema mutation")
        for role, password_key in [
            ("agentping_migrator", "migrator_password"),
            ("agentping_runtime", "runtime_password"),
        ]:
            exists = connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone()
            if not exists:
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOCREATEDB NOCREATEROLE").format(
                        sql.Identifier(role), sql.Literal(credentials[password_key])
                    )
                )
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute("GRANT CREATE, USAGE ON SCHEMA public TO agentping_migrator")
        connection.execute("GRANT USAGE ON SCHEMA public TO agentping_runtime")
        connection.execute(
            "GRANT CONNECT ON DATABASE neondb TO agentping_migrator, agentping_runtime"
        )
    save("database-initialized.json", {"branch": BRANCH, "verified_host": host})

    def url(role: str, password: str, pooled: bool = False) -> str:
        return URL.create(
            "postgresql+psycopg",
            username=role,
            password=password,
            host=host.replace(".ap-southeast", "-pooler.ap-southeast") if pooled else host,
            database="neondb",
            query={"sslmode": "verify-full", "sslrootcert": "system"},
        ).render_as_string(hide_password=False)

    runtime_url = url("agentping_runtime", credentials["runtime_password"], pooled=True)
    migration_url = url("agentping_migrator", credentials["migrator_password"])
    credentials.update(runtime_url=runtime_url, migration_url=migration_url)
    save("database.json", credentials)
    with psycopg.connect(
        make_url(migration_url)
        .set(drivername="postgresql")
        .update_query_dict({"sslrootcert": certifi.where()})
        .render_as_string(hide_password=False)
    ) as connection:
        connection.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agentping_runtime"
        )
        connection.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO agentping_runtime"
        )
    env = {
        **os.environ,
        **environment(),
        "CLOUD_TASKS_QUEUE": "",
        "DATABASE_URL": runtime_url,
        "MIGRATION_DATABASE_URL": migration_url,
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
        raise RuntimeError(
            f"Production migration failed (exit {result.returncode}); output withheld"
        )
    with psycopg.connect(
        make_url(migration_url)
        .set(drivername="postgresql")
        .update_query_dict({"sslrootcert": certifi.where()})
        .render_as_string(hide_password=False)
    ) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version is not None
        revision = version[0]
        owners = connection.execute(
            "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname='public'"
        ).fetchall()
        if owners != [("agentping_migrator",)]:
            raise RuntimeError("Unexpected production table ownership")
    with psycopg.connect(
        make_url(runtime_url)
        .set(drivername="postgresql")
        .update_query_dict({"sslrootcert": certifi.where()})
        .render_as_string(hide_password=False)
    ) as connection:
        grants = connection.execute(
            "SELECT has_schema_privilege(current_user,'public','CREATE'), "
            "pg_has_role(current_user,'agentping_migrator','MEMBER')"
        ).fetchone()
        if grants is None or any(grants):
            raise RuntimeError("Runtime has forbidden DDL/migrator privileges")
        connection.execute("SELECT count(*) FROM users").fetchone()
    save(
        "database-verified.json", {"revision": revision, "runtime_ddl": False, "tls": "verify-full"}
    )
    print(json.dumps({"stage": "database", "schema": revision, "runtime_ddl": False}))


def infrastructure() -> None:
    gcloud("services", "enable", "cloudtasks.googleapis.com", "cloudscheduler.googleapis.com")
    for name in ("agentping-prod", "agentping-task-invoker"):
        accounts = json.loads(gcloud("iam", "service-accounts", "list", "--format=json"))
        if not any(a["email"] == f"{name}@{PROJECT}.iam.gserviceaccount.com" for a in accounts):
            gcloud("iam", "service-accounts", "create", name, f"--display-name={name}")
    repositories = json.loads(
        gcloud("artifacts", "repositories", "list", f"--location={REGION}", "--format=json")
    )
    if not any(r["name"].endswith(f"/repositories/{REPOSITORY}") for r in repositories):
        gcloud(
            "artifacts",
            "repositories",
            "create",
            REPOSITORY,
            "--repository-format=docker",
            f"--location={REGION}",
            "--labels=app=agentping,environment=production",
        )
    policies = [
        {
            "name": "delete-old",
            "action": {"type": "Delete"},
            "condition": {"tagState": "any", "olderThan": "604800s"},
        },
        {"name": "keep-two", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
    ]
    save("cleanup.json", policies)
    gcloud(
        "artifacts",
        "repositories",
        "set-cleanup-policies",
        REPOSITORY,
        f"--location={REGION}",
        f"--policy={PRIVATE / 'cleanup.json'}",
        "--no-dry-run",
    )
    queues = json.loads(gcloud("tasks", "queues", "list", f"--location={REGION}", "--format=json"))
    operation = (
        "update" if any(q["name"].endswith(f"/queues/{QUEUE}") for q in queues) else "create"
    )
    gcloud(
        "tasks",
        "queues",
        operation,
        QUEUE,
        f"--location={REGION}",
        "--max-concurrent-dispatches=1",
        "--max-dispatches-per-second=1",
        "--max-attempts=8",
        "--max-retry-duration=3600s",
        "--min-backoff=5s",
        "--max-backoff=300s",
    )
    gcloud(
        "tasks",
        "queues",
        "add-iam-policy-binding",
        QUEUE,
        f"--location={REGION}",
        f"--member=serviceAccount:{RUNTIME}",
        "--role=roles/cloudtasks.enqueuer",
    )
    gcloud(
        "iam",
        "service-accounts",
        "add-iam-policy-binding",
        INVOKER,
        f"--member=serviceAccount:{RUNTIME}",
        "--role=roles/iam.serviceAccountUser",
    )
    gcloud(
        "projects",
        "add-iam-policy-binding",
        PROJECT,
        f"--member=serviceAccount:{RUNTIME}",
        f"--role=projects/{PROJECT}/roles/agentPingFcmSender",
        "--condition=None",
    )
    existing = json.loads(gcloud("secrets", "list", "--format=json"))
    credentials = read("database.json")
    for name, value in [
        ("agentping-prod-database-url", credentials["runtime_url"]),
        ("agentping-prod-worker-token", credentials["worker_token"]),
    ]:
        if not any(s["name"].endswith(f"/secrets/{name}") for s in existing):
            gcloud(
                "secrets",
                "create",
                name,
                "--replication-policy=automatic",
                "--labels=app=agentping,environment=production",
            )
            gcloud("secrets", "versions", "add", name, "--data-file=-", input_=value)
        gcloud(
            "secrets",
            "add-iam-policy-binding",
            name,
            f"--member=serviceAccount:{RUNTIME}",
            "--role=roles/secretmanager.secretAccessor",
        )
    print(json.dumps({"stage": "infrastructure", "min_instances": 0, "secrets": 2, "queue": QUEUE}))


def build() -> None:
    tag = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    image = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPOSITORY}/backend:{tag}"
    run(
        [
            "docker",
            "buildx",
            "build",
            "--platform=linux/amd64",
            "--load",
            "-t",
            image,
            "-f",
            "backend/Dockerfile",
            ".",
        ],
        timeout=900,
    )
    token = gcloud("auth", "print-access-token")
    run(
        [
            "docker",
            "login",
            "-u",
            "oauth2accesstoken",
            "--password-stdin",
            f"https://{REGION}-docker.pkg.dev",
        ],
        input_=token,
    )
    run(["docker", "push", image], timeout=600)
    save("image.json", {"image": image})
    print(json.dumps({"stage": "build", "image": image}))


def deploy() -> None:
    save("environment.json", environment())  # JSON is also a valid YAML mapping.
    image = read("image.json")["image"]
    output = gcloud(
        "run",
        "deploy",
        SERVICE,
        f"--image={image}",
        f"--region={REGION}",
        "--platform=managed",
        f"--service-account={RUNTIME}",
        "--no-allow-unauthenticated",
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
        f"--env-vars-file={PRIVATE / 'environment.json'}",
        "--set-secrets=DATABASE_URL=agentping-prod-database-url:1,WORKER_TRIGGER_TOKEN=agentping-prod-worker-token:1",
        "--startup-probe=httpGet.path=/health,httpGet.port=8000,initialDelaySeconds=0,periodSeconds=5,failureThreshold=12,timeoutSeconds=3",
        "--labels=app=agentping,environment=production",
        "--format=json",
        timeout=600,
    )
    data = json.loads(output)
    save("service.json", data)
    for member in (f"user:{read('owner.json')['email']}", f"serviceAccount:{INVOKER}"):
        gcloud(
            "run",
            "services",
            "add-iam-policy-binding",
            SERVICE,
            f"--region={REGION}",
            f"--member={member}",
            "--role=roles/run.invoker",
        )
    policy = json.loads(
        gcloud("run", "services", "get-iam-policy", SERVICE, f"--region={REGION}", "--format=json")
    )
    if any(
        m in ("allUsers", "allAuthenticatedUsers")
        for b in policy.get("bindings", [])
        for m in b["members"]
    ):
        raise RuntimeError("Unexpected broad Cloud Run invocation grant")
    scheduler_url = (
        f"https://cloudscheduler.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/jobs"
    )
    name = f"projects/{PROJECT}/locations/{REGION}/jobs/agentping-recovery"
    job = {
        "name": name,
        "description": "Hourly durable outbox recovery; does not poll while idle",
        "schedule": "0 * * * *",
        "timeZone": "Etc/UTC",
        "attemptDeadline": "120s",
        "retryConfig": {"retryCount": 3, "minBackoffDuration": "30s", "maxBackoffDuration": "300s"},
        "httpTarget": {
            "uri": f"{ORIGIN}/internal/worker/run",
            "httpMethod": "POST",
            "headers": {"X-Worker-Token": read("database.json")["worker_token"]},
            "oidcToken": {"serviceAccountEmail": INVOKER, "audience": ORIGIN},
        },
    }
    listed = google_api("GET", scheduler_url)
    if any(j["name"] == name for j in listed.get("jobs", [])):
        google_api("PATCH", f"https://cloudscheduler.googleapis.com/v1/{name}", job)
    else:
        google_api("POST", scheduler_url, job)
    print(
        json.dumps(
            {
                "stage": "deploy",
                "url": ORIGIN,
                "private": True,
                "revision": data.get("status", {}).get("latestReadyRevisionName"),
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=["identity", "database", "infrastructure", "build", "deploy"]
    )
    args = parser.parse_args()
    try:
        if args.stage == "identity":
            owner_claims()
            print(
                json.dumps(
                    {"stage": "identity", "provider": "Google OIDC", "owner_restricted": True}
                )
            )
        else:
            globals()[args.stage]()
    except Exception as error:
        save(
            "last-stage-error.json",
            {"stage": args.stage, "type": type(error).__name__, "message": str(error)},
        )
        print(
            json.dumps(
                {
                    "stage": args.stage,
                    "error_type": type(error).__name__,
                    "status": "failed",
                    "detail": "Sensitive output withheld",
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
