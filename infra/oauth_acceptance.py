"""One-use native OAuth acceptance harness, using the registered public Codex client.

This is deployment verification, not the customer's connection path. Codex has
already completed its own native login. No IAM credentials or helper is used here.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from auth0_setup import MCP_SCOPES
from production import read, save


def login(label: str, *, choose_account: bool = False) -> None:
    auth = read("auth0.json")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    received: dict[str, str] = {}
    complete = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/complete":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<h1>OAuth verification completed</h1><p>You can close this tab.</p>"
                )
                return
            values = parse_qs(parsed.query)
            if (
                parsed.path != "/callback"
                or values.get("state") != [state]
                or values.get("iss") != [auth["issuer"]]
            ):
                self.send_error(400, "Invalid OAuth callback")
                return
            if complete.is_set():
                self.send_error(400, "Callback already consumed")
                return
            if values.get("error"):
                received["error"] = "Authorization not completed"
            elif len(values.get("code", [""])[0]) < 4096:
                received["code"] = values.get("code", [""])[0]
            self.send_response(303)
            self.send_header("Location", "/complete")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            complete.set()

    server = ThreadingHTTPServer(("127.0.0.1", 8743), Callback)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    params = {
        "client_id": auth["codex_client_id"],
        "response_type": "code",
        "redirect_uri": auth["codex_callback_url"],
        "resource": auth["audience"],
        "scope": "offline_access " + " ".join(MCP_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if choose_account:
        params.update(prompt="login", connection="google-oauth2")
    save(f"{label}-authorization.json", {"url": auth["issuer"] + "authorize?" + urlencode(params)})
    print("Native OAuth verification listener ready; browser URL saved privately.", flush=True)
    try:
        if not complete.wait(600) or not received.get("code"):
            raise RuntimeError("Browser authorization was not completed")
        response = httpx.post(
            auth["issuer"] + "oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": auth["codex_client_id"],
                "redirect_uri": auth["codex_callback_url"],
                "code_verifier": verifier,
                "code": received["code"],
            },
            timeout=30,
        )
        if response.is_error:
            raise RuntimeError(f"Token exchange failed (HTTP {response.status_code})")
        data = response.json()
        if not data.get("access_token") or not data.get("refresh_token"):
            raise RuntimeError("Expected access and refresh tokens")
        save(f"{label}-tokens.json", data)
        print("Native authorization-code/PKCE exchange and refresh issuance verified.", flush=True)
        time.sleep(2)  # Let the browser finish its token-free /complete redirect.
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("label", choices=["live-user-a", "live-user-b"])
    parser.add_argument("--choose-account", action="store_true")
    args = parser.parse_args()
    try:
        login(args.label, choose_account=args.choose_account)
    except Exception as error:
        save("oauth-acceptance-error.json", {"type": type(error).__name__})
        print("Native OAuth verification did not complete; no credentials printed.")
        raise SystemExit(1) from None
