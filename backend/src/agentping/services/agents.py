"""Agent client authorizations: scoped, revocable, attributable credentials."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from agentping.auth import tokens
from agentping.auth.principal import AGENT_GRANTABLE_SCOPES, Scope
from agentping.db.models import AccessToken, AgentClient
from agentping.domain.enums import AgentStatus
from agentping.domain.errors import Conflict, Forbidden, NotFound, ValidationFailed
from agentping.domain.schemas import (
    AgentCreate,
    AgentTokenIssued,
    AgentTokenView,
    AgentUpdate,
    AgentView,
)
from agentping.services.core import audit
from agentping.services.runtime import ServiceContext

LIMIT_FIELDS = ("max_active_reminders", "max_ringing_reminders", "max_sends_per_hour")


def _require_manager(ctx: ServiceContext) -> None:
    if ctx.principal.is_agent:
        raise Forbidden("Agents cannot manage agent authorizations")
    ctx.principal.require(Scope.AGENTS_MANAGE)


def _validate_scopes(scopes: list[str]) -> list[str]:
    invalid = sorted(set(scopes) - AGENT_GRANTABLE_SCOPES)
    if invalid:
        raise ValidationFailed(
            f"Scopes cannot be granted to agents: {', '.join(invalid)}",
            details={"grantable": sorted(AGENT_GRANTABLE_SCOPES)},
        )
    return sorted(set(scopes))


async def _view(ctx: ServiceContext, agent: AgentClient) -> AgentView:
    token_rows = (
        await ctx.session.execute(
            select(AccessToken)
            .where(AccessToken.agent_client_id == agent.id)
            .order_by(AccessToken.created_at.desc())
        )
    ).scalars()
    return AgentView(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        kind=agent.kind,
        scopes=list(agent.scopes),
        status=agent.status,
        limits={k: int(v) for k, v in (agent.limits or {}).items()},
        created_at=agent.created_at,
        revoked_at=agent.revoked_at,
        last_used_at=agent.last_used_at,
        tokens=[
            AgentTokenView(
                id=t.id,
                token_prefix=t.token_prefix,
                scopes=list(t.scopes),
                created_at=t.created_at,
                expires_at=t.expires_at,
                revoked_at=t.revoked_at,
                last_used_at=t.last_used_at,
            )
            for t in token_rows
        ],
    )


async def _load(ctx: ServiceContext, agent_id: uuid.UUID) -> AgentClient:
    agent = (
        await ctx.session.execute(
            select(AgentClient).where(
                AgentClient.id == agent_id, AgentClient.user_id == ctx.user_id
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise NotFound("Agent not found", details={"agent_id": str(agent_id)})
    return agent


async def _issue(ctx: ServiceContext, agent: AgentClient, ttl_days: int | None) -> AgentTokenIssued:
    raw = tokens.generate_token(tokens.AGENT_PREFIX)
    expires_at = ctx.now + timedelta(days=ttl_days) if ttl_days else None
    ctx.session.add(
        AccessToken(
            user_id=ctx.user_id,
            agent_client_id=agent.id,
            token_prefix=tokens.display_prefix(raw),
            token_hash=tokens.hash_secret(raw),
            scopes=list(agent.scopes),
            label=f"agent:{agent.name}"[:120],
            expires_at=expires_at,
        )
    )
    await ctx.session.flush()
    await audit(
        ctx,
        "agent.token_issued",
        f"Token issued for agent '{agent.name}'",
        category="security",
        details={"agent_id": str(agent.id), "scopes": list(agent.scopes)},
    )
    return AgentTokenIssued(agent=await _view(ctx, agent), token=raw, expires_at=expires_at)


async def create_agent(ctx: ServiceContext, data: AgentCreate) -> AgentTokenIssued:
    _require_manager(ctx)
    await ctx.lock_user()
    agent = AgentClient(
        id=uuid.uuid4(),
        user_id=ctx.user_id,
        name=data.name,
        description=data.description,
        kind="token",
        scopes=_validate_scopes(data.scopes),
        limits={k: v for k in LIMIT_FIELDS if (v := getattr(data, k)) is not None},
        status=AgentStatus.ACTIVE.value,
    )
    ctx.session.add(agent)
    try:
        async with ctx.session.begin_nested():
            await ctx.session.flush()
    except IntegrityError as error:
        raise Conflict(f"An agent named '{data.name}' already exists") from error
    await audit(
        ctx,
        "agent.created",
        f"Agent '{agent.name}' authorized with scopes {', '.join(agent.scopes)}",
        category="agent",
        details={"agent_id": str(agent.id), "scopes": list(agent.scopes)},
    )
    return await _issue(ctx, agent, data.token_ttl_days)


async def list_agents(ctx: ServiceContext) -> list[AgentView]:
    _require_manager(ctx)
    rows = (
        await ctx.session.execute(
            select(AgentClient)
            .where(AgentClient.user_id == ctx.user_id)
            .order_by(AgentClient.created_at)
        )
    ).scalars()
    return [await _view(ctx, agent) for agent in rows]


async def update_agent(ctx: ServiceContext, agent_id: uuid.UUID, data: AgentUpdate) -> AgentView:
    _require_manager(ctx)
    await ctx.lock_user()
    agent = await _load(ctx, agent_id)
    if agent.status != AgentStatus.ACTIVE.value:
        raise Conflict("Revoked agents cannot be modified")
    if data.scopes is not None:
        agent.scopes = _validate_scopes(data.scopes)
        await ctx.session.execute(
            update(AccessToken)
            .where(AccessToken.agent_client_id == agent.id)
            .values(scopes=agent.scopes)
        )
    if data.description is not None:
        agent.description = data.description
    limits = dict(agent.limits or {})
    for key in LIMIT_FIELDS:
        value = getattr(data, key)
        if value is not None:
            limits[key] = value
    agent.limits = limits
    await audit(
        ctx,
        "agent.updated",
        f"Agent '{agent.name}' updated (scopes: {', '.join(agent.scopes)})",
        category="agent",
        details={"agent_id": str(agent.id), "scopes": list(agent.scopes), "limits": limits},
    )
    return await _view(ctx, agent)


async def issue_token(
    ctx: ServiceContext, agent_id: uuid.UUID, ttl_days: int | None = 90
) -> AgentTokenIssued:
    _require_manager(ctx)
    await ctx.lock_user()
    agent = await _load(ctx, agent_id)
    if agent.status != AgentStatus.ACTIVE.value:
        raise Conflict("Revoked agents cannot receive tokens")
    return await _issue(ctx, agent, ttl_days)


async def revoke_agent(ctx: ServiceContext, agent_id: uuid.UUID) -> AgentView:
    _require_manager(ctx)
    await ctx.lock_user()
    agent = await _load(ctx, agent_id)
    if agent.status != AgentStatus.REVOKED.value:
        agent.status = AgentStatus.REVOKED.value
        agent.revoked_at = ctx.now
        await ctx.session.execute(
            update(AccessToken)
            .where(AccessToken.agent_client_id == agent.id, AccessToken.revoked_at.is_(None))
            .values(revoked_at=ctx.now)
        )
        await audit(
            ctx,
            "agent.revoked",
            f"Agent '{agent.name}' revoked; all its tokens are invalid",
            category="security",
            details={"agent_id": str(agent.id)},
        )
    return await _view(ctx, agent)


async def revoke_token(ctx: ServiceContext, token_id: uuid.UUID) -> None:
    _require_manager(ctx)
    token = (
        await ctx.session.execute(
            select(AccessToken).where(
                AccessToken.id == token_id,
                AccessToken.user_id == ctx.user_id,
                AccessToken.agent_client_id.is_not(None),
            )
        )
    ).scalar_one_or_none()
    if token is None:
        raise NotFound("Token not found")
    if token.revoked_at is None:
        token.revoked_at = ctx.now
        await audit(
            ctx,
            "agent.token_revoked",
            f"Agent token {token.token_prefix}… revoked",
            category="security",
            details={"token_id": str(token.id)},
        )


async def reauthorize_agent(ctx: ServiceContext, agent_id: uuid.UUID) -> AgentView:
    """Explicit owner approval permits new OAuth tokens; old tokens stay invalid."""
    _require_manager(ctx)
    await ctx.lock_user()
    agent = await _load(ctx, agent_id)
    if agent.kind != "oidc":
        raise Conflict("Create a new credential for a revoked token agent")
    if agent.status == AgentStatus.REVOKED.value:
        agent.status = AgentStatus.ACTIVE.value
        agent.revoked_at = None
        agent.oauth_not_before = int(ctx.now.timestamp()) + 1
        await audit(
            ctx,
            "agent.reauthorized",
            f"New OAuth login allowed for '{agent.name}'",
            category="security",
            details={"agent_id": str(agent.id)},
        )
    return await _view(ctx, agent)
