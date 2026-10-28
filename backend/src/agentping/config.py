"""Application configuration loaded from environment variables."""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL, make_url


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    DEVELOPMENT = "development"
    OIDC = "oidc"


class PushProvider(StrEnum):
    FCM = "fcm"
    LOG = "log"
    DISABLED = "disabled"


StrList = Annotated[list[str], NoDecode]
LOCAL_DATABASE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "db", "postgres"})
NEON_CONTEXT_PATH = Path(__file__).resolve().parents[3] / ".agentping" / "neon-context.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: str = "INFO"
    log_json: bool = True

    database_url: SecretStr = SecretStr("")
    migration_database_url: SecretStr = SecretStr("")
    database_pool_size: int = 5
    database_max_overflow: int = 5
    neon_context_path: Path = NEON_CONTEXT_PATH

    public_base_url: str = "http://127.0.0.1:8000"
    mcp_allowed_hosts: StrList = Field(default_factory=list)
    mcp_allowed_origins: StrList = Field(default_factory=list)
    cors_allowed_origins: StrList = Field(default_factory=list)

    auth_mode: AuthMode = AuthMode.DEVELOPMENT
    dev_auth_token: SecretStr = SecretStr("")
    dev_user_email: str = "owner@agentping.local"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_android_client_id: str = ""
    oidc_jwks_url: str = ""
    oidc_allowed_subjects: StrList = Field(default_factory=list)
    oidc_scope_claim: str = "scope"

    push_provider: PushProvider = PushProvider.LOG
    firebase_project_id: str = ""
    android_application_id: str = "com.errorfactory.agentping"
    fcm_dry_run: bool = False
    fcm_restricted_package_name: str = ""

    default_timezone: str = "UTC"
    max_request_bytes: int = 64 * 1024
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 40
    agent_max_active_reminders: int = 200
    agent_max_ringing_reminders: int = 25
    agent_max_sends_per_hour: int = 60
    min_recurrence_interval_seconds: int = 300
    occurrence_horizon_days: int = 14
    occurrence_horizon_count: int = 50
    sync_change_retention_days: int = 30
    pairing_code_ttl_seconds: int = 600
    device_token_ttl_days: int = 365

    embedded_worker: bool = False
    worker_batch_size: int = 20
    worker_lease_seconds: int = 60
    worker_idle_max_seconds: float = 300.0
    worker_idle_min_seconds: float = 0.5
    worker_max_attempts: int = 8
    maintenance_interval_seconds: int = 6 * 3600
    worker_trigger_token: SecretStr = SecretStr("")
    worker_listen: bool = True
    worker_listen_database_url: SecretStr = SecretStr("")

    @field_validator(
        "mcp_allowed_hosts",
        "mcp_allowed_origins",
        "cors_allowed_origins",
        "oidc_allowed_subjects",
        mode="before",
    )
    @classmethod
    def _split_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _production_safety(self) -> Settings:
        if self.app_env is AppEnv.PRODUCTION:
            if self.auth_mode is AuthMode.DEVELOPMENT:
                raise ValueError("AUTH_MODE=development is refused when APP_ENV=production")
            if not self.public_base_url.startswith("https://"):
                raise ValueError("PUBLIC_BASE_URL must use HTTPS in production")
        if self.auth_mode is AuthMode.OIDC and not (self.oidc_issuer and self.oidc_audience):
            raise ValueError("AUTH_MODE=oidc requires OIDC_ISSUER and OIDC_AUDIENCE")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def mcp_resource_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/mcp"

    def runtime_database_url(self) -> URL:
        return checked_database_url(self.database_url.get_secret_value(), self, migration=False)

    def migration_url(self) -> URL:
        raw = self.migration_database_url.get_secret_value() or self.database_url.get_secret_value()
        return checked_database_url(raw, self, migration=True)


class DatabaseGuardError(ValueError):
    """Raised when a database URL is not safe for the current environment."""


def _verified_neon_hosts(settings: Settings, *, migration: bool) -> set[str]:
    path = settings.neon_context_path
    if not path.exists():
        return set()
    context = json.loads(path.read_text())
    production_branch = context.get("production_branch_id")
    hosts: set[str] = set()
    for key in ("development", "test"):
        branch = context.get(key) or {}
        if not branch.get("branch_id") or branch.get("branch_id") == production_branch:
            continue
        host = branch.get("direct_host" if migration else "pooled_host")
        if host:
            hosts.add(host)
    return hosts


def checked_database_url(raw: str, settings: Settings, *, migration: bool) -> URL:
    """Normalize a database URL to psycopg and refuse unsafe non-production targets.

    Outside production, only local hosts or API-verified Neon development/test endpoints
    are accepted. Remote hosts always require TLS. Error messages never include the URL.
    """
    if not raw:
        raise DatabaseGuardError("Database URL is not configured")
    url = make_url(raw)
    if url.get_backend_name() not in {"postgresql", "postgres"}:
        raise DatabaseGuardError("A PostgreSQL connection is required")
    host = url.host or ""
    local = host in LOCAL_DATABASE_HOSTS
    if not local and url.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise DatabaseGuardError("TLS (sslmode=require or stricter) is required for remote hosts")
    if not settings.is_production and not local:
        verified = _verified_neon_hosts(settings, migration=migration)
        if host not in verified:
            raise DatabaseGuardError(
                "Remote database host is not an API-verified development/test endpoint"
            )
    return url.set(drivername="postgresql+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
