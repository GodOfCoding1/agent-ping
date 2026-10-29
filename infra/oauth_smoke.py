"""Verify private OAuth deployment boundaries without pretending to be a signed-in user.

This deliberately leaves the public-release gate closed until real browser OAuth,
refresh, reconnect, revocation and a second independent account are proven.
"""

from __future__ import annotations

import json
import traceback

import httpx
from auth0_setup import MCP_SCOPES
from oauth_production import WORKER, WORKER_ORIGIN
from production import (
    ORIGIN,
    PROJECT,
    QUEUE,
    REGION,
    SERVICE,
    gcloud,
    google_api,
    read,
    save,
)


def verify() -> None:
    iam = gcloud("auth", "print-identity-token")
    gateway = {"X-Serverless-Authorization": "Bearer " + iam}
    auth = read("auth0.json")
    with httpx.Client(timeout=90) as http:

        def get(path: str) -> httpx.Response:
            return http.get(ORIGIN + path, headers=gateway)

        assert get("/health").status_code == 200
        assert get("/readyz").status_code == 401
        metadata = get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == ORIGIN + "/mcp"
        assert metadata.json()["authorization_servers"] == [auth["issuer"]]
        assert set(metadata.json()["scopes_supported"]) == set(MCP_SCOPES)
        assert get("/account").status_code == 200
        login = get("/login")
        assert login.status_code == 303
        assert login.headers["location"].startswith(auth["issuer"] + "authorize?")
        assert "code_challenge_method=S256" in login.headers["location"]
        assert "client_secret=" not in login.headers["location"]
        assert "Secure" in login.headers["set-cookie"] and "HttpOnly" in login.headers["set-cookie"]
        missing = http.post(
            ORIGIN + "/mcp",
            headers=gateway,
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )
        assert (
            missing.status_code == 401
            and "resource_metadata=" in missing.headers["www-authenticate"]
        )
        invalid = http.get(
            ORIGIN + "/api/v1/settings", headers={**gateway, "Authorization": "Bearer invalid"}
        )
        assert invalid.status_code == 401
        assert (
            http.post(
                ORIGIN + "/internal/worker/run",
                headers={**gateway, "X-Worker-Token": read("database.json")["worker_token"]},
            ).status_code
            == 404
        )
        assert http.get(WORKER_ORIGIN + "/health").status_code == 403
        assert http.post(WORKER_ORIGIN + "/internal/worker/run", headers=gateway).status_code == 401
        assert http.get(WORKER_ORIGIN + "/mcp", headers=gateway).status_code == 404
        assert http.get(WORKER_ORIGIN + "/api/v1/diagnostics", headers=gateway).status_code == 404
        worker = http.post(
            WORKER_ORIGIN + "/internal/worker/run",
            headers={**gateway, "X-Worker-Token": read("database.json")["worker_token"]},
        )
        assert worker.status_code == 200
        provider = http.get(auth["issuer"] + ".well-known/openid-configuration").json()
        assert provider["issuer"] == auth["issuer"]
        assert "S256" in provider["code_challenge_methods_supported"]
        assert provider["authorization_response_iss_parameter_supported"] is True
    for service in (SERVICE, WORKER):
        policy = json.loads(
            gcloud(
                "run", "services", "get-iam-policy", service, f"--region={REGION}", "--format=json"
            )
        )
        assert not any(
            m in ("allUsers", "allAuthenticatedUsers")
            for b in policy.get("bindings", [])
            for m in b["members"]
        )
    scheduler = google_api(
        "GET",
        f"https://cloudscheduler.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/jobs/agentping-recovery",
    )
    assert scheduler["httpTarget"]["uri"] == WORKER_ORIGIN + "/internal/worker/run"
    tasks = google_api(
        "GET",
        f"https://cloudtasks.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/queues/{QUEUE}/tasks",
    )
    targets = [task["httpRequest"]["url"] for task in tasks.get("tasks", [])]
    assert targets and all(url == WORKER_ORIGIN + "/internal/worker/run" for url in targets)
    proof = {
        "infrastructure_passed": True,
        "passed": False,
        "browser_oauth_verified": False,
        "two_live_users_verified": False,
        "public_release": False,
        "schema": read("oauth-migration-result.json")["schema"],
        "scheduled_targets": len(targets),
        "worker_private": True,
        "api_private_pending_browser_verification": True,
    }
    save("oauth-private-smoke.json", proof)
    print(json.dumps(proof))


if __name__ == "__main__":
    try:
        verify()
    except Exception as error:
        save(
            "oauth-smoke-error.json",
            {
                "type": type(error).__name__,
                "message": str(error),
                "frames": [
                    {"file": f.filename, "line": f.lineno}
                    for f in traceback.extract_tb(error.__traceback__)
                ],
            },
        )
        print(f"Private OAuth smoke failed ({type(error).__name__}); details remain private.")
        raise SystemExit(1) from None
