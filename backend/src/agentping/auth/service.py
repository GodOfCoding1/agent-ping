"""Bearer-token authentication shared by REST and MCP."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agentping.auth import tokens
from agentping.auth.oidc import OidcVerifier
from agentping.auth.principal import (
    AGENT_GRANTABLE_SCOPES,
    DEFAULT_AGENT_SCOPES,
    DEVICE_SCOPES,
    USER_SCOPES,
    Principal,
)
from agentping.config import AuthMode
from agentping.db.models import AccessToken, AgentClient, AuditEvent, Device, User, UserIdentity
from agentping.domain.enums import ActorKind, AgentStatus, DeviceStatus
from agentping.domain.errors import Unauthenticated
from agentping.services.runtime import Runtime

LAST_USED_RESOLUTION = timedelta(minutes=5)


class Authenticator:
    def __init__(self, runtime: Runtime, oidc: OidcVerifier | None = None) -> None:
        self.runtime = runtime
        settings = runtime.settings
        self.oidc = oidc or (
            OidcVerifier(settings) if settings.auth_mode is AuthMode.OIDC else None
        )
        self._dev_user_id: uuid.UUID | None = None

    async def authenticate(self, raw: str | None, *, for_mcp: bool = False) -> Principal:
        if not raw:
            raise Unauthenticated("Missing bearer token")
        settings = self.runtime.settings
        dev_token = settings.dev_auth_token.get_secret_value()
        if settings.auth_mode is AuthMode.DEVELOPMENT and dev_token:
            if tokens.constant_time_equals(raw, dev_token):
                return await self._dev_principal()
        if raw.startswith((tokens.AGENT_PREFIX, tokens.DEVICE_PREFIX)):
            return await self._opaque_principal(raw)
        if self.oidc is not None and raw.count(".") == 2:
            return await self._oidc_principal(raw, for_mcp=for_mcp)
        raise Unauthenticated("Invalid bearer token")

    async def _dev_principal(self) -> Principal:
        if self._dev_user_id is None:
            async with self.runtime.session_factory() as session, session.begin():
                self._dev_user_id = (await ensure_user(session, self.runtime)).id
        return Principal(
            user_id=self._dev_user_id,
            actor_kind=ActorKind.USER,
            scopes=frozenset(USER_SCOPES),
            auth_method="development_token",
        )

    async def _opaque_principal(self, raw: str) -> Principal:
        now = self.runtime.clock.now()
        async with self.runtime.session_factory() as session, session.begin():
            token = (
                await session.execute(
                    select(AccessToken).where(AccessToken.token_hash == tokens.hash_secret(raw))
                )
            ).scalar_one_or_none()
            if token is None or token.revoked_at is not None:
                raise Unauthenticated("Token is invalid or revoked")
            user = await session.get(User, token.user_id)
            if user is None or user.status != "active":
                raise Unauthenticated("Account is unavailable")
            if token.expires_at is not None and token.expires_at <= now:
                raise Unauthenticated("Token has expired")
            if token.last_used_at is None or now - token.last_used_at > LAST_USED_RESOLUTION:
                token.last_used_at = now
            if token.agent_client_id is not None:
                agent = await session.get(AgentClient, token.agent_client_id)
                if agent is None or agent.status != AgentStatus.ACTIVE.value:
                    raise Unauthenticated("Agent authorization has been revoked")
                if agent.last_used_at is None or now - agent.last_used_at > LAST_USED_RESOLUTION:
                    agent.last_used_at = now
                scopes = frozenset(token.scopes) & frozenset(agent.scopes) & AGENT_GRANTABLE_SCOPES
                return Principal(
                    user_id=token.user_id,
                    actor_kind=ActorKind.AGENT,
                    scopes=scopes,
                    auth_method="agent_token",
                    agent_client_id=agent.id,
                    agent_name=agent.name,
                    token_id=token.id,
                    limits={k: int(v) for k, v in (agent.limits or {}).items()},
                )
            device = await session.get(Device, token.device_id)
            if device is None or device.status != DeviceStatus.ACTIVE.value:
                raise Unauthenticated("Device has been deactivated")
            return Principal(
                user_id=token.user_id,
                actor_kind=ActorKind.DEVICE,
                scopes=frozenset(token.scopes) & DEVICE_SCOPES,
                auth_method="device_token",
                device_id=device.id,
                token_id=token.id,
            )

    async def _oidc_principal(self, raw: str, *, for_mcp: bool = False) -> Principal:
        assert self.oidc is not None
        settings = self.runtime.settings
        audience = (
            settings.oidc_audience
            if for_mcp
            else (settings.oidc_api_audience or settings.oidc_audience)
        )
        verified = await self.oidc.verify(raw, audience=audience)
        allowed = settings.oidc_allowed_subjects
        if allowed and verified.subject not in allowed:
            raise Unauthenticated("This account is not permitted to use AgentPing")
        trusted = set(settings.oidc_user_client_ids)
        if settings.oidc_android_client_id:
            trusted.add(settings.oidc_android_client_id)
        is_user_app = verified.client_id in trusted and not for_mcp
        scopes = verified.scopes & (USER_SCOPES if is_user_app else AGENT_GRANTABLE_SCOPES)
        if not scopes:
            raise Unauthenticated("No AgentPing permissions were delegated")
        async with self.runtime.session_factory() as session, session.begin():
            # Serialize first-login races by provider identity without trusting email.
            identity_key = f"{settings.oidc_issuer}|{verified.subject}"
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
                {"identity": identity_key},
            )
            identity = (
                await session.execute(
                    select(UserIdentity).where(
                        UserIdentity.issuer == settings.oidc_issuer,
                        UserIdentity.subject == verified.subject,
                    )
                )
            ).scalar_one_or_none()
            user: User | None
            if identity is None:
                if not settings.oidc_signup_enabled and not allowed:
                    raise Unauthenticated("Account registration is not enabled")
                user = User(
                    id=uuid.uuid4(),
                    email=verified.claims.get("email"),
                    display_name=str(verified.claims.get("name") or "")[:200],
                    timezone=settings.default_timezone,
                    preferences={},
                    status="active",
                )
                session.add(user)
                await session.flush()
                session.add(
                    UserIdentity(
                        user_id=user.id,
                        issuer=settings.oidc_issuer,
                        subject=verified.subject,
                    )
                )
                await session.flush()
            else:
                user = await session.get(User, identity.user_id, with_for_update=True)
                if user is None:
                    raise Unauthenticated("Account is unavailable")
            if user is None or user.status != "active":
                raise Unauthenticated("Account is suspended or closed")
            if is_user_app:
                return Principal(
                    user_id=user.id,
                    actor_kind=ActorKind.USER,
                    scopes=frozenset(scopes),
                    auth_method="oidc",
                )
            agent = (
                await session.execute(
                    select(AgentClient).where(
                        AgentClient.user_id == user.id,
                        AgentClient.external_client_id == verified.client_id,
                    )
                )
            ).scalar_one_or_none()
            if agent is None:
                fingerprint = hashlib.sha256(verified.client_id.encode()).hexdigest()[:12]
                agent = AgentClient(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    name=(
                        settings.oidc_agent_client_names.get(verified.client_id, "OAuth app")[:80]
                        + " · "
                        + fingerprint
                    ),
                    kind="oidc",
                    external_client_id=verified.client_id,
                    scopes=sorted(scopes & DEFAULT_AGENT_SCOPES),
                    limits={},
                    status=AgentStatus.ACTIVE.value,
                )
                session.add(agent)
                await session.flush()
                session.add(
                    AuditEvent(
                        user_id=user.id,
                        event_type="agent.oauth_authorized",
                        category="security",
                        actor_kind=ActorKind.USER.value,
                        agent_client_id=agent.id,
                        summary="OAuth client authorized with delegated permissions",
                        details={"scopes": agent.scopes},
                    )
                )
            if agent.status != AgentStatus.ACTIVE.value:
                # Refreshing a JWT cannot resurrect a locally revoked consent.
                raise Unauthenticated("Agent authorization has been revoked")
            if verified.claims["iat"] < agent.oauth_not_before:
                raise Unauthenticated("Sign in again after the account owner renewed access")
            if (
                agent.name.startswith("oauth:")
                and verified.client_id in settings.oidc_agent_client_names
            ):
                agent.name = (
                    settings.oidc_agent_client_names[verified.client_id][:80]
                    + " · "
                    + hashlib.sha256(verified.client_id.encode()).hexdigest()[:12]
                )
            now = self.runtime.clock.now()
            if agent.last_used_at is None or now - agent.last_used_at > LAST_USED_RESOLUTION:
                agent.last_used_at = now
            return Principal(
                user_id=user.id,
                actor_kind=ActorKind.AGENT,
                scopes=frozenset(scopes) & frozenset(agent.scopes) & AGENT_GRANTABLE_SCOPES,
                auth_method="oidc",
                agent_client_id=agent.id,
                agent_name=agent.name,
                limits={k: int(v) for k, v in (agent.limits or {}).items()},
            )


async def ensure_user(session: AsyncSession, runtime: Runtime) -> User:
    """Return (creating if needed) the single development owner account."""
    settings = runtime.settings
    if settings.auth_mode is not AuthMode.DEVELOPMENT:
        raise Unauthenticated("Development owner bootstrap is unavailable with OIDC")
    user = (
        await session.execute(
            select(User).where(User.oidc_subject == "agentping:development-owner")
        )
    ).scalar_one_or_none()
    if user is None:
        # Preserve the pre-OAuth local owner, but never match an external account
        # by profile email. Only an account with no external identity is eligible.
        user = (
            await session.execute(
                select(User)
                .where(
                    User.email == settings.dev_user_email,
                    User.oidc_subject.is_(None),
                    ~select(UserIdentity.id).where(UserIdentity.user_id == User.id).exists(),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if user is not None:
            user.oidc_subject = "agentping:development-owner"
    if user is None:
        user = User(
            id=uuid.uuid4(),
            email=settings.dev_user_email,
            oidc_subject="agentping:development-owner",
            display_name="Owner",
            timezone=settings.default_timezone,
            preferences={},
            sync_seq=0,
            settings_revision=1,
        )
        session.add(user)
        await session.flush()
    return user
