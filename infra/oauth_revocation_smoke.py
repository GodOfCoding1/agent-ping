"""Check real OAuth revocation and explicit owner reapproval without leaving Codex blocked."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime

import httpx
import psycopg
from agentping.auth.oidc import OidcVerifier
from agentping.config import Settings
from cryptography.fernet import Fernet
from oauth_live_smoke import resource
from oauth_production import db_url
from production import ORIGIN, read, save


async def verify() -> None:
    auth = read("auth0.json")
    verifier = OidcVerifier(
        Settings(
            auth_mode="oidc",
            oidc_issuer=auth["issuer"],
            oidc_audience=auth["audience"],
            public_base_url=ORIGIN,
        )
    )

    async def refresh_mcp() -> dict:
        tokens = read("live-user-a-refreshed-tokens.json")
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.post(
                auth["issuer"] + "oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "client_id": auth["codex_client_id"],
                    "refresh_token": tokens["refresh_token"],
                },
            )
        assert response.status_code == 200
        renewed = response.json()
        save("live-user-a-refreshed-tokens.json", renewed)
        await verifier.verify(renewed["access_token"])
        return renewed

    try:
        initial = await refresh_mcp()
        signed = await verifier.verify(initial["access_token"])
        cipher = Fernet(read("portal-key.json")["key"].encode())
        with psycopg.connect(db_url(read("database.json")["runtime_url"])) as connection:
            row = connection.execute(
                "SELECT user_id FROM user_identities WHERE issuer=%s AND subject=%s",
                (auth["issuer"], signed.subject),
            ).fetchone()
            assert row
            user_id = row[0]
            row = connection.execute(
                "SELECT token_hash,encrypted_data FROM portal_sessions "
                "WHERE user_id=%s AND expires_at>now() "
                "ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
                (user_id,),
            ).fetchone()
            assert row, "A real signed-in portal session is required"
            data = json.loads(cipher.decrypt(row[1].encode()))
            assert data["subject"] == signed.subject
            if data["expires_at"] < datetime.now(UTC).timestamp() + 60:
                response = httpx.post(
                    auth["issuer"] + "oauth/token",
                    json={
                        "grant_type": "refresh_token",
                        "client_id": auth["portal_client_id"],
                        "client_secret": auth["portal_client_secret"],
                        "refresh_token": data["refresh_token"],
                    },
                    timeout=30,
                )
                assert response.status_code == 200
                token = response.json()
                verified = await verifier.verify(
                    token["access_token"], audience=auth["api_audience"]
                )
                assert (
                    verified.subject == signed.subject
                    and verified.client_id == auth["portal_client_id"]
                )
                data.update(
                    access_token=token["access_token"],
                    refresh_token=token.get("refresh_token", data["refresh_token"]),
                    expires_at=verified.claims["exp"],
                )
                connection.execute(
                    "UPDATE portal_sessions SET encrypted_data=%s WHERE token_hash=%s",
                    (cipher.encrypt(json.dumps(data).encode()).decode(), row[0]),
                )
            row = connection.execute(
                "SELECT id FROM agent_clients WHERE user_id=%s AND external_client_id=%s",
                (user_id, auth["codex_client_id"]),
            ).fetchone()
            assert row
            agent_id = str(row[0])
        owner = {"Authorization": "Bearer " + data["access_token"]}
        async with httpx.AsyncClient(timeout=60) as http:
            profile = await http.get(ORIGIN + "/api/v1/me", headers=owner)
            assert profile.status_code == 200 and profile.json()["id"] == str(user_id)
            paired = await http.post(ORIGIN + "/api/v1/devices/pairing-codes", headers=owner)
            assert paired.status_code == 201
            registered = await http.post(
                ORIGIN + "/api/v1/devices/register",
                json={
                    "pairing_code": paired.json()["code"],
                    "installation_id": "oauth-verification-" + secrets.token_hex(8),
                    "name": "OAuth verification (no phone)",
                    "model": "backend-check",
                    "app_version": "backend-check",
                    "push_token": None,
                },
            )
            assert registered.status_code == 201
            device = registered.json()
            try:
                synced = await http.get(
                    ORIGIN + "/api/v1/sync",
                    headers={"Authorization": "Bearer " + device["device_token"]},
                )
                assert synced.status_code == 200
            finally:
                removed = await http.delete(
                    ORIGIN + "/api/v1/devices/" + device["device"]["id"], headers=owner
                )
                assert removed.status_code == 200
                assert removed.json()["status"] == "deactivated"
            # A delegated MCP token cannot be used as a first-party owner API token.
            wrong = await http.get(
                ORIGIN + "/api/v1/me",
                headers={"Authorization": "Bearer " + initial["access_token"]},
            )
            assert wrong.status_code == 401
            await resource(initial["access_token"])
            revoked = False
            restored = False

            async def denied(raw: str) -> bool:
                response = await http.post(
                    ORIGIN + "/mcp",
                    headers={"Authorization": "Bearer " + raw},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                return response.status_code == 401

            try:
                response = await http.post(
                    ORIGIN + f"/api/v1/agents/{agent_id}/revoke", headers=owner
                )
                assert response.status_code == 200
                revoked = True
                assert await denied(initial["access_token"])
                renewed = await refresh_mcp()
                assert await denied(renewed["access_token"])
                response = await http.post(
                    ORIGIN + f"/api/v1/agents/{agent_id}/reauthorize", headers=owner
                )
                assert response.status_code == 200
                restored = True
                assert await denied(initial["access_token"])
                assert await denied(renewed["access_token"])
                await asyncio.sleep(2)
                fresh = await refresh_mcp()
                await resource(fresh["access_token"])
            finally:
                if revoked and not restored:
                    response = await http.post(
                        ORIGIN + f"/api/v1/agents/{agent_id}/reauthorize", headers=owner
                    )
                    assert response.status_code == 200, "Grant recovery requires attention"
        proof = {
            "owner_portal_api_verified": True,
            "pairing_and_device_sync_verified": True,
            "mcp_audience_cannot_be_owner": True,
            "existing_and_refreshed_tokens_denied_after_revocation": True,
            "old_tokens_stay_invalid_after_explicit_reapproval": True,
            "fresh_token_works_after_reapproval": True,
            "codex_grant_active": True,
        }
        save("oauth-revocation-proof.json", proof)
        print(json.dumps(proof))
    finally:
        await verifier.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(verify())
    except Exception as error:
        import traceback

        save(
            "oauth-revocation-error.json",
            {
                "type": type(error).__name__,
                "frames": [
                    {"file": f.filename, "line": f.lineno}
                    for f in traceback.extract_tb(error.__traceback__)
                ],
            },
        )
        print("Live revocation check failed; credentials remain private.")
        raise SystemExit(1) from None
