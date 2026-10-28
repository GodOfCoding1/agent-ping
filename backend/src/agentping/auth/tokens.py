"""Opaque credential generation and hashing. Only SHA-256 digests are stored."""

from __future__ import annotations

import hashlib
import hmac
import secrets

AGENT_PREFIX = "apa_"
DEVICE_PREFIX = "apd_"
PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def display_prefix(token: str) -> str:
    return token[:12]


def generate_pairing_code() -> str:
    raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_pairing_code(code: str) -> str:
    cleaned = code.strip().upper().replace(" ", "").replace("-", "")
    return f"{cleaned[:4]}-{cleaned[4:]}"


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())
