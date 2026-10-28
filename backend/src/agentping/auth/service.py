"""Bearer-token authentication shared by REST and MCP."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
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
from agentping.db.models import AccessToken, AgentClient, Device, User
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

    async def authenticate(self, raw: str | None) -> Principal:
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
            return await self._oidc_principal(raw)
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

    async def _oidc_principal(self, raw: str) -> Principal:
        assert self.oidc is not None
        settings = self.runtime.settings
        verified = await self.oidc.verify(raw)
        allowed = settings.oidc_allowed_subjects
        if allowed and verified.subject not in allowed:
            raise Unauthenticated("This account is not permitted to use AgentPing")
        async with self.runtime.session_factory() as session, session.begin():
            user = (
                await session.execute(select(User).where(User.oidc_subject == verified.subject))
            ).scalar_one_or_none()
            if user is None:
                if not allowed:
                    raise Unauthenticated("Unknown user; add the subject to OIDC_ALLOWED_SUBJECTS")
                user = User(
                    id=uuid.uuid4(),
                    oidc_subject=verified.subject,
                    email=verified.claims.get("email"),
                    display_name=str(verified.claims.get("name") or ""),
                    timezone=settings.default_timezone,
                    preferences={},
                )
                session.add(user)
                await session.flush()
            is_user_app = verified.client_id in (None, settings.oidc_android_client_id)
            if is_user_app:
                scopes = verified.scopes & USER_SCOPES if verified.scopes else USER_SCOPES
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
                agent = AgentClient(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    name=f"oauth:{verified.client_id}"[:120],
                    kind="oidc",
                    external_client_id=verified.client_id,
                    scopes=sorted(DEFAULT_AGENT_SCOPES),
                    limits={},
                    status=AgentStatus.ACTIVE.value,
                )
                session.add(agent)
                await session.flush()
            if agent.status != AgentStatus.ACTIVE.value:
                raise Unauthenticated("Agent authorization has been revoked")
            agent.last_used_at = self.runtime.clock.now()
            return Principal(
                user_id=user.id,
                actor_kind=ActorKind.AGENT,
                scopes=verified.scopes & frozenset(agent.scopes) & AGENT_GRANTABLE_SCOPES,
                auth_method="oidc",
                agent_client_id=agent.id,
                agent_name=agent.name,
                limits={k: int(v) for k, v in (agent.limits or {}).items()},
            )


async def ensure_user(session: AsyncSession, runtime: Runtime) -> User:
    """Return (creating if needed) the single development owner account."""
    settings = runtime.settings
    user = (
        await session.execute(select(User).where(User.email == settings.dev_user_email))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            id=uuid.uuid4(),
            email=settings.dev_user_email,
            display_name="Owner",
            timezone=settings.default_timezone,
            preferences={},
            sync_seq=0,
            settings_revision=1,
        )
        session.add(user)
        await session.flush()
    return user
