"""Provider settings must match the strict API and portal token verifier."""

from copy import deepcopy

import auth0_setup


def test_provider_configuration_pins_id_and_access_token_algorithms(monkeypatch, tmp_path):
    calls = []
    clients = {}

    def api(method, path, data=None):
        calls.append((method, path, deepcopy(data)))
        if method == "get":
            if path == "tenants/settings":
                return {}
            if path in ("resource-servers", "clients", "client-grants"):
                return []
            if path == "connections":
                return [
                    {"id": "database", "strategy": "auth0", "options": {}},
                    {"id": "google", "strategy": "google-oauth2", "options": {}},
                ]
            return clients[path.removeprefix("clients/")]
        if method == "post" and path == "clients":
            client_id = "portal" if data["app_type"] == "regular_web" else "native"
            clients[client_id] = {
                **data,
                "client_id": client_id,
                "client_secret": "test-only-secret",
            }
            return clients[client_id]
        return {}

    monkeypatch.setattr(auth0_setup, "api", api)
    monkeypatch.setattr(auth0_setup, "PRIVATE", tmp_path)
    monkeypatch.setattr(auth0_setup, "save", lambda *args: None)
    auth0_setup.configure()
    assert len(clients) == 2
    assert all(c["jwt_configuration"]["alg"] == "RS256" for c in clients.values())
    resources = [
        body for method, path, body in calls if method == "post" and path == "resource-servers"
    ]
    assert all(
        r["signing_alg"] == "RS256"
        and r["token_dialect"] == "rfc9068_profile"
        and r["allow_offline_access"]
        for r in resources
    )
    assert all(
        c["grant_types"] == ["authorization_code", "refresh_token"] for c in clients.values()
    )
    assert clients["native"]["token_endpoint_auth_method"] == "none"
    grants = [body for method, path, body in calls if method == "post" and path == "client-grants"]
    assert all(g["subject_type"] == "user" for g in grants)
    native = next(g for g in grants if g["client_id"] == "native")
    assert "agents:manage" not in native["scope"]
    assert "alerts:critical" not in native["scope"]
