"""Configure the approved Auth0 tenant without exposing credentials in command output.

Requires an interactive `auth0 login` approved by the tenant owner. No paid features.
Run: uv run --project backend --locked python infra/auth0_setup.py
"""

from __future__ import annotations

import json
from typing import Any

from production import ORIGIN, PRIVATE, read, run, save

DOMAIN = "dev-olgiqoud72gt2u4h.us.auth0.com"
ISSUER = f"https://{DOMAIN}/"
MCP_SCOPES = {
    "reminders:read": "Read your reminders",
    "reminders:write": "Create and change your reminders",
    "notifications:send": "Send notifications to your devices",
    "devices:read": "View your paired devices",
    "activity:read": "Read your activity",
}
USER_SCOPES = {
    **MCP_SCOPES,
    "alerts:critical": "Manage critical alerts",
    "devices:manage": "Pair and manage your devices",
    "settings:write": "Change your preferences",
    "agents:manage": "Manage and revoke your agent authorizations",
}


def api(method: str, path: str, data: Any = None, *, query: dict[str, str] | None = None) -> Any:
    args = ["auth0", "api", method, path, "--tenant", DOMAIN, "--json", "--reveal-secrets"]
    for key, value in (query or {}).items():
        args += ["--query", f"{key}={value}"]
    if data is not None:
        args += ["--data", "@-"]
    raw = run(args, input_=json.dumps(data) if data is not None else None)
    return json.loads(raw) if raw else {}


def configure() -> None:
    tenant = api("get", "tenants/settings")
    save("auth0-tenant-before.json", tenant)
    api(
        "patch",
        "tenants/settings",
        {
            "friendly_name": "AgentPing",
            "resource_parameter_profile": "compatibility",
            "authorization_response_iss_parameter_supported": True,
        },
    )
    servers = api("get", "resource-servers")
    for name, audience, scopes in [
        ("AgentPing MCP", ORIGIN + "/mcp", MCP_SCOPES),
        ("AgentPing Account API", ORIGIN + "/api/v1", USER_SCOPES),
    ]:
        body = {
            "name": name,
            "signing_alg": "RS256",
            "token_dialect": "rfc9068_profile",
            "token_lifetime": 600,
            "token_lifetime_for_web": 600,
            "allow_offline_access": True,
            "skip_consent_for_verifiable_first_party_clients": False,
            "enforce_policies": False,
            "scopes": [{"value": value, "description": desc} for value, desc in scopes.items()],
            "subject_type_authorization": {
                "user": {"policy": "require_client_grant"},
                "client": {"policy": "deny_all"},
            },
        }
        existing = next((r for r in servers if r["identifier"] == audience), None)
        if existing:
            api("patch", "resource-servers/" + existing["id"], body)
        else:
            api("post", "resource-servers", {"identifier": audience, **body})
    clients = api("get", "clients")
    refresh = {
        "rotation_type": "rotating",
        "expiration_type": "expiring",
        "leeway": 10,
        "token_lifetime": 2592000,
        "idle_token_lifetime": 604800,
        "infinite_token_lifetime": False,
        "infinite_idle_token_lifetime": False,
    }
    definitions = [
        {
            "name": "AgentPing Account",
            "app_type": "regular_web",
            "is_first_party": True,
            "token_endpoint_auth_method": "client_secret_post",
            "oidc_conformant": True,
            "jwt_configuration": {"alg": "RS256", "lifetime_in_seconds": 600},
            "callbacks": [ORIGIN + "/auth/callback"],
            "allowed_logout_urls": [ORIGIN + "/account"],
            "grant_types": ["authorization_code", "refresh_token"],
            "refresh_token": refresh,
        },
        {
            "name": "Codex AgentPing",
            "app_type": "native",
            "is_first_party": False,
            "token_endpoint_auth_method": "none",
            "oidc_conformant": True,
            "jwt_configuration": {"alg": "RS256", "lifetime_in_seconds": 600},
            "callbacks": ["http://127.0.0.1:8743/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "refresh_token": refresh,
        },
    ]
    configured = []
    for body in definitions:
        existing = next((c for c in clients if c["name"] == body["name"]), None)
        if existing:
            body.pop("is_first_party")  # immutable after registration
            client = api("patch", "clients/" + existing["client_id"], body)
        else:
            client = api("post", "clients", body)
        configured.append(client)
    save("auth0-clients-configured.json", configured)
    grants = api("get", "client-grants")
    for client, audience, scopes in [
        (configured[0], ORIGIN + "/api/v1", USER_SCOPES),
        (configured[1], ORIGIN + "/mcp", MCP_SCOPES),
    ]:
        body = {
            "client_id": client["client_id"],
            "audience": audience,
            "subject_type": "user",
            "scope": list(scopes),
        }
        existing = next(
            (
                g
                for g in grants
                if g.get("client_id") == body["client_id"]
                and g["audience"] == audience
                and g.get("subject_type") == "user"
            ),
            None,
        )
        if existing:
            api("patch", "client-grants/" + existing["id"], {"scope": list(scopes)})
        else:
            api("post", "client-grants", body)
    # Database login is production supported without a third-party social developer key.
    # Google remains disabled for these clients until own production Google keys are configured.
    connections = api("get", "connections")
    google_ready = (PRIVATE / "google-oauth.json").exists()
    google = read("google-oauth.json") if google_ready else None
    for connection in connections:
        if connection["strategy"] in {"auth0", "google-oauth2"}:
            enabled = (connection["strategy"] == "google-oauth2") == google_ready
            patch: dict[str, Any] = {"is_domain_connection": enabled}
            if enabled and google is not None:
                patch["options"] = {
                    **connection.get("options", {}),
                    "client_id": google["client_id"],
                    "client_secret": google["client_secret"],
                    "scope": ["email", "profile"],
                    "email": True,
                    "profile": True,
                }
            api("patch", "connections/" + connection["id"], patch)
            api(
                "patch",
                "connections/" + connection["id"] + "/clients",
                [{"client_id": configured[0]["client_id"], "status": enabled}],
            )
    portal = api("get", "clients/" + configured[0]["client_id"])
    save(
        "auth0.json",
        {
            "issuer": ISSUER,
            "audience": ORIGIN + "/mcp",
            "api_audience": ORIGIN + "/api/v1",
            "portal_client_id": portal["client_id"],
            "portal_client_secret": portal["client_secret"],
            "codex_client_id": configured[1]["client_id"],
            "codex_callback_url": "http://127.0.0.1:8743/callback",
            "codex_callback_port": 8743,
        },
    )
    from auth0_onboarding import configure_onboarding

    configure_onboarding()
    print("Auth0 APIs and clients configured; confidential state saved privately.")
    print("Account client:", portal["client_id"])
    print("Codex public client:", configured[1]["client_id"])


if __name__ == "__main__":
    try:
        configure()
    except Exception as error:
        print(f"Auth0 configuration failed ({type(error).__name__}); details remain private.")
        raise SystemExit(1) from None
