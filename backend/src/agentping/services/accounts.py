"""Account profile and closure; identity links are preserved to prevent recreation bypass."""

from __future__ import annotations

from sqlalchemy import delete, select, update

from agentping.auth.principal import Scope
from agentping.db.models import AccessToken, AgentClient, Device, PortalSession, Reminder
from agentping.domain.enums import ReminderStatus
from agentping.domain.errors import Forbidden
from agentping.services import reminders
from agentping.services.core import audit
from agentping.services.runtime import ServiceContext


async def close_account(ctx: ServiceContext) -> None:
    if ctx.principal.is_agent or ctx.principal.device_id:
        raise Forbidden("Account closure requires your signed-in account")
    ctx.principal.require(Scope.SETTINGS_WRITE, Scope.AGENTS_MANAGE, Scope.DEVICES_MANAGE)
    user = await ctx.lock_user()
    pending = list(
        (
            await ctx.session.execute(
                select(Reminder.id).where(
                    Reminder.user_id == ctx.user_id, Reminder.status == ReminderStatus.ACTIVE.value
                )
            )
        ).scalars()
    )
    for reminder_id in pending:
        await reminders.cancel_reminder(ctx, reminder_id, reason="account_closed")
    await ctx.session.execute(
        update(AccessToken)
        .where(AccessToken.user_id == ctx.user_id, AccessToken.revoked_at.is_(None))
        .values(revoked_at=ctx.now)
    )
    await ctx.session.execute(
        update(AgentClient)
        .where(AgentClient.user_id == ctx.user_id)
        .values(status="revoked", revoked_at=ctx.now)
    )
    await ctx.session.execute(
        update(Device).where(Device.user_id == ctx.user_id).values(status="deactivated")
    )
    await ctx.session.execute(delete(PortalSession).where(PortalSession.user_id == ctx.user_id))
    user.status = "deleted"
    await audit(
        ctx,
        "account.closed",
        "Account closed; credentials revoked and reminders cancelled",
        category="security",
    )
