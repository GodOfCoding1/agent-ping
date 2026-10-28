"""OIDC/OAuth 2.0 JWT access-token verification against a managed provider's JWKS."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from agentping.config import Settings
from agentping.domain.errors import Unauthenticated


@dataclass(frozen=True)
class VerifiedJwt:
    subject: str
    client_id: str | None
    scopes: frozenset[str]
    claims: dict[str, Any]


class OidcVerifier:
    """Validates RS256/ES256 access tokens: signature, issuer, audience, expiry and scopes."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=10)
        self._keys: jwt.PyJWKSet | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def _jwks_url(self) -> str:
        if self._settings.oidc_jwks_url:
            return self._settings.oidc_jwks_url
        issuer = self._settings.oidc_issuer.rstrip("/")
        response = await self._http.get(f"{issuer}/.well-known/openid-configuration")
        response.raise_for_status()
        return str(response.json()["jwks_uri"])

    async def _key_set(self, *, force: bool = False) -> jwt.PyJWKSet:
        async with self._lock:
            stale = time.monotonic() - self._fetched_at > 3600
            recently = time.monotonic() - self._fetched_at < 30
            if self._keys is None or stale or (force and not recently):
                response = await self._http.get(await self._jwks_url())
                response.raise_for_status()
                self._keys = jwt.PyJWKSet.from_dict(response.json())
                self._fetched_at = time.monotonic()
            return self._keys

    async def verify(self, token: str) -> VerifiedJwt:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise Unauthenticated("Malformed bearer token") from error
        kid = header.get("kid")
        key = None
        for attempt in range(2):
            keys = await self._key_set(force=attempt > 0)
            key = next((k for k in keys.keys if k.key_id == kid), None)
            if key is not None:
                break
        if key is None:
            raise Unauthenticated("Token signing key is not trusted")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=["RS256", "ES256", "PS256"],
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
                leeway=30,
            )
        except jwt.PyJWTError as error:
            raise Unauthenticated("Invalid or expired access token") from error
        raw_scopes = claims.get(self._settings.oidc_scope_claim) or claims.get("scope") or ""
        scopes = raw_scopes.split() if isinstance(raw_scopes, str) else list(raw_scopes)
        permissions = claims.get("permissions")
        if isinstance(permissions, list):
            scopes.extend(str(p) for p in permissions)
        client_id = claims.get("azp") or claims.get("client_id")
        return VerifiedJwt(
            subject=str(claims["sub"]),
            client_id=str(client_id) if client_id else None,
            scopes=frozenset(scopes),
            claims=claims,
        )
