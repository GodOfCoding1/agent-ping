"""Non-disruptive environment probes. No application or cloud provisioning code."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import URL, create_engine, make_url, text

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / ".agentping" / "neon-context.json"
PROJECT = "empty-surf-03967309"


def emit(check: str, status: str, **details: Any) -> None:
    print(json.dumps({"check": check, "status": status, **details}, sort_keys=True))


def run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, text=True, capture_output=True, timeout=timeout, check=False
    )


def doctor() -> int:
    failed = False
    emit("python", "PASS", version=platform.python_version(), executable=sys.executable)
    commands = {
        "git": ["--version"],
        "uv": ["--version"],
        "node": ["--version"],
        "npm": ["--version"],
        "java": ["-version"],
        "docker": ["--version"],
        "psql": ["--version"],
        "curl": ["--version"],
        "gh": ["--version"],
        "gcloud": ["version", "--format=json"],
        "neon": ["--version"],
        "firebase": ["--version"],
        "android": ["--version"],
        "sdkmanager": ["--version"],
        "adb": ["version"],
        "emulator": ["-version"],
    }
    for tool, arguments in commands.items():
        executable = shutil.which(tool)
        if executable is None:
            emit(tool, "MISSING")
            failed = True
            continue
        try:
            result = run([executable, *arguments])
            output = (result.stdout or result.stderr).strip().splitlines()
            version = output[0] if output else ""
            if tool == "gcloud" and result.returncode == 0:
                version = json.loads(result.stdout).get("Google Cloud SDK", "unknown")
            emit(
                tool,
                "PASS" if result.returncode == 0 else "FAIL",
                path=executable,
                version=version,
                stderr_present=bool(result.stderr.strip()),
            )
            failed |= result.returncode != 0
        except (OSError, subprocess.TimeoutExpired) as error:
            emit(tool, "FAIL", error_type=type(error).__name__)
            failed = True
    if shutil.which("docker"):
        try:
            result = run(["docker", "info", "--format", "{{.ServerVersion}}"])
            emit("docker_engine", "PASS" if result.returncode == 0 else "FAIL")
            failed |= result.returncode != 0
        except (OSError, subprocess.TimeoutExpired) as error:
            emit("docker_engine", "FAIL", error_type=type(error).__name__)
            failed = True
    if shutil.which("gcloud"):
        result = run(
            [
                "gcloud",
                "auth",
                "list",
                "--filter=status:ACTIVE",
                "--format=value(account)",
            ]
        )
        emit(
            "gcloud_login",
            "PASS" if result.returncode == 0 and result.stdout.strip() else "PENDING",
        )
    for name in [
        "DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "FIREBASE_PROJECT_ID",
        "OIDC_ISSUER",
    ]:
        emit(name, "CONFIGURED" if os.environ.get(name) else "PENDING")
    emit("neon_verified_context", "CONFIGURED" if CONTEXT.exists() else "PENDING")
    return int(failed)


def import_check() -> int:
    for name in [
        "fastapi",
        "uvicorn",
        "mcp",
        "sqlalchemy",
        "psycopg",
        "alembic",
        "pydantic",
        "pydantic_settings",
        "firebase_admin",
        "google.auth",
        "pytest",
        "httpx",
    ]:
        importlib.import_module(name)
        emit(name, "PASS")
    return 0


def guarded_url(
    name: str, environment: Mapping[str, str], context: Mapping[str, Any]
) -> URL:
    """Require an API-verified nonproduction endpoint before opening any connection."""
    if environment.get("APP_ENV") not in {"development", "test"}:
        raise ValueError("Set APP_ENV to development or test")
    if context.get("project_id") != PROJECT:
        raise ValueError("Expected verified Neon project context")
    branch = context.get("test" if name.startswith("TEST_") else "development", {})
    if not isinstance(branch, dict) or not branch.get("branch_id"):
        raise ValueError("Missing API-verified branch context")
    if branch["branch_id"] == context.get("production_branch_id"):
        raise ValueError("Production branch is forbidden")
    raw = environment.get(name, "")
    if not raw:
        raise ValueError(f"{name} is not configured")
    url = make_url(raw)
    expected = branch.get("direct_host" if "MIGRATION" in name else "pooled_host")
    if not expected or url.host != expected:
        raise ValueError("Endpoint does not match API-verified development context")
    if url.get_backend_name() not in {"postgresql", "postgres"}:
        raise ValueError("A PostgreSQL connection is required")
    if url.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise ValueError("TLS is required")
    return url.set(drivername="postgresql+psycopg")


def database_check() -> int:
    if not CONTEXT.exists():
        emit(
            "database",
            "PENDING",
            reason="Neon login and verified development context required",
        )
        return 2
    context = json.loads(CONTEXT.read_text())
    failed = False
    for name in [
        "DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_MIGRATION_DATABASE_URL",
    ]:
        engine = None
        try:
            url = guarded_url(name, os.environ, context)
            engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
                hide_parameters=True,
                connect_args={"connect_timeout": 20, "prepare_threshold": None},
            )
            with engine.connect() as connection, connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                value: int = connection.execute(text("SELECT 1")).scalar_one()
                # Neon terminates client TLS at its proxy. pg_stat_ssl describes
                # the proxy-to-compute connection, not this client's transport.
                driver = connection.connection.driver_connection
                tls = bool(driver is not None and driver.pgconn.ssl_in_use)
                if value != 1 or not tls:
                    raise RuntimeError("Read-only query or TLS verification failed")
                can_create: bool = connection.execute(
                    text(
                        "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
                    )
                ).scalar_one()
                if can_create != ("MIGRATION" in name):
                    raise RuntimeError(
                        "Unexpected runtime or migration schema privileges"
                    )
            emit(name, "PASS", query="SELECT 1", tls=True)
        except Exception as error:
            # Driver exception messages can embed credentials or connection strings.
            emit(name, "FAIL", error_type=type(error).__name__)
            failed = True
        finally:
            if engine is not None:
                engine.dispose()
    return int(failed)


def firebase_check() -> int:
    import firebase_admin
    import google.auth
    from firebase_admin import credentials as admin_credentials
    from firebase_admin import messaging
    from google.auth.transport.requests import Request

    project = os.environ.get("FIREBASE_PROJECT_ID")
    if not project:
        emit("firebase_adc", "PENDING", reason="Firebase project selection required")
        return 2
    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        credentials.refresh(Request())
        emit(
            "firebase_adc",
            "PASS",
            credential_type=f"{type(credentials).__module__}.{type(credentials).__name__}",
        )
        app = firebase_admin.initialize_app(
            admin_credentials.ApplicationDefault(),
            {"projectId": project},
            name="setup-probe",
        )
        try:
            messaging.send(
                messaging.Message(
                    topic="agentping-environment-check",
                    data={"probe": "credential-validation"},
                ),
                dry_run=True,
                app=app,
            )
            emit("fcm_validate_only", "PASS", delivered=False)
        finally:
            firebase_admin.delete_app(app)
        emit(
            "fcm_delivery",
            "NOT_RUN",
            reason="Application and device registration token required",
        )
        return 0
    except Exception as error:
        emit("firebase_adc", "FAIL", error_type=type(error).__name__)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["doctor", "imports", "db-check", "firebase-check"]
    )
    args = parser.parse_args()
    actions = {
        "doctor": doctor,
        "imports": import_check,
        "db-check": database_check,
        "firebase-check": firebase_check,
    }
    return actions[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
