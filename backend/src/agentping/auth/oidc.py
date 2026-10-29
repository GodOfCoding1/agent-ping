"""Managed-provider access-token verification; never treats an ID token as API access."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from agentping.config import Settings
from agentping.domain.errors import Unauthenticated

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerifiedJwt:
    subject: str
    client_id: str
    scopes: frozenset[str]
    claims: dict[str, Any]


class OidcVerifier:
    """RFC 9068 RS256 access tokens, exact issuer/audience, bounded JWKS refresh."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=10)
        self._keys: jwt.PyJWKSet | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _jwks_url(self) -> str:
        if self._settings.oidc_jwks_url:
            return self._settings.oidc_jwks_url
        issuer = self._settings.oidc_issuer.rstrip("/")
        response = await self._http.get(f"{issuer}/.well-known/openid-configuration")
        response.raise_for_status()
        metadata = response.json()
        if metadata.get("issuer") != self._settings.oidc_issuer:
            raise Unauthenticated("Provider discovery issuer mismatch")
        uri = str(metadata["jwks_uri"])
        if not uri.startswith("https://"):
            raise Unauthenticated("Provider signing keys must use HTTPS")
        return uri

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

    async def _decode(self, token: str, audience: str, *, access: bool) -> dict[str, Any]:
        if len(token) > 16384:
            raise Unauthenticated("Bearer token is too large")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise Unauthenticated("Token signing algorithm is not permitted")
            if access and header.get("typ") not in ("at+jwt", "application/at+jwt"):
                raise Unauthenticated("An API access token is required, not an ID token")
            kid = header.get("kid")
            if not isinstance(kid, str) or not kid or len(kid) > 200:
                raise Unauthenticated("Missing token signing key identifier")
            key = None
            for attempt in range(2):
                keys = await self._key_set(force=attempt > 0)
                key = next((k for k in keys.keys if k.key_id == kid), None)
                if key is not None:
                    break
            if key is None:
                raise Unauthenticated("Token signing key is not trusted")
            required = ["exp", "iat", "sub", "iss", "aud"]
            if access:
                required += ["client_id", "jti"]
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=["RS256"],
                audience=audience,
                issuer=self._settings.oidc_issuer,
                options={"require": required},
                leeway=30,
            )
            subject = claims["sub"]
            if not isinstance(subject, str) or not subject or len(subject) > 255:
                raise Unauthenticated("Invalid user identity")
            if claims["exp"] <= claims["iat"]:
                raise Unauthenticated("Invalid token lifetime")
            if (
                access
                and claims["exp"] - claims["iat"] > self._settings.oidc_max_token_lifetime_seconds
            ):
                raise Unauthenticated("Access token lifetime exceeds policy")
            return claims
        except jwt.PyJWTError as error:
            logger.info("oidc_verification_rejected", extra={"error_type": type(error).__name__})
            raise Unauthenticated("Invalid or expired access token") from error
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            # Never echo provider responses or the raw bearer token.
            logger.warning("oidc_provider_failure", extra={"error_type": type(error).__name__})
            raise Unauthenticated("Identity provider could not validate this token") from error

    async def verify(self, token: str, *, audience: str | None = None) -> VerifiedJwt:
        claims = await self._decode(token, audience or self._settings.oidc_audience, access=True)
        client_id = claims["client_id"]
        if not isinstance(client_id, str) or not client_id or len(client_id) > 255:
            raise Unauthenticated("Missing OAuth client identity")
        if claims.get("azp") not in (None, client_id):
            raise Unauthenticated("Conflicting OAuth client identity")
        if claims.get("gty") == "client-credentials" or claims["sub"].endswith("@clients"):
            raise Unauthenticated("A signed-in user is required")
        raw_scopes = claims.get(self._settings.oidc_scope_claim)
        if not isinstance(raw_scopes, str) or not raw_scopes.strip():
            raise Unauthenticated("A scoped access token is required")
        # permissions may be broader than the user's delegated scope. Never union it.
        return VerifiedJwt(claims["sub"], client_id, frozenset(raw_scopes.split()), claims)

    async def verify_id_token(self, token: str, *, client_id: str, nonce: str) -> dict[str, Any]:
        claims = await self._decode(token, client_id, access=False)
        received = claims.get("nonce")
        if not isinstance(received, str) or not secrets.compare_digest(received, nonce):
            raise Unauthenticated("Browser login nonce mismatch")
        if claims.get("azp") not in (None, client_id):
            raise Unauthenticated("ID token belongs to another client")
        return claims
