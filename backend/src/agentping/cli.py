"""`agentping-admin`: owner administration from a trusted shell.

Secrets (agent tokens, pairing codes) are printed once to stdout, or written to a
file with owner-only permissions when ``--output`` is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

from agentping.auth.principal import DEFAULT_AGENT_SCOPES, USER_SCOPES, Principal
from agentping.auth.service import ensure_user
from agentping.bootstrap import build_runtime
from agentping.config import get_settings
from agentping.domain.enums import ActionSource, ActorKind, PushTokenKind
from agentping.domain.errors import DomainError
from agentping.domain.schemas import AgentCreate
from agentping.push.base import PushMessage
from agentping.services import agents, devices
from agentping.services.runtime import ServiceContext


def _emit(payload: dict[str, Any], output: str | None, secret_field: str | None = None) -> None:
    if output and secret_field:
        path = Path(output)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(str(payload[secret_field]) + "\n")
        payload = {**payload, secret_field: f"<written to {path}>"}
    print(json.dumps(payload, indent=2, default=str))


async def _as_owner[T](operation: Callable[[ServiceContext], Awaitable[T]]) -> T:
    settings = get_settings()
    runtime, engine = build_runtime(settings)
    try:
        async with runtime.session_factory() as session, session.begin():
            user = await ensure_user(session, runtime)
        principal = Principal(
            user_id=user.id,
            actor_kind=ActorKind.USER,
            scopes=frozenset(USER_SCOPES),
            auth_method="admin_cli",
        )
        async with runtime.unit(principal, source=ActionSource.SYSTEM, rate_limit=False) as ctx:
            return await operation(ctx)
    finally:
        await engine.dispose()


async def _bootstrap(_: argparse.Namespace) -> None:
    async def run(ctx: ServiceContext) -> dict[str, Any]:
        user = await ctx.user()
        return {"user_id": user.id, "email": user.email, "timezone": user.timezone}

    _emit(await _as_owner(run), None)


async def _create_agent(args: argparse.Namespace) -> None:
    data = AgentCreate(
        name=args.name,
        description=args.description,
        scopes=args.scopes or sorted(DEFAULT_AGENT_SCOPES),
        token_ttl_days=args.ttl_days,
    )

    async def run(ctx: ServiceContext) -> dict[str, Any]:
        issued = await agents.create_agent(ctx, data)
        return issued.model_dump(mode="json")

    result = await _as_owner(run)
    _emit({**result, "token": result.pop("token")}, args.output, "token")


async def _list_agents(_: argparse.Namespace) -> None:
    async def run(ctx: ServiceContext) -> dict[str, Any]:
        return {"agents": [a.model_dump(mode="json") for a in await agents.list_agents(ctx)]}

    _emit(await _as_owner(run), None)


async def _revoke_agent(args: argparse.Namespace) -> None:
    import uuid

    async def run(ctx: ServiceContext) -> dict[str, Any]:
        return (await agents.revoke_agent(ctx, uuid.UUID(args.agent_id))).model_dump(mode="json")

    _emit(await _as_owner(run), None)


async def _pairing_code(args: argparse.Namespace) -> None:
    async def run(ctx: ServiceContext) -> dict[str, Any]:
        return (await devices.create_pairing_code(ctx)).model_dump(mode="json")

    _emit(await _as_owner(run), args.output, "code")


async def _list_devices(_: argparse.Namespace) -> None:
    async def run(ctx: ServiceContext) -> dict[str, Any]:
        rows = await devices.list_devices(ctx, include_inactive=True)
        return {"devices": [d.model_dump(mode="json") for d in rows]}

    _emit(await _as_owner(run), None)


async def _fcm_check(args: argparse.Namespace) -> None:
    """Validate-only FCM send. Success proves credentials and project access, not delivery."""
    from agentping.push.fcm import FcmPushSender

    settings = get_settings()
    token = ""
    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    sender = FcmPushSender(
        settings.firebase_project_id,
        package_name=settings.fcm_restricted_package_name or None,
        dry_run=True,
    )
    probe = token or "agentping-validate-only-probe-token"
    try:
        [result] = await sender.send(
            [
                PushMessage(
                    token=probe,
                    token_kind=PushTokenKind(args.token_kind),
                    data={"type": "diagnostic"},
                    priority="normal",
                )
            ]
        )
    finally:
        await sender.aclose()
    if token:
        verdict = "token_valid" if result.accepted else "rejected"
    elif result.token_invalid or result.error_code in {"INVALID_ARGUMENT", "invalid-argument"}:
        verdict = "credentials_ok_probe_token_rejected_as_expected"
    else:
        verdict = "accepted" if result.accepted else "credential_or_project_error"
    _emit(
        {
            "project_id": settings.firebase_project_id,
            "validate_only": True,
            "verdict": verdict,
            "accepted": result.accepted,
            "error_code": result.error_code,
            "error_type": result.detail,
            "note": "validate_only success is not device delivery",
        },
        None,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentping-admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap", help="Create the owner account if needed").set_defaults(
        handler=_bootstrap
    )

    create = sub.add_parser("create-agent", help="Authorize an agent and issue a token")
    create.add_argument("--name", required=True)
    create.add_argument("--description", default="")
    create.add_argument("--scopes", nargs="*", help="Defaults to all non-critical agent scopes")
    create.add_argument("--ttl-days", type=int, default=90)
    create.add_argument("--output", help="Write the token to this file (mode 0600)")
    create.set_defaults(handler=_create_agent)

    sub.add_parser("list-agents").set_defaults(handler=_list_agents)

    revoke = sub.add_parser("revoke-agent")
    revoke.add_argument("agent_id")
    revoke.set_defaults(handler=_revoke_agent)

    pairing = sub.add_parser("pairing-code", help="Create a one-time device pairing code")
    pairing.add_argument("--output", help="Write the code to this file (mode 0600)")
    pairing.set_defaults(handler=_pairing_code)

    sub.add_parser("list-devices").set_defaults(handler=_list_devices)

    fcm = sub.add_parser("fcm-check", help="Validate-only FCM send using ADC credentials")
    fcm.add_argument(
        "--token-file", help="File containing a real FID or registration token to validate"
    )
    fcm.add_argument(
        "--token-kind",
        choices=[kind.value for kind in PushTokenKind],
        default=PushTokenKind.FID.value,
    )
    fcm.set_defaults(handler=_fcm_check)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], Coroutine[Any, Any, None]] = args.handler
    try:
        asyncio.run(handler(args))
    except DomainError as error:
        print(json.dumps({"error": error.code, "message": error.message}), file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
