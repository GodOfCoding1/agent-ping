"""Automatic registration must not open account management or machine access."""

from copy import deepcopy

import auth0_onboarding as onboarding
import pytest


@pytest.fixture
def provider(monkeypatch):
    state = {
        "tenant": {
            "client_id_metadata_document_supported": True,
            "resource_parameter_profile": "compatibility",
            "authorization_response_iss_parameter_supported": True,
            "flags": {"enable_sso": True},
        },
        "resources": [
            {
                "identifier": audience,
                "subject_type_authorization": {
                    "user": {"policy": "require_client_grant"},
                    "client": {"policy": "deny_all"},
                },
                "skip_consent_for_verifiable_first_party_clients": False,
                "signing_alg": "RS256",
                "token_dialect": "rfc9068_profile",
                "token_lifetime": 600,
                "allow_offline_access": True,
            }
            for audience in (onboarding.MCP_AUDIENCE, onboarding.ACCOUNT_AUDIENCE)
        ],
        "grants": [
            {"id": "portal", "client_id": "portal", "audience": onboarding.ACCOUNT_AUDIENCE},
            {"id": "codex", "client_id": "codex", "audience": onboarding.MCP_AUDIENCE},
        ],
        "connections": [{"strategy": "google-oauth2", "is_domain_connection": True}],
    }
    mutations = []
    snapshots = []

    def api(method, path, data=None, *, query=None):
        if method == "get":
            name = {
                "tenants/settings": "tenant",
                "resource-servers": "resources",
                "client-grants": "grants",
                "connections": "connections",
            }[path]
            values = state[name]
            if isinstance(values, list):
                start = int(query["page"]) * int(query["per_page"])
                values = values[start : start + int(query["per_page"])]
            return deepcopy(values)
        mutations.append((method, path, deepcopy(data)))
        if method == "patch" and path == "tenants/settings":
            # Auth0 merges tenant flags; DCR is nested, whereas CIMD is top-level.
            for key, value in data.items():
                if key == "flags":
                    state["tenant"]["flags"].update(value)
                else:
                    state["tenant"][key] = value
        elif method == "post" and path == "client-grants":
            state["grants"].append({"id": "default", **data})
        elif method == "patch" and path == "client-grants/default":
            next(g for g in state["grants"] if g["id"] == "default").update(data)
        else:
            raise AssertionError((method, path))
        return {}

    monkeypatch.setattr(onboarding.auth0, "api", api)
    monkeypatch.setattr(onboarding, "save", lambda name, data: snapshots.append((name, data)))
    return state, mutations, snapshots


def test_onboarding_is_limited_ordered_and_idempotent(provider):
    state, mutations, snapshots = provider
    explicit_grants = deepcopy(state["grants"])
    assert onboarding.configure_onboarding()["ready"]
    assert mutations[0] == (
        "post",
        "client-grants",
        {
            "default_for": "third_party_clients",
            "audience": onboarding.MCP_AUDIENCE,
            "subject_type": "user",
            "scope": sorted(onboarding.auth0.MCP_SCOPES),
        },
    )
    assert mutations[1] == (
        "patch",
        "tenants/settings",
        {
            "client_id_metadata_document_supported": False,
            "flags": {"enable_dynamic_client_registration": True},
        },
    )
    assert state["tenant"]["flags"]["enable_sso"] is True
    assert state["grants"][:2] == explicit_grants
    assert snapshots[0][0].startswith("auth0-onboarding-before-")
    assert snapshots[0][1]["tenant"]["client_id_metadata_document_supported"] is True
    mutations.clear()
    assert onboarding.configure_onboarding()["ready"]
    assert mutations == []


@pytest.mark.parametrize(
    "change",
    [
        "account_default",
        "machine_default",
        "cimd_active",
        "account_open",
        "consent_skipped",
        "google_missing",
    ],
)
def test_unsafe_boundaries_fail_before_any_mutation(provider, change):
    state, mutations, snapshots = provider
    if change in {"account_default", "machine_default"}:
        state["grants"].append(
            {
                "default_for": "third_party_clients",
                "audience": (
                    onboarding.ACCOUNT_AUDIENCE
                    if change == "account_default"
                    else onboarding.MCP_AUDIENCE
                ),
                "subject_type": "client" if change == "machine_default" else "user",
            }
        )
    elif change == "cimd_active":
        state["tenant"]["client_id_metadata_document_dynamic_registration_supported"] = True
    elif change == "account_open":
        state["resources"][1]["subject_type_authorization"]["user"]["policy"] = "allow_all"
    elif change == "consent_skipped":
        state["resources"][0]["skip_consent_for_verifiable_first_party_clients"] = True
    else:
        state["connections"] = []
    with pytest.raises(RuntimeError):
        onboarding.configure_onboarding()
    assert mutations == snapshots == []


def test_existing_default_is_narrowed_without_duplicate(provider):
    state, mutations, _ = provider
    state["grants"].append(
        {
            "id": "default",
            "default_for": "third_party_clients",
            "audience": onboarding.MCP_AUDIENCE,
            "subject_type": "user",
            "scope": [*onboarding.auth0.MCP_SCOPES, "agents:manage", "alerts:critical"],
        }
    )
    onboarding.configure_onboarding()
    assert mutations[0] == (
        "patch",
        "client-grants/default",
        {"scope": sorted(onboarding.auth0.MCP_SCOPES)},
    )
    assert len(state["grants"]) == 3


def test_grant_inspection_does_not_stop_at_first_page(provider):
    state, mutations, _ = provider
    state["grants"] = [{"client_id": f"existing-{i}"} for i in range(100)] + [
        {
            "default_for": "third_party_clients",
            "audience": onboarding.ACCOUNT_AUDIENCE,
            "subject_type": "user",
        }
    ]
    with pytest.raises(RuntimeError):
        onboarding.configure_onboarding()
    assert mutations == []
