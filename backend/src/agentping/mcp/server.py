"""Remote MCP server (Streamable HTTP) exposing AgentPing tools.

Tools are thin adapters over the shared service layer; all authorization, validation,
limits and auditing happen in the services, identical to the REST API.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, Field, ValidationError

from agentping import __version__
from agentping.auth.principal import Principal
from agentping.auth.service import Authenticator
from agentping.domain.enums import (
    ActionKind,
    ActionSource,
    DeliveryMode,
    ReminderStatus,
)
from agentping.domain.errors import DomainError
from agentping.domain.schemas import (
    ActivityEvent,
    CancelReminderResult,
    CreateReminderResult,
    DeviceView,
    EscalationPolicy,
    NotificationButton,
    OccurrenceActionResult,
    OnceSchedule,
    Page,
    RecurringSchedule,
    ReminderCreate,
    ReminderDetail,
    ReminderUpdate,
    ReminderView,
    SendNotification,
    SendNotificationResult,
    SnoozePolicy,
    TargetSelection,
    UpdateReminderResult,
)
from agentping.mcp.metadata import MCP_DESCRIPTION, MCP_NAME
from agentping.services import activity, devices, reminders
from agentping.services.occurrences import apply_action
from agentping.services.runtime import Runtime, ServiceContext
from agentping.services.settings import get_settings
from agentping.services.views import occurrence_view

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    MCP_DESCRIPTION
    + "\n\n"
    + """\

Choosing a tool:
- Something should reach the user NOW -> send_notification.
- Something should happen LATER or REPEATEDLY -> create_reminder (once or recurring).
- Never use send_notification to emulate a schedule, and never schedule a reminder
  a few seconds ahead to emulate an immediate notification.

Delivery modes: 'normal' (ordinary notification), 'persistent' (repeats until
acknowledged, bounded), 'alarm' (rings like an alarm clock on the primary phone),
'critical' (urgent; requires the user's opt-in and the alerts:critical scope; cannot
guarantee bypassing Do Not Disturb). Prefer 'normal' unless the user asked otherwise.

Times: use ISO 8601. For one-time reminders give 'at' WITH a UTC offset, or
'local_datetime' WITHOUT offset plus an IANA 'timezone'. Recurring reminders use a
local 'start_local' plus an RFC 5545 RRULE. Call get_schedule_context for the user's
timezone and current time if unsure.

