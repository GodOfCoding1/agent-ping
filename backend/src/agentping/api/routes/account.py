"""Activity, settings and agent-authorization endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from agentping.api.deps import Call
from agentping.api.errors import ERROR_RESPONSES
from agentping.domain.schemas import (
    ActivityEvent,
    AgentCreate,
    AgentTokenIssued,
    AgentUpdate,
    AgentView,
    Page,
    SettingsUpdate,
    SettingsView,
)
from agentping.services import activity, agents, settings

router = APIRouter(responses=ERROR_RESPONSES)


@router.get("/activity", response_model=Page[ActivityEvent], tags=["activity"])
async def list_activity(
    call: Call,
    category: Annotated[list[str] | None, Query()] = None,
    reminder_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    event_type: Annotated[str | None, Query(max_length=60)] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[ActivityEvent]:
    return await call(
        activity.list_activity,
        categories=category,
        reminder_id=reminder_id,
        device_id=device_id,
        agent_id=agent_id,
        event_type=event_type,
        since=since,
        until=until,
        cursor=cursor,
        limit=limit,
    )


@router.get("/settings", response_model=SettingsView, tags=["settings"])
async def get_settings(call: Call) -> SettingsView:
    return await call(settings.get_settings)


@router.patch(
    "/settings",
    response_model=SettingsView,
    tags=["settings"],
    summary="Update user preferences (quiet hours, critical opt-in, timezone, privacy)",
)
async def update_settings(body: SettingsUpdate, call: Call) -> SettingsView:
    return await call(settings.update_settings, body)


@router.post(
    "/agents",
    response_model=AgentTokenIssued,
    status_code=status.HTTP_201_CREATED,
    tags=["agents"],
    summary="Authorize a new agent and issue its first token",
)
async def create_agent(body: AgentCreate, call: Call) -> AgentTokenIssued:
    return await call(agents.create_agent, body)


@router.get("/agents", response_model=list[AgentView], tags=["agents"])
async def list_agents(call: Call) -> list[AgentView]:
    return await call(agents.list_agents)


@router.patch("/agents/{agent_id}", response_model=AgentView, tags=["agents"])
async def update_agent(agent_id: uuid.UUID, body: AgentUpdate, call: Call) -> AgentView:
    return await call(agents.update_agent, agent_id, body)


@router.post("/agents/{agent_id}/revoke", response_model=AgentView, tags=["agents"])
async def revoke_agent(agent_id: uuid.UUID, call: Call) -> AgentView:
    return await call(agents.revoke_agent, agent_id)


@router.post(
    "/agents/{agent_id}/tokens",
    response_model=AgentTokenIssued,
    status_code=status.HTTP_201_CREATED,
    tags=["agents"],
)
async def issue_agent_token(
    agent_id: uuid.UUID,
    call: Call,
    ttl_days: Annotated[int | None, Query(ge=1, le=3650)] = 90,
) -> AgentTokenIssued:
    return await call(agents.issue_token, agent_id, ttl_days)


@router.delete("/agents/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["agents"])
async def revoke_agent_token(token_id: uuid.UUID, call: Call) -> Response:
    await call(agents.revoke_token, token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
