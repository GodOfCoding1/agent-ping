"""State machine, quiet hours, schemas, tokens and log redaction."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from agentping.auth import tokens
from agentping.auth.principal import AGENT_GRANTABLE_SCOPES, DEFAULT_AGENT_SCOPES, Scope
from agentping.config import DatabaseGuardError, Settings, checked_database_url
from agentping.domain.enums import ActionKind, ActionResult, OccurrenceState
from agentping.domain.quiet_hours import quiet_hours_end
from agentping.domain.schemas import QuietHours, ReminderCreate, SendNotification
from agentping.domain.states import decide_action
from agentping.logging import JsonFormatter, redact

S = OccurrenceState


@pytest.mark.parametrize(
    ("state", "action", "result", "new_state"),
    [
        (S.SCHEDULED, ActionKind.ACKNOWLEDGE, ActionResult.APPLIED, S.ACKNOWLEDGED),
        (S.FIRED, ActionKind.ACKNOWLEDGE, ActionResult.APPLIED, S.ACKNOWLEDGED),
        (S.SNOOZED, ActionKind.ACKNOWLEDGE, ActionResult.APPLIED, S.ACKNOWLEDGED),
        (S.MISSED, ActionKind.ACKNOWLEDGE, ActionResult.APPLIED, S.ACKNOWLEDGED),
        (S.FIRED, ActionKind.SNOOZE, ActionResult.APPLIED, S.SNOOZED),
        (S.SNOOZED, ActionKind.SNOOZE, ActionResult.APPLIED, S.SNOOZED),
        (S.SCHEDULED, ActionKind.SKIP, ActionResult.APPLIED, S.SKIPPED),
        (S.ACKNOWLEDGED, ActionKind.ACKNOWLEDGE, ActionResult.DUPLICATE, None),
        (S.SKIPPED, ActionKind.SKIP, ActionResult.DUPLICATE, None),
        (S.ACKNOWLEDGED, ActionKind.SNOOZE, ActionResult.REJECTED_STALE, None),
        (S.CANCELLED, ActionKind.ACKNOWLEDGE, ActionResult.REJECTED_STALE, None),
        (S.SUPERSEDED, ActionKind.ACKNOWLEDGE, ActionResult.REJECTED_STALE, None),
        (S.MISSED, ActionKind.SNOOZE, ActionResult.REJECTED_STALE, None),
    ],
)
def test_action_state_machine(state, action, result, new_state) -> None:
    decision = decide_action(state, action)
    assert decision.result is result
    if new_state is not None:
        assert decision.new_state is new_state


def test_quiet_hours_window_across_midnight() -> None:
    tz = ZoneInfo("Asia/Kolkata")
    quiet = QuietHours(enabled=True, start=time(22, 0), end=time(7, 0))
    inside = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)  # 23:30 IST
    end = quiet_hours_end(quiet, tz, inside)
    assert end == datetime(2026, 1, 2, 1, 30, tzinfo=UTC)  # 07:00 IST
    early = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)  # 05:30 IST
    assert quiet_hours_end(quiet, tz, early) == datetime(2026, 1, 1, 1, 30, tzinfo=UTC)
    outside = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)  # 11:30 IST
    assert quiet_hours_end(quiet, tz, outside) is None
    assert quiet_hours_end(QuietHours(enabled=False), tz, inside) is None


def test_reminder_schema_rejects_unknown_fields_and_bad_timezone() -> None:
    base = {"title": "x", "schedule": {"kind": "once", "at": "2026-01-01T09:00:00Z"}}
    ReminderCreate.model_validate(base)
    with pytest.raises(ValidationError):
        ReminderCreate.model_validate({**base, "surprise": 1})
    with pytest.raises(ValidationError):
        ReminderCreate.model_validate(
            {
                "title": "x",
                "schedule": {
                    "kind": "once",
                    "local_datetime": "2026-01-01T09:00:00",
                    "timezone": "Mars/Base",
                },
            }
        )
    with pytest.raises(ValidationError):
        ReminderCreate.model_validate({"title": "", "schedule": base["schedule"]})


def test_send_notification_limits() -> None:
    SendNotification.model_validate({"title": "Build done"})
    with pytest.raises(ValidationError):
        SendNotification.model_validate({"title": "x" * 201})
    with pytest.raises(ValidationError):
        SendNotification.model_validate({"title": "ok", "body": "x" * 5000})


def test_token_helpers() -> None:
    raw = tokens.generate_token(tokens.AGENT_PREFIX)
    assert raw.startswith("apa_") and len(raw) > 40
    assert tokens.hash_secret(raw) == tokens.hash_secret(raw)
    assert tokens.hash_secret(raw) != tokens.hash_secret(raw + "x")
    code = tokens.generate_pairing_code()
    assert tokens.normalize_pairing_code(code.lower().replace("-", " ")) == code
    assert tokens.constant_time_equals("abc", "abc") and not tokens.constant_time_equals("a", "b")


def test_default_agent_scopes_exclude_critical() -> None:
    assert Scope.ALERTS_CRITICAL in AGENT_GRANTABLE_SCOPES
    assert Scope.ALERTS_CRITICAL not in DEFAULT_AGENT_SCOPES
    assert Scope.SETTINGS_WRITE not in AGENT_GRANTABLE_SCOPES
    assert Scope.AGENTS_MANAGE not in AGENT_GRANTABLE_SCOPES


def test_redaction_of_secrets() -> None:
    text = (
        "postgresql://user:hunter2@db.example/app Bearer abc.def.ghi "
        "apa_SECRETSECRETSECRET password=letmein"
    )
    cleaned = redact(text)
    for secret in ("hunter2", "abc.def.ghi", "SECRETSECRETSECRET", "letmein"):
        assert secret not in cleaned


def test_json_formatter_redacts_sensitive_extras() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "push sent", None, None)
    record.fcm_token = "secret-token"
    record.body = "private reminder text"
    record.device_id = "d1"
    output = JsonFormatter().format(record)
    assert "secret-token" not in output
    assert "private reminder text" not in output
    assert "d1" in output


def test_database_guard_rejects_unverified_remote_and_plaintext(tmp_path) -> None:
    settings = Settings(app_env="development", neon_context_path=tmp_path / "missing.json")
    with pytest.raises(DatabaseGuardError, match="TLS"):
        checked_database_url("postgresql://u:p@ep-x.neon.tech/db", settings, migration=False)
    with pytest.raises(DatabaseGuardError, match="API-verified"):
        checked_database_url(
            "postgresql://u:p@ep-x.neon.tech/db?sslmode=require", settings, migration=False
        )
    url = checked_database_url("postgresql://u:p@localhost/db", settings, migration=False)
    assert url.drivername == "postgresql+psycopg"


def test_production_refuses_development_auth() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="production", auth_mode="development", public_base_url="https://x")
    with pytest.raises(ValidationError):
        Settings(
            app_env="production",
            auth_mode="oidc",
            oidc_issuer="https://issuer/",
            oidc_audience="aud",
            public_base_url="http://insecure",
        )
