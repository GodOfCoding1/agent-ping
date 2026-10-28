"""Recurrence engine.

Semantics (documented in docs/scheduling.md):

* Recurrences are expanded on *local wall-clock* datetimes using RFC 5545 RRULE rules
  (python-dateutil), never by adding fixed 24-hour durations.
* Each local instance is converted to an absolute UTC instant:
  - Nonexistent local times (DST gap) use the UTC offset in effect before the gap, which
    moves the instant forward by the gap length (02:30 in a 02:00-03:00 gap fires at 03:30),
    matching RFC 5545 section 3.3.5.
  - Ambiguous local times (DST overlap) resolve to the first (earlier) instant.
* Monthly/yearly rules that land on dates that do not exist in some months (31st, Feb 29)
  follow RFC 5545 and skip those periods by default. ``monthly_day_policy=clamp`` instead
  moves them to the last day of the shorter month.
* Occurrence IDs are UUIDv5 values derived from the reminder ID and the instance's original
  UTC instant, so recomputation always yields the same ID.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrule, rrulestr

from agentping.domain.enums import MonthlyDayPolicy
from agentping.domain.errors import AmbiguousDateTime, InvalidSchedule

OCCURRENCE_NAMESPACE = uuid.UUID("6f1d3a4e-8c1b-5b8e-9a51-0c5e2b7f4d10")

_ALLOWED_PARTS = {
    "FREQ",
    "INTERVAL",
    "COUNT",
    "UNTIL",
    "BYMINUTE",
    "BYHOUR",
    "BYDAY",
    "BYMONTHDAY",
    "BYYEARDAY",
    "BYWEEKNO",
    "BYMONTH",
    "BYSETPOS",
    "WKST",
}
_ALLOWED_FREQ = {"MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
_UNTIL_RE = re.compile(r"^(\d{8})(T(\d{6})(Z)?)?$")
MAX_COUNT = 10_000


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise InvalidSchedule(
            f"Unknown IANA timezone {name!r}; use a name such as 'Asia/Kolkata' or 'UTC'",
            details={"field": "timezone"},
        ) from error


@dataclass(frozen=True)
class LocalResolution:
    instant: datetime
    adjusted_for_gap: bool
    ambiguous: bool


def localize(local: datetime, tz: ZoneInfo) -> LocalResolution:
    """Resolve a naive local wall time to a UTC instant using the documented DST policy."""
    if local.tzinfo is not None:
        raise ValueError("localize expects a naive local datetime")
    first = local.replace(tzinfo=tz, fold=0)
    second = local.replace(tzinfo=tz, fold=1)
    instant = first.astimezone(UTC)
    round_trip = instant.astimezone(tz).replace(tzinfo=None)
    adjusted = round_trip != local
    ambiguous = not adjusted and first.utcoffset() != second.utcoffset()
    return LocalResolution(instant=instant, adjusted_for_gap=adjusted, ambiguous=ambiguous)


def combine_local(day: date, clock: time, tz: ZoneInfo) -> datetime:
    return localize(datetime.combine(day, clock), tz).instant


def to_local_naive(instant: datetime, tz: ZoneInfo) -> datetime:
    return instant.astimezone(tz).replace(tzinfo=None)


def occurrence_id(reminder_id: uuid.UUID, original_at: datetime) -> uuid.UUID:
    stamp = original_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return uuid.uuid5(OCCURRENCE_NAMESPACE, f"{reminder_id}/{stamp}")


@dataclass(frozen=True)
class ParsedRule:
    """A validated RRULE without DTSTART, COUNT or UNTIL (those are carried separately)."""

    parts: dict[str, str]
    count: int | None
    until_local: datetime | None
    until_utc: datetime | None

    @property
    def freq(self) -> str:
        return self.parts["FREQ"]

    def normalized(self) -> str:
        ordered = ["FREQ"] + sorted(key for key in self.parts if key != "FREQ")
        return ";".join(f"{key}={self.parts[key]}" for key in ordered)


def parse_rrule(text: str, tz: ZoneInfo) -> ParsedRule:
    raw = text.strip()
    if raw.upper().startswith("RRULE:"):
        raw = raw[6:]
    if not raw or "\n" in raw or "DTSTART" in raw.upper():
        raise InvalidSchedule(
            "rrule must be a single RFC 5545 RRULE value such as 'FREQ=WEEKLY;BYDAY=MO,WE'; "
            "DTSTART is provided separately via start_local",
            details={"field": "rrule"},
        )
    parts: dict[str, str] = {}
    for piece in raw.split(";"):
        if not piece:
            continue
        if "=" not in piece:
            raise InvalidSchedule(f"Malformed RRULE part {piece!r}", details={"field": "rrule"})
        key, value = piece.split("=", 1)
        key = key.strip().upper()
        value = value.strip().upper()
        if key not in _ALLOWED_PARTS:
            raise InvalidSchedule(
                f"Unsupported RRULE part {key!r}", details={"field": "rrule", "part": key}
            )
        if key in parts:
            raise InvalidSchedule(f"Duplicate RRULE part {key!r}", details={"field": "rrule"})
        parts[key] = value
    if parts.get("FREQ") not in _ALLOWED_FREQ:
        raise InvalidSchedule(
            "RRULE FREQ must be one of " + ", ".join(sorted(_ALLOWED_FREQ)),
            details={"field": "rrule"},
        )
    count: int | None = None
    until_local: datetime | None = None
    until_utc: datetime | None = None
    if "COUNT" in parts:
        try:
            count = int(parts.pop("COUNT"))
        except ValueError as error:
            raise InvalidSchedule("RRULE COUNT must be an integer") from error
    if "UNTIL" in parts:
        match = _UNTIL_RE.match(parts.pop("UNTIL"))
        if not match:
            raise InvalidSchedule("RRULE UNTIL must be YYYYMMDD or YYYYMMDDTHHMMSS[Z]")
        day = datetime.strptime(match.group(1), "%Y%m%d")
        clock = match.group(3) or "235959"
        local = day.replace(hour=int(clock[:2]), minute=int(clock[2:4]), second=int(clock[4:]))
        if match.group(4):
            until_utc = local.replace(tzinfo=UTC)
            until_local = to_local_naive(until_utc, tz)
        else:
            until_local = local
            until_utc = localize(local, tz).instant
    if count is not None and until_local is not None:
        raise InvalidSchedule("RRULE cannot contain both COUNT and UNTIL (RFC 5545)")
    if "INTERVAL" in parts:
        try:
            if int(parts["INTERVAL"]) < 1:
                raise ValueError
        except ValueError as error:
            raise InvalidSchedule("RRULE INTERVAL must be a positive integer") from error
    return ParsedRule(parts=parts, count=count, until_local=until_local, until_utc=until_utc)


@dataclass(frozen=True)
class Instance:
    original_at: datetime
    local_wall: datetime
    adjusted_for_gap: bool


@dataclass(frozen=True)
class RecurrenceSpec:
    start_local: datetime
    timezone: str
    rule: str
    count: int | None = None
    until_utc: datetime | None = None
    monthly_day_policy: MonthlyDayPolicy = MonthlyDayPolicy.SKIP


def _apply_clamp(parts: dict[str, str], start: datetime) -> dict[str, str]:
    if "BYSETPOS" in parts:
        return parts
    freq = parts["FREQ"]
    result = dict(parts)
    if freq == "MONTHLY" and "BYDAY" not in parts:
        days = parts.get("BYMONTHDAY")
        day = start.day if days is None else (int(days) if days.isdigit() else None)
        if day is not None and day > 28:
            result["BYMONTHDAY"] = ",".join(str(d) for d in range(28, day + 1))
            result["BYSETPOS"] = "-1"
    elif freq == "YEARLY" and not ({"BYDAY", "BYYEARDAY", "BYWEEKNO"} & set(parts)):
        month = parts.get("BYMONTH", str(start.month))
        days = parts.get("BYMONTHDAY")
        day = start.day if days is None else (int(days) if days.isdigit() else None)
        if month == "2" and day == 29:
            result["BYMONTH"] = "2"
            result["BYMONTHDAY"] = "28,29"
            result["BYSETPOS"] = "-1"
    return result


def build_rrule(spec: RecurrenceSpec) -> tuple[rrule, ZoneInfo]:
    tz = load_timezone(spec.timezone)
    parsed = parse_rrule(spec.rule, tz)
    count = spec.count if spec.count is not None else parsed.count
    until_utc = spec.until_utc if spec.until_utc is not None else parsed.until_utc
    if spec.count is not None and parsed.count is not None and spec.count != parsed.count:
        raise InvalidSchedule("count conflicts with the RRULE COUNT value")
    if count is not None and until_utc is not None:
        raise InvalidSchedule("Provide either count or until, not both")
    if count is not None and not 1 <= count <= MAX_COUNT:
        raise InvalidSchedule(f"count must be between 1 and {MAX_COUNT}")
    parts = parsed.parts
    if spec.monthly_day_policy is MonthlyDayPolicy.CLAMP:
        parts = _apply_clamp(parts, spec.start_local)
    text = ParsedRule(parts=parts, count=None, until_local=None, until_utc=None).normalized()
    if count is not None:
        text += f";COUNT={count}"
    if until_utc is not None:
        text += ";UNTIL=" + to_local_naive(until_utc, tz).strftime("%Y%m%dT%H%M%S")
    try:
        rule = rrulestr("RRULE:" + text, dtstart=spec.start_local.replace(microsecond=0))
    except (ValueError, TypeError) as error:
        raise InvalidSchedule(f"Invalid RRULE: {error}", details={"field": "rrule"}) from error
    if not isinstance(rule, rrule):
        raise InvalidSchedule("Only a single RRULE is supported", details={"field": "rrule"})
    return rule, tz


def iter_instances(spec: RecurrenceSpec, after: datetime) -> Iterator[Instance]:
    """Yield instances whose UTC instant is >= ``after`` in chronological order."""
    rule, tz = build_rrule(spec)
    local_after = to_local_naive(after - timedelta(days=1), tz)
    previous: datetime | None = None
    for local in rule.xafter(local_after, inc=True):
        resolved = localize(local, tz)
        if resolved.instant < after:
            continue
        if previous is not None and resolved.instant <= previous:
            # Two local times collapsed to one instant across a DST transition.
            continue
        previous = resolved.instant
        yield Instance(
            original_at=resolved.instant,
            local_wall=local,
            adjusted_for_gap=resolved.adjusted_for_gap,
        )


def instances_between(
    spec: RecurrenceSpec, start: datetime, end: datetime, limit: int
) -> list[Instance]:
    result: list[Instance] = []
    for instance in iter_instances(spec, start):
        if instance.original_at >= end or len(result) >= limit:
            break
        result.append(instance)
    return result


def first_instance(spec: RecurrenceSpec, after: datetime) -> Instance | None:
    return next(iter_instances(spec, after), None)


def validate_recurrence(
    spec: RecurrenceSpec, *, now: datetime, min_interval_seconds: int
) -> list[str]:
    """Validate a recurrence, returning warnings. Raises InvalidSchedule for errors."""
    warnings: list[str] = []
    rule, tz = build_rrule(spec)
    sample: list[datetime] = []
    for instance in iter_instances(spec, min(now, localize(spec.start_local, tz).instant)):
        sample.append(instance.original_at)
        if instance.adjusted_for_gap and not any("DST" in w for w in warnings):
            warnings.append(
                "Some occurrences fall in a DST gap and are shifted forward by the gap length"
            )
        if len(sample) >= 64:
            break
    if not sample:
        raise InvalidSchedule("Schedule produces no occurrences")
    spacing = min(
        (b - a for a, b in zip(sample, sample[1:], strict=False)),
        default=timedelta.max,
    )
    if spacing < timedelta(seconds=min_interval_seconds):
        raise InvalidSchedule(
            f"Occurrences are {int(spacing.total_seconds())}s apart; the minimum allowed "
            f"interval is {min_interval_seconds}s",
            details={"field": "rrule", "min_interval_seconds": min_interval_seconds},
        )
    if first_instance(spec, now) is None:
        raise InvalidSchedule("Schedule has no occurrences in the future")
    parts = parse_rrule(spec.rule, tz).parts
    day = parts.get("BYMONTHDAY")
    start_day = spec.start_local.day
    if (
        spec.monthly_day_policy is MonthlyDayPolicy.SKIP
        and parts["FREQ"] in {"MONTHLY", "YEARLY"}
        and (
            (day is None and start_day > 28)
            or (day is not None and day.isdigit() and int(day) > 28)
        )
    ):
        warnings.append(
            "This rule skips months without the requested day (RFC 5545). "
            "Use monthly_day_policy='clamp' to use the last day of shorter months instead"
        )
    return warnings


def parse_local_datetime(value: datetime | str, field: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as error:
            raise InvalidSchedule(f"{field} must be an ISO 8601 local datetime") from error
    if value.tzinfo is not None:
        raise AmbiguousDateTime(
            f"{field} must be a local wall-clock time without an offset; the timezone is given "
            "separately",
            details={"field": field},
        )
    return value


def require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AmbiguousDateTime(
            f"{field} has no UTC offset. Provide an offset (e.g. 2026-10-28T09:00:00+05:30 or "
            "...Z), or use local_datetime with an IANA timezone",
            details={"field": field},
        )
    return value.astimezone(UTC)


def is_leap_day(value: date) -> bool:
    return value.month == 2 and value.day == 29
