"""Quiet-hours evaluation in the user's local timezone."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agentping.domain.scheduling import combine_local
from agentping.domain.schemas import QuietHours


def quiet_hours_end(quiet: QuietHours, tz: ZoneInfo, at: datetime) -> datetime | None:
    """Return the UTC instant quiet hours end if ``at`` is inside them, else None.

    Windows may span midnight (22:00-07:00). start == end means "never quiet".
    """
    if not quiet.enabled or quiet.start == quiet.end:
        return None
    local = at.astimezone(tz)
    clock = local.timetz().replace(tzinfo=None)
    today = local.date()
    if quiet.start < quiet.end:
        inside = quiet.start <= clock < quiet.end
        end_date = today
    else:
        inside = clock >= quiet.start or clock < quiet.end
        end_date = today + timedelta(days=1) if clock >= quiet.start else today
    if not inside:
        return None
    return combine_local(end_date, quiet.end, tz)
