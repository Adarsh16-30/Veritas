"""API keys and roles.

A key looks like ``vk.<client_id>.<secret>``. The client id says which row to
check; only a per-key salt and ``sha256(salt + secret)`` are stored, compared in
constant time. The secret is 32 random bytes, so a fast hash suffices: there is
nothing to brute-force, and what matters is that a database dump does not hand
out working keys.

Roles:

* ``submitter``: submit procurement requests; read their own.
* ``approver``: read every workflow; decide escalations they did not submit.
* ``viewer``: read every workflow.
* ``admin``: everything.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

ROLES = ("submitter", "approver", "viewer", "admin")
PREFIX = "vk"
CLIENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


@dataclass(frozen=True)
class Principal:
    client_id: str
    roles: frozenset[str]

    def has(self, *roles: str) -> bool:
        return "admin" in self.roles or any(r in self.roles for r in roles)


def hash_secret(salt: str, secret: str) -> str:
    return hashlib.sha256(f"{salt}:{secret}".encode()).hexdigest()


def new_key(client_id: str) -> tuple[str, str, str]:
    """``(token, salt, hash)`` for a new client. The token is shown once."""
    if not CLIENT_ID.match(client_id):
        raise ValueError("client_id must be 2-63 chars of a-z, 0-9 and '-', starting alphanumeric")
    secret = secrets.token_urlsafe(32)
    salt = secrets.token_hex(16)
    return f"{PREFIX}.{client_id}.{secret}", salt, hash_secret(salt, secret)


def parse_token(token: str) -> tuple[str, str] | None:
    """``(client_id, secret)`` from a bearer token, or None if malformed."""
    parts = token.split(".", 2)
    if len(parts) != 3 or parts[0] != PREFIX or not CLIENT_ID.match(parts[1]) or not parts[2]:
        return None
    return parts[1], parts[2]


def verify(secret: str, salt: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(salt, secret), expected_hash)