'queued'/'sync_queued' means durably stored; the push provider accepting a message is
not proof that the phone received it. Use get_reminder to see delivery state.
Always pass an idempotency_key when retrying a create/send call.
"""
)


class PrincipalAccessToken(AccessToken):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    principal: Any = None


class AgentPingTokenVerifier:
    def __init__(self, authenticator: Authenticator) -> None:
        self._authenticator = authenticator

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = await self._authenticator.authenticate(token, for_mcp=True)
        except DomainError:
            return None
        return PrincipalAccessToken(
            token="[redacted]",
            client_id=str(principal.agent_client_id or principal.device_id or principal.user_id),
            scopes=sorted(principal.scopes),
            resource=self._authenticator.runtime.settings.mcp_resource_url,
            subject=str(principal.user_id),
            principal=principal,
        )


def _principal() -> Principal:
    token = get_access_token()
    principal = getattr(token, "principal", None)
    if not isinstance(principal, Principal):
        raise ToolError("[unauthenticated] A valid bearer token is required")
    return principal


def _tool_error(error: DomainError) -> ToolError:
    text = f"[{error.code}] {error.message}"
    if error.details:
        text += " " + json.dumps(error.details, default=str)
    return ToolError(text)


def _validation_error(error: ValidationError) -> ToolError:
    problems = [
        {"field": ".".join(str(p) for p in item["loc"]) or "input", "error": item["msg"]}
        for item in error.errors(include_url=False, include_context=False)
    ]
    return ToolError("[validation_failed] " + json.dumps(problems))


def build_mcp_server(runtime: Runtime, authenticator: Authenticator) -> MCPServer:
    settings = runtime.settings
    auth = AuthSettings(
        issuer_url=settings.oidc_issuer or settings.public_base_url,  # type: ignore[arg-type]
        resource_server_url=settings.mcp_resource_url,  # type: ignore[arg-type]
        required_scopes=None,
        validate_token_resource=True,
    )
    server: MCPServer = MCPServer(
        "agentping",
        title=MCP_NAME,
        instructions=INSTRUCTIONS,
        version=__version__,
        token_verifier=AgentPingTokenVerifier(authenticator),
        auth=auth,
    )

    async def run[T](operation: Callable[[ServiceContext], Awaitable[T]]) -> T:
        principal = _principal()
        try:
            async with runtime.unit(principal, source=ActionSource.MCP) as ctx:
                return await operation(ctx)
        except DomainError as error:
            raise _tool_error(error) from error
        except ValidationError as error:
            raise _validation_error(error) from error

    def build[M](model: type[M], **values: Any) -> M:
        try:
            return model(**{k: v for k, v in values.items() if v is not None})  # type: ignore[call-arg]
        except ValidationError as error:
            raise _validation_error(error) from error

    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
    read = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(
        title="Send a notification now",
        annotations=write,
        description="Deliver a notification to the user's devices IMMEDIATELY. Use this only "
        "for things the user should see right now. For anything at a later time or on a "
        "schedule use create_reminder instead. Returns once the notification is durably "
        "queued; device delivery is tracked separately (see get_reminder).",
    )
    async def send_notification(
        title: Annotated[str, Field(description="Short headline (max 200 chars)")],
        body: Annotated[str, Field(description="Details (max 4000 chars)")] = "",
        delivery_mode: DeliveryMode = DeliveryMode.NORMAL,
        target_device_ids: Annotated[
            list[uuid.UUID] | None,
            Field(description="Specific device IDs from list_devices. Omit for the default."),
        ] = None,
        all_devices: Annotated[
            bool | None, Field(description="True to send to every device that accepts the mode")
        ] = None,
        ttl_seconds: Annotated[
            int | None, Field(description="Discard if undeliverable after this many seconds")
        ] = None,
        requires_ack: bool | None = None,
        actions: Annotated[
            list[NotificationButton] | None, Field(description="Up to 3 action buttons")
        ] = None,
        idempotency_key: Annotated[
            str | None, Field(description="Reuse when retrying to avoid duplicates")
        ] = None,
    ) -> SendNotificationResult:
        targets = _targets(target_device_ids, all_devices)
        data = build(
            SendNotification,
            title=title,
            body=body,
            delivery_mode=delivery_mode,
            targets=targets,
            ttl_seconds=ttl_seconds,
            requires_ack=requires_ack,
            actions=actions or [],
            idempotency_key=idempotency_key,
        )
        return await run(lambda ctx: reminders.send_notification(ctx, data))

    @server.tool(
        title="Create a scheduled reminder or alarm",
        annotations=write,
        description="Schedule a reminder, alarm or recurring reminder for LATER. "
        "schedule.kind='once' needs 'at' (with UTC offset) or 'local_datetime' + 'timezone'. "
        "schedule.kind='recurring' needs 'start_local' (local time, no offset), 'timezone' and "
        "an RFC 5545 'rrule' such as 'FREQ=DAILY' or 'FREQ=WEEKLY;BYDAY=MO,WE,FR', with "
        "optional 'until' or 'count'. Alarms ring on the primary phone by default. "
        "Do not use for immediate notifications (use send_notification).",
    )
    async def create_reminder(
        title: Annotated[str, Field(description="Short headline (max 200 chars)")],
        schedule: Annotated[OnceSchedule | RecurringSchedule, Field(discriminator="kind")],
        body: str = "",
        delivery_mode: DeliveryMode = DeliveryMode.NORMAL,
        targets: Annotated[
            TargetSelection | None,
            Field(
                description="Default: normal/persistent -> all devices, alarm/critical -> primary"
            ),
        ] = None,
        requires_ack: bool | None = None,
        snooze: SnoozePolicy | None = None,
        escalation: EscalationPolicy | None = None,
        expire_after_minutes: int | None = None,
        late_grace_minutes: int | None = None,
        respect_quiet_hours: bool | None = None,
        actions: list[NotificationButton] | None = None,
        idempotency_key: str | None = None,
    ) -> CreateReminderResult:
        data = build(
            ReminderCreate,
            title=title,
            body=body,
            delivery_mode=delivery_mode,
            schedule=schedule,
            targets=targets,
            requires_ack=requires_ack,
            snooze=snooze,
            escalation=escalation,
            expire_after_minutes=expire_after_minutes,
            late_grace_minutes=late_grace_minutes,
            respect_quiet_hours=respect_quiet_hours,
            actions=actions or [],
            idempotency_key=idempotency_key,
        )
        return await run(lambda ctx: reminders.create_reminder(ctx, data))

    @server.tool(
        title="Update a reminder",
        annotations=write,
        description="Partially update a reminder YOU created. expected_revision must equal the "
        "reminder's current revision (from get_reminder/list_reminders); on a revision_conflict "
        "error re-read the reminder and retry. status='paused' suspends it, 'active' resumes. "
        "Schedule changes replace future occurrences; already-ringing ones are unaffected.",
    )
    async def update_reminder(
        reminder_id: uuid.UUID,
        expected_revision: int,
        title: str | None = None,
        body: str | None = None,
        delivery_mode: DeliveryMode | None = None,
        schedule: Annotated[
            OnceSchedule | RecurringSchedule | None, Field(discriminator="kind")
        ] = None,
        targets: TargetSelection | None = None,
        requires_ack: bool | None = None,
        snooze: SnoozePolicy | None = None,
        escalation: EscalationPolicy | None = None,
        expire_after_minutes: int | None = None,
        late_grace_minutes: int | None = None,
        respect_quiet_hours: bool | None = None,
        actions: list[NotificationButton] | None = None,
        status: Literal["active", "paused"] | None = None,
    ) -> UpdateReminderResult:
        data = build(
            ReminderUpdate,
            expected_revision=expected_revision,
            title=title,
            body=body,
            delivery_mode=delivery_mode,
            schedule=schedule,
            targets=targets,
            requires_ack=requires_ack,
            snooze=snooze,
            escalation=escalation,
            expire_after_minutes=expire_after_minutes,
            late_grace_minutes=late_grace_minutes,
            respect_quiet_hours=respect_quiet_hours,
            actions=actions,
            status=status,
        )
        return await run(lambda ctx: reminders.update_reminder(ctx, reminder_id, data))

    @server.tool(
        title="Cancel a reminder",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True
        ),
        description="Cancel a reminder YOU created: all future and ringing occurrences are "
        "cancelled and removed from the user's phones. Cannot be undone.",
    )
    async def cancel_reminder(
        reminder_id: uuid.UUID,
        expected_revision: int | None = None,
        reason: str | None = None,
    ) -> CancelReminderResult:
        return await run(
            lambda ctx: reminders.cancel_reminder(
                ctx, reminder_id, expected_revision=expected_revision, reason=reason
            )
        )

    @server.tool(
        title="List reminders",
        annotations=read,
        description="List reminders, newest first. Filter by status, delivery mode, device, "
        "source agent or next-occurrence window. Use next_cursor for more pages.",
    )
    async def list_reminders(
        status: list[ReminderStatus] | None = None,
        delivery_mode: DeliveryMode | None = None,
        device_id: uuid.UUID | None = None,
        source_agent_id: uuid.UUID | None = None,
        due_after: datetime | None = None,
        due_before: datetime | None = None,
        search: str | None = None,
        include_notifications: bool = False,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> Page[ReminderView]:
        return await run(
            lambda ctx: reminders.list_reminders(
                ctx,
                statuses=status,
                mode=delivery_mode,
                device_id=device_id,
                source_agent_id=source_agent_id,
                due_after=due_after,
                due_before=due_before,
                query=search,
                include_notifications=include_notifications,
                cursor=cursor,
                limit=limit,
            )
        )

    @server.tool(
        title="Get reminder details",
        annotations=read,
        description="Return a reminder with its upcoming occurrences (IDs needed for "
        "snooze/acknowledge/skip) and recent occurrences with per-device delivery state "
        "(provider_accepted is NOT device receipt; device_received/displayed are).",
    )
    async def get_reminder(
        reminder_id: uuid.UUID,
        upcoming_limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> ReminderDetail:
        return await run(
            lambda ctx: reminders.get_reminder(ctx, reminder_id, upcoming_limit=upcoming_limit)
        )

    async def occurrence_action(
        occurrence_id: uuid.UUID,
        action: ActionKind,
        *,
        idempotency_key: str | None,
        minutes: int | None = None,
        reason: str | None = None,
        note: str | None = None,
    ) -> OccurrenceActionResult:
        async def operation(ctx: ServiceContext) -> OccurrenceActionResult:
            outcome = await apply_action(
                ctx,
                occurrence_id,
                action,
                source=ActionSource.MCP,
                client_action_id=idempotency_key,
                snooze_minutes=minutes,
                reason=reason,
                note=note,
            )
            return OccurrenceActionResult(
                result=outcome.result,
                occurrence=occurrence_view(outcome.occurrence),
                reason=outcome.reason,
            )

        return await run(operation)

    @server.tool(
        title="Snooze one occurrence",
        annotations=write,
        description="Postpone ONE occurrence by N minutes. The rest of a recurring series is "
        "unchanged. Get occurrence IDs from get_reminder.",
    )
    async def snooze_occurrence(
        occurrence_id: uuid.UUID,
        minutes: Annotated[int | None, Field(ge=1, le=720)] = None,
        idempotency_key: str | None = None,
    ) -> OccurrenceActionResult:
        return await occurrence_action(
            occurrence_id, ActionKind.SNOOZE, idempotency_key=idempotency_key, minutes=minutes
        )

    @server.tool(
        title="Acknowledge one occurrence",
        annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
        description="Mark ONE occurrence as acknowledged/done on the user's behalf (only "
        "for reminders you created). Stops it ringing on all phones. Repeating the call "
        "is safe. Stale acknowledgements never revive cancelled reminders.",
    )
    async def acknowledge_occurrence(
        occurrence_id: uuid.UUID,
        note: Annotated[str | None, Field(max_length=500)] = None,
        idempotency_key: str | None = None,
    ) -> OccurrenceActionResult:
        return await occurrence_action(
            occurrence_id, ActionKind.ACKNOWLEDGE, idempotency_key=idempotency_key, note=note
        )

    @server.tool(
        title="Skip one occurrence",
        annotations=write,
        description="Skip/dismiss ONE occurrence with an explicit reason; the series continues.",
    )
    async def skip_occurrence(
        occurrence_id: uuid.UUID,
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        idempotency_key: str | None = None,
    ) -> OccurrenceActionResult:
        return await occurrence_action(
            occurrence_id, ActionKind.SKIP, idempotency_key=idempotency_key, reason=reason
        )

    @server.tool(
        title="List devices",
        annotations=read,
        description="List the user's registered devices with capabilities, primary status, "
        "last sync time and health (missing permissions, stale, unreachable).",
    )
    async def list_devices() -> list[DeviceView]:
        return await run(lambda ctx: devices.list_devices(ctx))

    @server.tool(
        title="Get activity history",
        annotations=read,
        description="Paginated history (newest first) of reminder changes, notifications, "
        "deliveries, acknowledgements, failures and agent actions. Categories: reminder, "
        "notification, action, delivery, device, settings, security, agent, error.",
    )
    async def get_activity(
        categories: list[str] | None = None,
        reminder_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> Page[ActivityEvent]:
        return await run(
            lambda ctx: activity.list_activity(
                ctx,
                categories=categories,
                reminder_id=reminder_id,
                device_id=device_id,
                agent_id=agent_id,
                since=since,
                until=until,
                cursor=cursor,
                limit=limit,
            )
        )

    @server.tool(
        title="Get scheduling context",
        annotations=read,
        description="Return the current server time, the user's home timezone and quiet hours. "
        "Use before scheduling when the user's timezone is unknown.",
    )
    async def get_schedule_context() -> dict[str, Any]:
        async def operation(ctx: ServiceContext) -> dict[str, Any]:
            view = await get_settings(ctx)
            local = ctx.now.astimezone(ZoneInfo(view.timezone))
            return {
                "server_time_utc": ctx.now.isoformat(),
                "user_timezone": view.timezone,
                "user_local_time": local.isoformat(),
                "quiet_hours": view.preferences.quiet_hours.model_dump(mode="json"),
                "critical_alerts_enabled": view.preferences.critical_alerts_enabled,
            }

        return await run(operation)

    return server


def _targets(
    device_ids: list[uuid.UUID] | None, all_devices: bool | None
) -> TargetSelection | None:
    if device_ids:
        if all_devices:
            raise ToolError("[validation_failed] Use either target_device_ids or all_devices")
        return TargetSelection(strategy="devices", device_ids=device_ids)  # type: ignore[arg-type]
    if all_devices:
        return TargetSelection(strategy="all")  # type: ignore[arg-type]
    return None


def transport_security(runtime: Runtime) -> TransportSecuritySettings:
    """DNS-rebinding protection; hosts default to the public base URL's host."""
    settings = runtime.settings
    hosts = list(settings.mcp_allowed_hosts)
    origins = list(settings.mcp_allowed_origins)
    if not hosts:
        parsed = urlsplit(settings.public_base_url)
        if parsed.hostname:
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            hosts = [f"{host}:*", host]
            origins = origins or [f"{parsed.scheme}://{host}:*", f"{parsed.scheme}://{host}"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )
