"""Recurrence, DST and calendar edge cases (pure functions, no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentping.domain.enums import MonthlyDayPolicy
from agentping.domain.errors import AmbiguousDateTime, InvalidSchedule
from agentping.domain.scheduling import (
    RecurrenceSpec,
    combine_local,
    instances_between,
    localize,
    occurrence_id,
    parse_local_datetime,
    parse_rrule,
    require_aware,
    validate_recurrence,
)

NY = ZoneInfo("America/New_York")
KOLKATA = "Asia/Kolkata"


def _locals(spec: RecurrenceSpec, start: datetime, days: int, limit: int = 100) -> list[str]:
    instances = instances_between(spec, start, start + timedelta(days=days), limit)
    return [i.local_wall.isoformat() for i in instances]


def test_daily_rule_in_fixed_offset_zone() -> None:
    spec = RecurrenceSpec(datetime(2026, 1, 1, 9, 0), KOLKATA, "FREQ=DAILY")
    instances = instances_between(
        spec, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 4, tzinfo=UTC), 10
    )
    assert [i.original_at for i in instances] == [
        datetime(2026, 1, d, 3, 30, tzinfo=UTC) for d in (1, 2, 3)
    ]


def test_weekday_rule() -> None:
    spec = RecurrenceSpec(datetime(2026, 3, 2, 8, 0), "UTC", "FREQ=WEEKLY;BYDAY=MO,WE,FR")
    walls = _locals(spec, datetime(2026, 3, 2, tzinfo=UTC), 7)
    assert walls == ["2026-03-02T08:00:00", "2026-03-04T08:00:00", "2026-03-06T08:00:00"]


def test_spring_forward_gap_shifts_forward_by_gap_length() -> None:
    # 2026-03-08 02:30 does not exist in New York; RFC 5545 shifts it to 03:30 EDT.
    spec = RecurrenceSpec(datetime(2026, 3, 7, 2, 30), "America/New_York", "FREQ=DAILY")
    instances = instances_between(
        spec, datetime(2026, 3, 7, tzinfo=UTC), datetime(2026, 3, 10, tzinfo=UTC), 5
    )
    gap = [i for i in instances if i.local_wall.day == 8][0]
    assert gap.adjusted_for_gap is True
    assert gap.original_at == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)  # 03:30 EDT
    assert gap.original_at.astimezone(NY).hour == 3
    after = [i for i in instances if i.local_wall.day == 9][0]
    assert after.original_at.astimezone(NY).time() == time(2, 30)


def test_fall_back_ambiguous_time_fires_once_at_first_instance() -> None:
    spec = RecurrenceSpec(datetime(2026, 10, 31, 1, 30), "America/New_York", "FREQ=DAILY")
    instances = instances_between(
        spec, datetime(2026, 10, 31, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC), 5
    )
    nov1 = [i for i in instances if i.local_wall.day == 1]
    assert len(nov1) == 1
    assert nov1[0].original_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT


def test_localize_reports_gap_and_ambiguity() -> None:
    gap = localize(datetime(2026, 3, 8, 2, 30), NY)
    assert gap.adjusted_for_gap and not gap.ambiguous
    fold = localize(datetime(2026, 11, 1, 1, 30), NY)
    assert fold.ambiguous and not fold.adjusted_for_gap
    assert fold.instant.utcoffset() == timedelta(0)
    assert fold.instant == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_monthly_31st_skip_policy_follows_rfc5545() -> None:
    spec = RecurrenceSpec(datetime(2026, 1, 31, 9, 0), "UTC", "FREQ=MONTHLY")
    walls = _locals(spec, datetime(2026, 1, 1, tzinfo=UTC), 220)
    assert [w[:10] for w in walls] == [
        "2026-01-31",
        "2026-03-31",
        "2026-05-31",
        "2026-07-31",
    ]


def test_monthly_31st_clamp_policy_uses_last_day() -> None:
    spec = RecurrenceSpec(
        datetime(2026, 1, 31, 9, 0),
        "UTC",
        "FREQ=MONTHLY",
        monthly_day_policy=MonthlyDayPolicy.CLAMP,
    )
    walls = _locals(spec, datetime(2026, 1, 1, tzinfo=UTC), 120)
    assert [w[:10] for w in walls] == ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]


def test_leap_day_yearly_skip_and_clamp() -> None:
    start = datetime(2028, 2, 29, 7, 0)
    skip = RecurrenceSpec(start, "UTC", "FREQ=YEARLY")
    walls = _locals(skip, datetime(2028, 1, 1, tzinfo=UTC), 366 * 5)
    assert [w[:10] for w in walls] == ["2028-02-29", "2032-02-29"]
    clamp = RecurrenceSpec(start, "UTC", "FREQ=YEARLY", monthly_day_policy=MonthlyDayPolicy.CLAMP)
    walls = _locals(clamp, datetime(2028, 1, 1, tzinfo=UTC), 366 * 3)
    assert [w[:10] for w in walls] == ["2028-02-29", "2029-02-28", "2030-02-28"]


def test_count_and_until_limit_instances() -> None:
    counted = RecurrenceSpec(datetime(2026, 1, 1, 9), "UTC", "FREQ=DAILY;COUNT=3")
    assert len(_locals(counted, datetime(2026, 1, 1, tzinfo=UTC), 30)) == 3
    until = RecurrenceSpec(datetime(2026, 1, 1, 9), "UTC", "FREQ=DAILY;UNTIL=20260103T235959Z")
    assert len(_locals(until, datetime(2026, 1, 1, tzinfo=UTC), 30)) == 3


@pytest.mark.parametrize(
    "rule",
    [
        "DTSTART:20260101T000000\nRRULE:FREQ=DAILY",
        "FREQ=SECONDLY",
        "FREQ=DAILY;BYSECOND=5",
        "FREQ=DAILY;COUNT=2;UNTIL=20260105",
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=DAILY;FREQ=WEEKLY",
        "INTERVAL=2",
        "FREQ=DAILY;X-FOO=1",
    ],
)
def test_invalid_rules_rejected(rule: str) -> None:
    with pytest.raises(InvalidSchedule):
        parse_rrule(rule, ZoneInfo("UTC"))


def test_minimum_interval_enforced() -> None:
    spec = RecurrenceSpec(datetime(2026, 1, 1, 9), "UTC", "FREQ=MINUTELY;INTERVAL=1")
    with pytest.raises(InvalidSchedule, match="minimum"):
        validate_recurrence(spec, now=datetime(2026, 1, 1, tzinfo=UTC), min_interval_seconds=300)
    ok = RecurrenceSpec(datetime(2026, 1, 1, 9), "UTC", "FREQ=MINUTELY;INTERVAL=5")
    assert (
        validate_recurrence(ok, now=datetime(2026, 1, 1, tzinfo=UTC), min_interval_seconds=300)
        == []
    )


def test_schedule_entirely_in_past_rejected() -> None:
    spec = RecurrenceSpec(datetime(2025, 1, 1, 9), "UTC", "FREQ=DAILY;COUNT=2")
    with pytest.raises(InvalidSchedule, match="future"):
        validate_recurrence(spec, now=datetime(2026, 1, 1, tzinfo=UTC), min_interval_seconds=300)


def test_skip_policy_warns_about_short_months() -> None:
    spec = RecurrenceSpec(datetime(2026, 1, 31, 9), "UTC", "FREQ=MONTHLY")
    warnings = validate_recurrence(
        spec, now=datetime(2026, 1, 1, tzinfo=UTC), min_interval_seconds=300
    )
    assert any("clamp" in w for w in warnings)


def test_dst_gap_warning() -> None:
    spec = RecurrenceSpec(datetime(2026, 3, 1, 2, 30), "America/New_York", "FREQ=DAILY")
    warnings = validate_recurrence(
        spec, now=datetime(2026, 3, 1, tzinfo=UTC), min_interval_seconds=300
    )
    assert any("DST" in w for w in warnings)


def test_occurrence_ids_are_deterministic() -> None:
    reminder = uuid.uuid4()
    at = datetime(2026, 1, 1, 9, tzinfo=UTC)
    assert occurrence_id(reminder, at) == occurrence_id(reminder, at)
    assert occurrence_id(reminder, at) == occurrence_id(reminder, at.astimezone(NY))
    assert occurrence_id(reminder, at) != occurrence_id(reminder, at + timedelta(days=1))


def test_local_and_aware_datetime_validation() -> None:
    with pytest.raises(AmbiguousDateTime):
        parse_local_datetime("2026-01-01T09:00:00+05:30", "start_local")
    assert parse_local_datetime("2026-01-01T09:00:00", "start_local") == datetime(2026, 1, 1, 9)
    with pytest.raises(AmbiguousDateTime):
        require_aware(datetime(2026, 1, 1, 9), "at")
    assert require_aware(datetime(2026, 1, 1, 9, tzinfo=NY), "at").tzinfo is UTC


def test_combine_local_handles_gap() -> None:
    value = combine_local(date(2026, 3, 8), time(2, 15), NY)
    assert value.astimezone(NY).time() == time(3, 15)
