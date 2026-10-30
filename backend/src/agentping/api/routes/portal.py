"""First-party account portal. OAuth credentials stay encrypted on the server."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from agentping.auth.principal import USER_SCOPES, Principal
from agentping.config import Settings
from agentping.db.models import PortalLogin, PortalSession, User
from agentping.domain.errors import Forbidden, Unauthenticated
from agentping.mcp.metadata import MCP_DESCRIPTION, MCP_NAME
from agentping.services import accounts, agents, devices

router = APIRouter(include_in_schema=False)


def _cipher(settings: Settings) -> Fernet:
    return Fernet(settings.portal_encryption_key.get_secret_value().encode())


def _encrypt(settings: Settings, data: dict[str, Any]) -> str:
    return _cipher(settings).encrypt(json.dumps(data).encode()).decode()


def _decrypt(settings: Settings, data: str) -> dict[str, Any]:
    try:
        return json.loads(_cipher(settings).decrypt(data.encode()))
    except (InvalidToken, ValueError) as error:
        raise Unauthenticated("Session is invalid; please sign in again") from error


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _cookie(settings: Settings, purpose: str) -> str:
    return ("__Host-" if settings.is_production else "") + "agentping_" + purpose


def _set_cookie(response: Response, settings: Settings, purpose: str, raw: str, age: int) -> None:
    response.set_cookie(
        _cookie(settings, purpose),
        raw,
        max_age=age,
        secure=settings.is_production,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _page(title: str, content: str, status: int = 200) -> HTMLResponse:
    nonce = secrets.token_urlsafe(24)
    # Dynamic strings are escaped at their call sites; there is no client-side script.
    return HTMLResponse(
        f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · AgentPing</title>
<style nonce="{nonce}">body{{font:16px/1.6 system-ui;background:#f5f6fa;color:#172137;margin:0}}
main{{max-width:800px;margin:64px auto;padding:0 24px}}h1{{font-size:36px;line-height:1.2}}
section{{background:white;border:1px solid #dde3ef;border-radius:16px;padding:24px;margin:24px 0}}
a{{color:#3456bb}}button,.button{{font:inherit;background:#294ac5;color:white;border:0;
border-radius:8px;padding:10px 18px;cursor:pointer;text-decoration:none;display:inline-block}}
button.danger{{background:#a02b36}}small{{color:#5c6980}}code{{overflow-wrap:anywhere}}
textarea{{box-sizing:border-box;width:100%;font:inherit;padding:12px;border:1px solid #dde3ef;
border-radius:8px;resize:vertical}}label{{display:block;font-weight:600}}
.row{{padding:16px 0;border-bottom:1px solid #e6eaf1}}
nav{{display:flex;justify-content:space-between}}
form{{display:inline}}.code{{font-size:30px;letter-spacing:3px;font-weight:bold}}</style>
<main><nav><a href="/account">AgentPing</a><a href="/privacy">Privacy</a></nav>
<h1>{title}</h1>{content}</main></html>''',
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            # HTML form POSTs need their real Origin for the CSRF origin check.
            # No referrer is sent to external sites under same-origin either.
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": f"default-src 'none'; style-src 'nonce-{nonce}'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        },
    )


def _form(path: str, csrf: str, label: str, *, danger: bool = False) -> str:
    return (
        f'<form method="post" action="{html.escape(path, quote=True)}">'
        f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
        f'<button class="{"danger" if danger else ""}">{label}</button></form>'
    )


async def _token(request: Request, body: dict[str, str]) -> dict[str, Any]:
    settings = request.app.state.runtime.settings
    # Fixed issuer and client: never accept provider URLs or credentials from browser input.
    body.update(
        client_id=settings.portal_client_id,
        client_secret=settings.portal_client_secret.get_secret_value(),
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                settings.oidc_issuer.rstrip("/") + "/oauth/token", json=body
            )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result.get("access_token"), str):
            raise ValueError("missing access token")
        return result
    except (httpx.HTTPError, ValueError, KeyError) as error:
        raise Unauthenticated("Sign-in could not be completed; please sign in again") from error


@router.get("/login")
async def login(request: Request) -> Response:
    runtime = request.app.state.runtime
    settings = runtime.settings
    state, browser, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(4))
    async with runtime.session_factory() as session, session.begin():
        session.add(
            PortalLogin(
                state_hash=_hash(state),
                browser_hash=_hash(browser),
                encrypted_data=_encrypt(settings, {"verifier": verifier, "nonce": nonce}),
                expires_at=runtime.clock.now() + timedelta(minutes=10),
            )
        )
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    query = urlencode(
        {
            "client_id": settings.portal_client_id,
            "response_type": "code",
            "redirect_uri": settings.public_base_url.rstrip("/") + "/auth/callback",
            "audience": settings.oidc_api_audience,
            "scope": "openid profile email offline_access " + " ".join(sorted(USER_SCOPES)),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
        }
    )
    response = RedirectResponse(settings.oidc_issuer.rstrip("/") + "/authorize?" + query, 303)
    _set_cookie(response, settings, "login", browser, 600)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/auth/callback")
async def callback(request: Request) -> Response:
    runtime = request.app.state.runtime
    settings = runtime.settings
    state = request.query_params.get("state", "")
    browser = request.cookies.get(_cookie(settings, "login"), "")
    if not state or not browser or len(state) > 200:
        raise Unauthenticated("Missing sign-in state or browser cookie")
    async with runtime.session_factory() as session, session.begin():
        pending = (
            await session.execute(
                select(PortalLogin).where(PortalLogin.state_hash == _hash(state)).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            pending is None
            or pending.expires_at <= runtime.clock.now()
            or not secrets.compare_digest(pending.browser_hash, _hash(browser))
        ):
            raise Unauthenticated("Sign-in expired or belongs to a different browser")
        data = _decrypt(settings, pending.encrypted_data)
        await session.delete(pending)
    # State is consumed before exchanging a code; failed requests cannot replay it.
    if request.query_params.get("error") or not request.query_params.get("code"):
        return _page("Sign-in cancelled", '<p><a href="/login">Try signing in again</a></p>', 400)
    if request.query_params.get("iss") not in (None, settings.oidc_issuer):
        raise Unauthenticated("Unexpected authorization server")
    result = await _token(
        request,
        {
            "grant_type": "authorization_code",
            "code": request.query_params["code"],
            "code_verifier": data["verifier"],
            "redirect_uri": settings.public_base_url.rstrip("/") + "/auth/callback",
        },
    )
    auth = request.app.state.authenticator
    identity = await auth.oidc.verify_id_token(
        result.get("id_token", ""), client_id=settings.portal_client_id, nonce=data["nonce"]
    )
    verified = await auth.oidc.verify(result["access_token"], audience=settings.oidc_api_audience)
    if verified.subject != identity["sub"] or verified.client_id != settings.portal_client_id:
        raise Unauthenticated("Token identity does not match sign-in")
    principal = await auth.authenticate(result["access_token"])
    raw = secrets.token_urlsafe(32)
    async with runtime.session_factory() as session, session.begin():
        user = await session.get(User, principal.user_id)
        if user is None or user.status != "active":
            raise Unauthenticated("Account is unavailable")
        if identity.get("email_verified") is True and isinstance(identity.get("email"), str):
            user.email = identity["email"][:320]
        if isinstance(identity.get("name"), str):
            user.display_name = identity["name"][:200]
        session.add(
            PortalSession(
                token_hash=_hash(raw),
                user_id=user.id,
                encrypted_data=_encrypt(
                    settings,
                    {
                        "access_token": result["access_token"],
                        "refresh_token": result.get("refresh_token"),
                        "subject": verified.subject,
                        "expires_at": verified.claims["exp"],
                        "csrf": secrets.token_urlsafe(32),
                    },
                ),
                expires_at=runtime.clock.now() + timedelta(days=settings.portal_session_days),
            )
        )
    response = RedirectResponse("/account", 303)
    _set_cookie(response, settings, "session", raw, settings.portal_session_days * 86400)
    response.delete_cookie(
        _cookie(settings, "login"),
        path="/",
        secure=settings.is_production,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def _session(request: Request, *, csrf: bool = False) -> tuple[Principal, str, str]:
    runtime = request.app.state.runtime
    settings = runtime.settings
    raw = request.cookies.get(_cookie(settings, "session"), "")
    if not raw or len(raw) > 200:
        raise Unauthenticated("Please sign in")
    digest = _hash(raw)
    async with runtime.session_factory() as session, session.begin():
        row = (
            await session.execute(
                select(PortalSession).where(PortalSession.token_hash == digest).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.expires_at <= runtime.clock.now():
            raise Unauthenticated("Session expired; please sign in again")
        user = await session.get(User, row.user_id)
        if user is None or user.status != "active":
            raise Unauthenticated("Account is unavailable")
        data = _decrypt(settings, row.encrypted_data)
        if csrf:
            origin = request.headers.get("origin")
            if origin and origin != settings.public_base_url.rstrip("/"):
                raise Forbidden("Invalid request origin")
            form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
            supplied = form.get("csrf", [""])[0]
            if not secrets.compare_digest(supplied, data["csrf"]):
                raise Forbidden("Invalid form token; reload the page")
        if data["expires_at"] <= runtime.clock.now().timestamp() + 30:
            if not data.get("refresh_token"):
                raise Unauthenticated("Session needs a new sign-in")
            result = await _token(
                request, {"grant_type": "refresh_token", "refresh_token": data["refresh_token"]}
            )
            verified = await request.app.state.authenticator.oidc.verify(
                result["access_token"], audience=settings.oidc_api_audience
            )
            if (
                verified.subject != data["subject"]
                or verified.client_id != settings.portal_client_id
            ):
                raise Unauthenticated("Refreshed token identity mismatch")
            data.update(
                access_token=result["access_token"],
                expires_at=verified.claims["exp"],
                refresh_token=result.get("refresh_token", data["refresh_token"]),
            )
            row.encrypted_data = _encrypt(settings, data)
        access_token = data["access_token"]
        user_id = row.user_id
    principal = await request.app.state.authenticator.authenticate(access_token)
    if principal.user_id != user_id:
        raise Unauthenticated("Session account mismatch")
    return principal, data["csrf"], digest


def _connection_guide(settings: Settings) -> str:
    return (
        "<section><h2>Connect your agent</h2>"
        "<p>When adding this MCP server in your AI app, use these values. "
        "If it asks for a description or instructions for the agent, "
        "select and copy the suggested text below.</p>"
        "<p><b>Name</b><br><code>" + html.escape(MCP_NAME) + "</code></p>"
        "<p><b>MCP server URL</b><br><code>"
        + html.escape(settings.mcp_resource_url)
        + "</code></p><p><b>Transport</b><br>Streamable HTTP (if asked)</p>"
        '<label for="mcp-description">Description / agent instructions</label>'
        '<textarea id="mcp-description" rows="7" readonly aria-describedby="mcp-description-help">'
        + html.escape(MCP_DESCRIPTION)
        + '</textarea><small id="mcp-description-help">Select and copy this text into your '
        "AI app. It describes when the agent should use AgentPing.</small>"
        "<p><b>Authentication</b><br>OAuth / browser sign-in. Use the same AgentPing account "
        "you use to pair your phone. Your MCP application needs a registered OAuth client; "
        "use its assigned client ID if asked. A description does not grant access.</p>"
        "<p>Pair your phone from this account page and enable notifications in the Android "
        "app before asking your agent to send an alert. Access applies only to your account.</p>"
        "</section>"
    )


async def _account(request: Request, *, message: str = "") -> Response:
    runtime = request.app.state.runtime
    try:
        principal, csrf, _ = await _session(request)
    except Unauthenticated:
        return _page(
            "Your agents. Your devices.",
            "<section><p>Sign in to connect an AI agent, "
            "pair your phone and manage access to your notifications and reminders.</p>"
            '<a class="button" href="/login">Sign in or create an account</a></section>'
            + _connection_guide(runtime.settings),
        )
    async with runtime.unit(principal) as ctx:
        user = await ctx.session.get(User, principal.user_id)
        agent_rows = await agents.list_agents(ctx)
        device_rows = await devices.list_devices(ctx)
    content = (
        f"<p>Signed in as {html.escape(user.display_name or user.email or 'AgentPing user')}</p>"
    )
    content += message
    content += _connection_guide(runtime.settings)
    content += "<section><h2>Connected agents</h2>"
    for agent in agent_rows:
        content += (
            f'<div class="row"><b>{html.escape(agent.name)}</b> · {html.escape(agent.status)}'
        )
        content += "<br><small>" + html.escape(", ".join(agent.scopes)) + "</small><br>"
        if agent.status == "active":
            content += _form(
                f"/account/agents/{agent.id}/revoke", csrf, "Revoke access", danger=True
            )
        elif agent.kind == "oidc":
            content += _form(f"/account/agents/{agent.id}/reauthorize", csrf, "Allow a new login")
        content += "</div>"
    if not agent_rows:
        content += "<p>No agents connected yet.</p>"
    content += "</section><section><h2>Your devices</h2>"
    for device in device_rows:
        content += f'<div class="row">{html.escape(device.name)} '
        content += _form(f"/account/devices/{device.id}/revoke", csrf, "Unpair device", danger=True)
        content += "</div>"
    if not device_rows:
        content += "<p>No phone paired yet.</p>"
    content += "<p>Create a one-time code and enter it in AgentPing on your phone. "
    content += "Keep it private; it grants access to your account.</p>"
    content += _form("/account/pair", csrf, "Create pairing code") + "</section>"
    content += _form("/account/logout", csrf, "Sign out")
    content += "<section><h2>Close account</h2><p>Cancel active reminders, unpair devices and "
    content += "revoke all access. Account records and activity are retained. "
    content += "Closing cannot be undone from this page. Type CLOSE to confirm.</p>"
    content += '<form method="post" action="/account/close">'
    content += f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
    content += '<input name="confirmation" required pattern="CLOSE" aria-label="Type CLOSE"> '
    content += '<button class="danger">Close account</button></form></section>'
    return _page("Your account", content)


@router.get("/")
@router.get("/account")
async def account(request: Request) -> Response:
    return await _account(request)


@router.post("/account/pair")
async def pair(request: Request) -> Response:
    principal, _, _ = await _session(request, csrf=True)
    async with request.app.state.runtime.unit(principal) as ctx:
        result = await devices.create_pairing_code(ctx)
    return await _account(
        request,
        message='<section><h2>Your pairing code</h2><p class="code">'
        + html.escape(result.code)
        + "</p><p>Expires in 10 minutes. Use it once on your phone.</p></section>",
    )


@router.post("/account/agents/{agent_id}/revoke")
async def revoke_agent(request: Request, agent_id: uuid.UUID) -> Response:
    principal, _, _ = await _session(request, csrf=True)
    async with request.app.state.runtime.unit(principal) as ctx:
        await agents.revoke_agent(ctx, agent_id)
    return RedirectResponse("/account", 303)


@router.post("/account/devices/{device_id}/revoke")
async def revoke_device(request: Request, device_id: uuid.UUID) -> Response:
    principal, _, _ = await _session(request, csrf=True)
    async with request.app.state.runtime.unit(principal) as ctx:
        await devices.deactivate_device(ctx, device_id)
    return RedirectResponse("/account", 303)


@router.post("/account/logout")
async def logout(request: Request) -> Response:
    _, _, digest = await _session(request, csrf=True)
    runtime = request.app.state.runtime
    async with runtime.session_factory() as session, session.begin():
        row = await session.get(PortalSession, digest)
        if row:
            await session.delete(row)
    response = RedirectResponse("/account", 303)
    response.delete_cookie(
        _cookie(runtime.settings, "session"),
        path="/",
        secure=runtime.settings.is_production,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/privacy")
async def privacy() -> Response:
    return _page(
        "Privacy and account access",
        "<section><p>AgentPing stores your account identity, "
        "paired device information, agent permissions, reminders, "
        "notification contents and activity "
        "records to deliver the service. Auth0 handles sign-in. "
        "Google Cloud handles the API and push "
        "delivery; Neon stores application data.</p><p>Each signed-in account has separate data. "
        "Agent access can be revoked in your account; device access can be removed by unpairing. "
        "Signing out ends this browser session. "
        "Revoking an agent blocks its existing access tokens "
        "at AgentPing. Account data is retained when an agent or device is disconnected."
        "</p></section>",
    )


@router.post("/account/close")
async def close_account(request: Request) -> Response:
    principal, _, _ = await _session(request, csrf=True)
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    if form.get("confirmation") != ["CLOSE"]:
        raise Forbidden("Type CLOSE to confirm account closure")
    async with request.app.state.runtime.unit(principal) as ctx:
        await accounts.close_account(ctx)
    response = _page(
        "Account closed",
        "<p>All access is revoked and active reminders are cancelled. "
        "Account records and activity are retained.</p>",
    )
    response.delete_cookie(_cookie(request.app.state.runtime.settings, "session"), path="/")
    return response


@router.post("/account/agents/{agent_id}/reauthorize")
async def reauthorize_agent(request: Request, agent_id: uuid.UUID) -> Response:
    principal, _, _ = await _session(request, csrf=True)
    async with request.app.state.runtime.unit(principal) as ctx:
        await agents.reauthorize_agent(ctx, agent_id)
    return await _account(
        request,
        message="<section><p>Access is allowed again. Sign in again "
        "in your MCP client. Tokens issued before this approval stay invalid."
        "</p></section>",
    )
