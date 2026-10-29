"""Signed OAuth token validation, user isolation, browser binding and service separation."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from agentping.api.app import create_app
from agentping.api.routes import portal
from agentping.auth.oidc import OidcVerifier
from agentping.auth.principal import DEFAULT_AGENT_SCOPES, USER_SCOPES, Scope
from agentping.auth.service import Authenticator
from agentping.config import ServiceMode
from agentping.db.models import AgentClient, PortalSession, User, UserIdentity
from agentping.domain.enums import ActorKind
from agentping.domain.errors import Unauthenticated
from tests.conftest import running_lifespan

ISSUER = "https://identity.example/"
ORIGIN = "https://agentping.example"
MCP = ORIGIN + "/mcp"
API = ORIGIN + "/api/v1"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def token(signing_key):
    def make(sub="user-a", client="codex", audience=MCP, scopes=None, headers=None, **claims):
        now = int(time.time())
        values = {
            "iss": ISSUER,
            "aud": audience,
            "sub": sub,
            "client_id": client,
            "iat": now,
            "exp": now + 600,
            "jti": secrets.token_hex(16),
            "scope": " ".join(scopes if scopes is not None else DEFAULT_AGENT_SCOPES),
        }
        values.update(claims)
        return jwt.encode(
            values,
            signing_key,
            algorithm="RS256",
            headers={"kid": "signing-1", "typ": "at+jwt", **(headers or {})},
        )

    return make


@pytest.fixture
def settings_overrides():
    return {
        "auth_mode": "oidc",
        "service_mode": "api",
        "oidc_issuer": ISSUER,
        "oidc_audience": MCP,
        "oidc_api_audience": API,
        "oidc_signup_enabled": True,
        "oidc_user_client_ids": ["portal"],
        "portal_client_id": "portal",
        "portal_client_secret": "test-only-secret",
        "portal_encryption_key": Fernet.generate_key(),
        "public_base_url": ORIGIN,
    }


@pytest.fixture
async def verifier(harness, signing_key):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    public.update(kid="signing-1", alg="RS256", use="sig")

    def provider(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": ISSUER + "jwks"})
        assert request.url.path == "/jwks"
        return httpx.Response(200, json={"keys": [public]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        yield OidcVerifier(harness.runtime.settings, http)


@pytest.fixture
async def auth(harness, verifier):
    return Authenticator(harness.runtime, verifier)


@pytest.fixture
async def oauth_client(harness, verifier):
    harness.clock.set(datetime.now(UTC))
    application = create_app(runtime=harness.runtime)
    application.state.authenticator.oidc = verifier
    async with running_lifespan(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url=ORIGIN
        ) as client:
            yield client


async def test_valid_signed_access_and_id_token_nonce(verifier, token):
    verified = await verifier.verify(token())
    assert verified.subject == "user-a"
    assert verified.client_id == "codex"
    assert verified.scopes == DEFAULT_AGENT_SCOPES
    raw = token(audience="portal", headers={"typ": "JWT"}, nonce="browser-nonce")
    claims = await verifier.verify_id_token(raw, client_id="portal", nonce="browser-nonce")
    assert claims["sub"] == "user-a"
    with pytest.raises(Unauthenticated):
        await verifier.verify_id_token(raw, client_id="portal", nonce="another-browser")


@pytest.mark.parametrize(
    "changes",
    [
        {"audience": API},
        {"iss": "https://attacker.example/"},
        {"exp": 1},
        {"headers": {"typ": "JWT"}},
        {"headers": {"kid": "unknown"}},
        {"client": ""},
        {"client": None},
        {"scope": ""},
        {"scope": []},
        {"scope": None, "permissions": ["reminders:read"]},
        {"sub": ""},
        {"gty": "client-credentials"},
        {"sub": "service@clients"},
        {"azp": "another-client"},
        {"exp": int(time.time()) + 7200},
        {"iat": int(time.time()) + 120},
        {"jti": None},
    ],
)
async def test_rejects_invalid_or_wrong_kind_tokens(verifier, token, changes):
    with pytest.raises(Unauthenticated):
        await verifier.verify(token(**changes))


async def test_permissions_claim_cannot_expand_consent(auth, token):
    principal = await auth.authenticate(
        token(scopes=[Scope.REMINDERS_READ], permissions=list(USER_SCOPES)), for_mcp=True
    )
    assert principal.actor_kind is ActorKind.AGENT
    assert principal.scopes == {Scope.REMINDERS_READ}


async def test_client_and_audience_do_not_become_owner(auth, token):
    agent = await auth.authenticate(token(client="codex", scopes=USER_SCOPES), for_mcp=True)
    assert agent.actor_kind is ActorKind.AGENT
    assert Scope.AGENTS_MANAGE not in agent.scopes
    owner = await auth.authenticate(token(client="portal", audience=API, scopes=USER_SCOPES))
    assert owner.actor_kind is ActorKind.USER
    assert agent.user_id == owner.user_id
    with pytest.raises(Unauthenticated):
        await auth.authenticate(
            token(client="portal", audience=API, scopes=USER_SCOPES), for_mcp=True
        )
    with pytest.raises(Unauthenticated):
        await auth.authenticate(token())


async def test_independent_subjects_same_email_are_not_linked(auth, token, harness):
    a = await auth.authenticate(token(email="same@example.com"), for_mcp=True)
    b = await auth.authenticate(token(sub="user-b", email="same@example.com"), for_mcp=True)
    assert a.user_id != b.user_id
    again = await auth.authenticate(token(email="changed@example.com"), for_mcp=True)
    assert again.user_id == a.user_id
    async with harness.runtime.session_factory() as session:
        users = list((await session.execute(select(User))).scalars())
        identities = list((await session.execute(select(UserIdentity))).scalars())
    assert len(users) == len(identities) == 2


async def test_revocation_and_suspension_apply_to_existing_jwts(auth, token, harness):
    raw = token()
    agent = await auth.authenticate(raw, for_mcp=True)
    async with harness.runtime.session_factory() as session, session.begin():
        row = await session.get(AgentClient, agent.agent_client_id)
        row.status = "revoked"
    with pytest.raises(Unauthenticated):
        await auth.authenticate(raw, for_mcp=True)
    with pytest.raises(Unauthenticated):
        await auth.authenticate(token(), for_mcp=True)  # fresh token cannot revive a grant
    async with harness.runtime.session_factory() as session, session.begin():
        user = await session.get(User, agent.user_id)
        user.status = "suspended"
    with pytest.raises(Unauthenticated):
        await auth.authenticate(token(client="portal", audience=API, scopes=USER_SCOPES))


async def test_public_metadata_challenge_and_private_worker(oauth_client):
    metadata = await oauth_client.get("/.well-known/oauth-protected-resource/mcp")
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == MCP
    assert set(metadata.json()["scopes_supported"]) == DEFAULT_AGENT_SCOPES
    denied = await oauth_client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}
    )
    assert denied.status_code == 401
    assert "resource_metadata=" in denied.headers["www-authenticate"]
    assert (await oauth_client.get("/readyz")).status_code == 401
    assert (await oauth_client.post("/internal/worker/run")).status_code == 404


async def test_two_users_rest_and_grants_are_isolated(oauth_client, token):
    def user(sub):
        return {
            "Authorization": "Bearer "
            + token(sub=sub, client="portal", audience=API, scopes=USER_SCOPES)
        }

    a, b = user("user-a"), user("user-b")
    created = await oauth_client.post(
        "/api/v1/agents", headers=a, json={"name": "Private agent", "scopes": ["reminders:read"]}
    )
    assert created.status_code == 201
    agent_id = created.json()["agent"]["id"]
    assert (await oauth_client.get("/api/v1/agents", headers=b)).json() == []
    assert (
        await oauth_client.post(f"/api/v1/agents/{agent_id}/revoke", headers=b)
    ).status_code == 404
    assert len((await oauth_client.get("/api/v1/agents", headers=a)).json()) == 1
    a_code = await oauth_client.post("/api/v1/devices/pairing-codes", headers=a)
    assert a_code.status_code == 201
    assert (await oauth_client.get("/api/v1/devices", headers=b)).json() == []


async def test_browser_login_pkce_cookie_state_and_replay(
    oauth_client, harness, token, monkeypatch
):
    login = await oauth_client.get("/login")
    assert login.status_code == 303
    assert login.headers.get_list("referrer-policy") == ["no-referrer"]
    query = parse_qs(urlparse(login.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert "client_secret" not in query
    state, nonce = query["state"][0], query["nonce"][0]
    callback = f"/auth/callback?code=provider-code&state={state}&iss={ISSUER}"
    # A stolen callback without the browser binding cannot exchange a code.
    async with httpx.AsyncClient(transport=oauth_client._transport, base_url=ORIGIN) as other:
        assert (await other.get(callback)).status_code == 401

    async def exchange(request, body):
        assert body["code"] == "provider-code"
        challenge = (
            portal.base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == query["code_challenge"][0]
        return {
            "access_token": token(client="portal", audience=API, scopes=USER_SCOPES),
            "refresh_token": "provider-refresh-value",
            "id_token": token(
                audience="portal",
                headers={"typ": "JWT"},
                nonce=nonce,
                email="a@example.com",
                email_verified=True,
                name="User A",
            ),
        }

    monkeypatch.setattr(portal, "_token", exchange)
    completed = await oauth_client.get(callback)
    assert completed.status_code == 303
    assert completed.headers.get_list("referrer-policy") == ["no-referrer"]
    assert "HttpOnly" in completed.headers["set-cookie"]
    assert "SameSite=lax" in completed.headers["set-cookie"]
    assert (await oauth_client.get(callback)).status_code == 401
    page = await oauth_client.get("/account")
    # Native form POSTs send Origin: null under no-referrer. The middleware must
    # preserve the portal's same-origin policy without adding a second override.
    assert page.headers.get_list("referrer-policy") == ["same-origin"]
    assert "User A" in page.text
    assert "provider-refresh-value" not in page.text
    assert "csrf" in page.text
    async with harness.runtime.session_factory() as session:
        rows = list((await session.execute(select(PortalSession))).scalars())
        assert len(rows) == 1
        assert "provider-refresh-value" not in rows[0].encrypted_data
        data = portal._decrypt(harness.runtime.settings, rows[0].encrypted_data)
    assert (await oauth_client.post("/account/pair", data={"csrf": "wrong"})).status_code == 403
    assert (
        await oauth_client.post(
            "/account/pair",
            data={"csrf": data["csrf"]},
            headers={"Origin": "https://attacker.example"},
        )
    ).status_code == 403
    assert (
        await oauth_client.post(
            "/account/pair", data={"csrf": data["csrf"]}, headers={"Origin": "null"}
        )
    ).status_code == 403
    assert (
        await oauth_client.post(
            "/account/pair", data={"csrf": data["csrf"]}, headers={"Origin": ORIGIN}
        )
    ).status_code == 200
    assert (
        await oauth_client.post("/account/logout", data={"csrf": data["csrf"]})
    ).status_code == 303
    assert "Sign in or create an account" in (await oauth_client.get("/account")).text


async def test_worker_mode_has_no_mcp_or_customer_routes(harness):
    harness.runtime.settings.service_mode = ServiceMode.WORKER
    app = create_app(runtime=harness.runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/mcp")).status_code == 404
        assert (await client.get("/api/v1/settings")).status_code == 404
        assert (await client.get("/login")).status_code == 404
        assert (await client.post("/internal/worker/run")).status_code == 404  # no trigger secret


async def test_durable_user_quota_spans_clients_and_runtime_instances(auth, token, harness):
    harness.runtime.settings.user_requests_per_minute = 2
    a = await auth.authenticate(token(), for_mcp=True)
    b = await auth.authenticate(token(client="another-agent"), for_mcp=True)
    for principal in (a, b):
        async with harness.runtime.unit(principal):
            pass
    # Disabling the process-local limiter does not reset the database account quota.
    harness.runtime.rate_limiter = None
    from agentping.domain.errors import RateLimited

    with pytest.raises(RateLimited):
        async with harness.runtime.unit(a):
            pass


async def test_closed_account_cannot_recreate_or_use_existing_credentials(oauth_client, token):
    owner = {"Authorization": "Bearer " + token(client="portal", audience=API, scopes=USER_SCOPES)}
    issued = await oauth_client.post(
        "/api/v1/agents", headers=owner, json={"name": "Closure test", "scopes": ["reminders:read"]}
    )
    opaque = issued.json()["token"]
    assert (
        await oauth_client.post("/api/v1/me/close", headers=owner, json={"confirmation": "WRONG"})
    ).status_code == 422
    assert (
        await oauth_client.post("/api/v1/me/close", headers=owner, json={"confirmation": "CLOSE"})
    ).status_code == 204
    assert (await oauth_client.get("/api/v1/settings", headers=owner)).status_code == 401
    assert (
        await oauth_client.get("/api/v1/reminders", headers={"Authorization": "Bearer " + opaque})
    ).status_code == 401


async def test_concurrent_first_login_creates_one_identity(auth, token, harness):
    import asyncio

    principals = await asyncio.gather(
        *[auth.authenticate(token(sub="new-user"), for_mcp=True) for _ in range(5)]
    )
    assert len({p.user_id for p in principals}) == 1
    async with harness.runtime.session_factory() as session:
        assert len(list((await session.execute(select(UserIdentity))).scalars())) == 1


async def test_portal_refresh_rotates_encrypted_token_and_rejects_identity_switch(
    oauth_client, auth, token, harness, monkeypatch
):
    principal = await auth.authenticate(token(client="portal", audience=API, scopes=USER_SCOPES))
    raw = secrets.token_urlsafe(32)
    async with harness.runtime.session_factory() as session, session.begin():
        session.add(
            PortalSession(
                token_hash=portal._hash(raw),
                user_id=principal.user_id,
                encrypted_data=portal._encrypt(
                    harness.runtime.settings,
                    {
                        "access_token": "expired-access",
                        "refresh_token": "refresh-old",
                        "expires_at": 0,
                        "subject": "user-a",
                        "csrf": "csrf-example",
                    },
                ),
                expires_at=harness.clock.now() + portal.timedelta(days=1),
            )
        )
    oauth_client.cookies.set("agentping_session", raw)

    async def exchange(request, body):
        assert body["refresh_token"] == "refresh-old"
        return {
            "access_token": token(client="portal", audience=API, scopes=USER_SCOPES),
            "refresh_token": "refresh-new",
        }

    monkeypatch.setattr(portal, "_token", exchange)
    assert "Your account" in (await oauth_client.get("/account")).text
    async with harness.runtime.session_factory() as session, session.begin():
        row = await session.get(PortalSession, portal._hash(raw))
        data = portal._decrypt(harness.runtime.settings, row.encrypted_data)
        assert data["refresh_token"] == "refresh-new"
        data["expires_at"] = 0
        row.encrypted_data = portal._encrypt(harness.runtime.settings, data)

    async def switched(request, body):
        return {
            "access_token": token(sub="user-b", client="portal", audience=API, scopes=USER_SCOPES)
        }

    monkeypatch.setattr(portal, "_token", switched)
    assert "Sign in or create an account" in (await oauth_client.get("/account")).text


async def test_oidc_mode_cannot_bootstrap_development_owner(harness):
    from agentping.auth.service import ensure_user

    async with harness.runtime.session_factory() as session, session.begin():
        with pytest.raises(Unauthenticated):
            await ensure_user(session, harness.runtime)


async def test_owner_can_allow_new_login_without_reviving_old_tokens(auth, token, harness):
    from agentping.services import agents

    harness.clock.set(datetime.now(UTC))
    old = token(iat=int(time.time()) - 10)
    agent = await auth.authenticate(old, for_mcp=True)
    owner = await auth.authenticate(token(client="portal", audience=API, scopes=USER_SCOPES))
    issued = await harness.call(owner, agents.issue_token, agent.agent_client_id, 1)
    await harness.call(owner, agents.revoke_agent, agent.agent_client_id)
    await harness.call(owner, agents.reauthorize_agent, agent.agent_client_id)
    with pytest.raises(Unauthenticated):
        await auth.authenticate(old, for_mcp=True)
    with pytest.raises(Unauthenticated):
        await auth.authenticate(issued.token)
    fresh = await auth.authenticate(token(iat=int(time.time()) + 3), for_mcp=True)
    assert fresh.agent_client_id == agent.agent_client_id
    assert fresh.scopes == agent.scopes


async def test_other_account_and_agent_cannot_restore_grant(auth, token, harness):
    from agentping.domain.errors import Forbidden, NotFound
    from agentping.services import agents

    agent = await auth.authenticate(token(), for_mcp=True)
    other = await auth.authenticate(
        token(sub="user-b", client="portal", audience=API, scopes=USER_SCOPES)
    )
    with pytest.raises(NotFound):
        await harness.call(other, agents.reauthorize_agent, agent.agent_client_id)
    with pytest.raises(Forbidden):
        await harness.call(agent, agents.reauthorize_agent, agent.agent_client_id)


async def test_registered_app_has_recognizable_name(auth, token, harness):
    harness.runtime.settings.oidc_agent_client_names = {"codex": "Codex"}
    principal = await auth.authenticate(token(), for_mcp=True)
    assert principal.agent_name.startswith("Codex · ")
