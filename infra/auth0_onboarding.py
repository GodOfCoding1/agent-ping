"""Configure automatic MCP client onboarding without re-running provider bootstrap.

Run `... python infra/auth0_onboarding.py status` for a read-only check, or
`... python infra/auth0_onboarding.py configure` to apply the approved DCR policy.
Provider responses are captured; snapshots stay in ignored owner-only storage.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

import auth0_setup as auth0
from production import ORIGIN, save

MCP_AUDIENCE = ORIGIN + "/mcp"
ACCOUNT_AUDIENCE = ORIGIN + "/api/v1"
TENANT_POLICY = {
    # Advertise the automatic flow we actually support. Existing explicit
    # Codex and portal clients keep their IDs, callbacks and grants.
    "client_id_metadata_document_supported": False,
}


def tenant_changes(tenant: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {k: v for k, v in TENANT_POLICY.items() if tenant.get(k) != v}
    if tenant.get("flags", {}).get("enable_dynamic_client_registration") is not True:
        changes["flags"] = {"enable_dynamic_client_registration": True}
    return changes


def list_all(path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(100):
        batch = auth0.api("get", path, query={"per_page": "100", "page": str(page)})
        if not isinstance(batch, list):
            raise RuntimeError("Unexpected provider list response")
        items.extend(batch)
        if len(batch) < 100:
            return items
    raise RuntimeError("Provider list exceeded pagination limit")


def inspect_onboarding() -> dict[str, Any]:
    return {
        "tenant": auth0.api("get", "tenants/settings"),
        "resources": list_all("resource-servers"),
        "grants": list_all("client-grants"),
        "connections": list_all("connections"),
    }


def validate_boundaries(state: dict[str, Any]) -> list[dict[str, Any]]:
    tenant = state["tenant"]
    if tenant.get("client_id_metadata_document_dynamic_registration_supported"):
        raise RuntimeError("Dynamic CIMD is already active; review before switching registration")
    if tenant.get("resource_parameter_profile") != "compatibility":
        raise RuntimeError("MCP resource parameter compatibility must be configured first")
    if not tenant.get("authorization_response_iss_parameter_supported"):
        raise RuntimeError("OAuth authorization response issuer identification is required")
    for audience in (MCP_AUDIENCE, ACCOUNT_AUDIENCE):
        resource = next((r for r in state["resources"] if r["identifier"] == audience), None)
        if resource is None:
            raise RuntimeError("AgentPing APIs must be configured first")
        policy = resource.get("subject_type_authorization", {})
        if (
            policy.get("user", {}).get("policy") != "require_client_grant"
            or policy.get("client", {}).get("policy") != "deny_all"
            or resource.get("skip_consent_for_verifiable_first_party_clients") is not False
        ):
            raise RuntimeError("AgentPing API access or consent policy requires review")
        if (
            resource.get("signing_alg") != "RS256"
            or resource.get("token_dialect") != "rfc9068_profile"
            or resource.get("token_lifetime") != 600
        ):
            raise RuntimeError("AgentPing access-token policy requires review")
        if audience == MCP_AUDIENCE and not resource.get("allow_offline_access"):
            raise RuntimeError("MCP refresh access must be configured first")
    if not any(
        c.get("strategy") == "google-oauth2" and c.get("is_domain_connection")
        for c in state["connections"]
    ):
        raise RuntimeError("Google sign-in must be available to third-party applications")
    defaults = [g for g in state["grants"] if g.get("default_for") == "third_party_clients"]
    if any(g.get("audience") != MCP_AUDIENCE or g.get("subject_type") != "user" for g in defaults):
        raise RuntimeError("Unexpected third-party default grants; review before opening DCR")
    if len(defaults) > 1:
        raise RuntimeError("Duplicate MCP default grants require review")
    return defaults


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    defaults = validate_boundaries(state)
    scopes = sorted(auth0.MCP_SCOPES)
    return {
        "dcr_enabled": state["tenant"]
        .get("flags", {})
        .get("enable_dynamic_client_registration", False),
        "cimd_advertised": state["tenant"].get("client_id_metadata_document_supported", False),
        "mcp_default_scopes": sorted(defaults[0].get("scope", [])) if defaults else [],
        "ready": not tenant_changes(state["tenant"])
        and bool(defaults)
        and sorted(defaults[0].get("scope", [])) == scopes,
    }


def configure_onboarding() -> dict[str, Any]:
    before = inspect_onboarding()
    defaults = validate_boundaries(before)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    save(f"auth0-onboarding-before-{stamp}.json", before)
    scopes = sorted(auth0.MCP_SCOPES)
    # Establish the limited user-delegated default before accepting new clients.
    if not defaults:
        auth0.api(
            "post",
            "client-grants",
            {
                "default_for": "third_party_clients",
                "audience": MCP_AUDIENCE,
                "subject_type": "user",
                "scope": scopes,
            },
        )
    elif sorted(defaults[0].get("scope", [])) != scopes:
        auth0.api("patch", "client-grants/" + defaults[0]["id"], {"scope": scopes})
    changes = tenant_changes(before["tenant"])
    if changes:
        auth0.api("patch", "tenants/settings", changes)
    result = summarize(inspect_onboarding())
    if not result["ready"]:
        raise RuntimeError("Provider read-back did not confirm the onboarding policy")
    save("auth0-onboarding-verification.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["status", "configure"])
    args = parser.parse_args()
    try:
        result = (
            configure_onboarding() if args.stage == "configure" else summarize(inspect_onboarding())
        )
        print(json.dumps(result, sort_keys=True))
    except Exception as error:
        print(f"Auth0 onboarding failed ({type(error).__name__}); no credentials printed.")
        raise SystemExit(1) from None
