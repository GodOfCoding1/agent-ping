"""Occurrence state machine.

States:
  scheduled  -> waiting for its due time.
  fired      -> a device reported that it rang/displayed the occurrence.
  snoozed    -> postponed; ``scheduled_at`` holds the snooze deadline.
  acknowledged, skipped, missed, cancelled -> terminal.
  superseded -> removed by a schedule edit or pause; the scheduler may reinstate it
                (same deterministic ID) if a later edit or resume produces the same instant.
                User/agent/device actions are never applied to superseded occurrences.

User actions arriving for a terminal occurrence never change it. A repeated identical
terminal action is reported as a duplicate; anything else is rejected as stale, so an old
acknowledgement from an offline phone cannot resurrect a cancelled reminder.
``missed`` occurrences may still be acknowledged or skipped late; this records the user's
response without re-alerting.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentping.domain.enums import ActionKind, ActionResult, OccurrenceState

S = OccurrenceState

_ACTION_TARGET = {
    ActionKind.ACKNOWLEDGE: S.ACKNOWLEDGED,
    ActionKind.SNOOZE: S.SNOOZED,
    ActionKind.SKIP: S.SKIPPED,
}

_ALLOWED: dict[ActionKind, frozenset[OccurrenceState]] = {
    ActionKind.ACKNOWLEDGE: frozenset({S.SCHEDULED, S.FIRED, S.SNOOZED, S.MISSED}),
    ActionKind.SNOOZE: frozenset({S.SCHEDULED, S.FIRED, S.SNOOZED}),
    ActionKind.SKIP: frozenset({S.SCHEDULED, S.FIRED, S.SNOOZED, S.MISSED}),
}

SYSTEM_TRANSITIONS: dict[OccurrenceState, frozenset[OccurrenceState]] = {
    S.SCHEDULED: frozenset({S.FIRED, S.MISSED, S.CANCELLED, S.SUPERSEDED}),
    S.FIRED: frozenset({S.FIRED, S.MISSED, S.CANCELLED}),
    S.SNOOZED: frozenset({S.FIRED, S.MISSED, S.CANCELLED}),
    S.SUPERSEDED: frozenset({S.SCHEDULED, S.CANCELLED}),
    S.ACKNOWLEDGED: frozenset(),
    S.SKIPPED: frozenset(),
    S.MISSED: frozenset(),
    S.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class ActionDecision:
    result: ActionResult
    new_state: OccurrenceState | None
    reason: str | None = None


def decide_action(current: OccurrenceState, action: ActionKind) -> ActionDecision:
    target = _ACTION_TARGET[action]
    if current in _ALLOWED[action]:
        return ActionDecision(ActionResult.APPLIED, target)
    if current is target and action is not ActionKind.SNOOZE:
        return ActionDecision(ActionResult.DUPLICATE, None, f"already {current.value}")
    return ActionDecision(
        ActionResult.REJECTED_STALE,
        None,
        f"cannot {action.value} an occurrence that is {current.value}",
    )


def can_system_transition(current: OccurrenceState, target: OccurrenceState) -> bool:
    return target in SYSTEM_TRANSITIONS[current]
