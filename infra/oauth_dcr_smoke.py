"""Verify live automatic registration; does not sign in or mint user tokens.

Creates one public verification client via anonymous DCR, reusing it on reruns.
The receipt stays private. Browser consent and real ChatGPT tool use are separate.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import urljoin, urlsplit

import auth0_setup as auth0
import httpx
from auth0_onboarding import MCP_AUDIENCE, inspect_onboarding, summarize
from production import PRIVATE, read, save

RECEIPT = "oauth-dcr-verification-client.json"
CALLBACK = "http://127.0.0.1:8744/callback"


def verify() -> dict[str, object]:
    if not summarize(inspect_onboarding())["ready"]:
        raise RuntimeError("Automatic onboarding policy is not ready")
    with httpx.Client(timeout=30, follow_redirects=False) as http:
        response = http.get(auth0.ISSUER + ".well-known/oauth-authorization-server")
        response.raise_for_status()
        metadata = response.json()
        if (
            metadata.get("issuer") != auth0.ISSUER
            or metadata.get("registration_endpoint") != auth0.ISSUER + "oidc/register"
            or metadata.get("client_id_metadata_document_supported", False)
            or "S256" not in metadata.get("code_challenge_methods_supported", [])
        ):
            raise RuntimeError("Public OAuth discovery does not advertise the expected DCR flow")
        if (PRIVATE / RECEIPT).exists():
            client = read(RECEIPT)
        else:
            registered = http.post(
                metadata["registration_endpoint"],
                json={
                    "client_name": "AgentPing automatic onboarding verification",
                    "redirect_uris": [CALLBACK],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )
            if registered.status_code != 201:
                raise RuntimeError(f"Anonymous registration failed (HTTP {registered.status_code})")
            client = registered.json()
            save(RECEIPT, client)
        client_id = client["client_id"]
        managed = auth0.api("get", "clients/" + client_id)
        if (
            not client_id.startswith("tpc_")
            or managed.get("is_first_party") is not False
            or managed.get("third_party_security_mode") != "strict"
            or managed.get("token_endpoint_auth_method") != "none"
            or set(managed.get("grant_types", [])) != {"authorization_code", "refresh_token"}
        ):
            raise RuntimeError("DCR client did not receive the expected third-party policy")
        refresh = managed.get("refresh_token", {})
        if (
            refresh.get("rotation_type") != "rotating"
            or refresh.get("expiration_type") != "expiring"
            or refresh.get("infinite_token_lifetime") is not False
            or refresh.get("infinite_idle_token_lifetime") is not False
        ):
            raise RuntimeError("DCR refresh-token rotation and finite lifetimes are required")
        if any(g.get("client_id") == client_id for g in inspect_onboarding()["grants"]):
            raise RuntimeError("Verification client unexpectedly has per-application grants")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": CALLBACK,
            "resource": MCP_AUDIENCE,
            "scope": "openid email offline_access " + " ".join(auth0.MCP_SCOPES),
            "state": secrets.token_urlsafe(32),
            "code_challenge": challenge.rstrip("="),
            "code_challenge_method": "S256",
        }
        authorized = http.get(auth0.ISSUER + "authorize", params=params)
        destination_url = urljoin(str(authorized.url), authorized.headers.get("location", ""))
        destination = urlsplit(destination_url)
        issuer = urlsplit(auth0.ISSUER)
        save(
            "oauth-dcr-authorization-probe.json",
            {
                "status": authorized.status_code,
                "location": authorized.headers.get("location"),
                "body": authorized.text,
            },
        )
        if (
            authorized.status_code != 302
            or destination.netloc != issuer.netloc
            or not destination.path.startswith("/u/login")
        ):
            raise RuntimeError("New client did not reach Auth0 browser login")
        login = http.get(destination_url)
        if login.status_code != 200 or "google" not in login.text.lower():
            raise RuntimeError("Google browser login did not load for the new client")
        result = {
            "anonymous_registration": True,
            "strict_third_party_client": True,
            "per_client_grant_required": False,
            "public_discovery_advertises_dcr": True,
            "authorization_reaches_login": True,
            "browser_consent_and_token_exchange_tested": False,
            "refresh_token_policy": managed.get("refresh_token", {}),
        }
        save("oauth-dcr-verification.json", result)
        return result


if __name__ == "__main__":
    try:
        print(json.dumps(verify(), sort_keys=True))
    except Exception as error:
        print(f"Live DCR verification failed ({type(error).__name__}); no credentials printed.")
        raise SystemExit(1) from None
