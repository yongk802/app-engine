"""Versioned, salted owner credentials, including upgrade of legacy SHA-256."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

ROUNDS = 600_000
PREFIX = 'pbkdf2-sha256-v1'
_LEGACY = re.compile(r'[a-f0-9]{64}\Z')


def legacy_hash(stored: object) -> bool:
    return isinstance(stored, str) and _LEGACY.fullmatch(stored) is not None


def hash_secret(secret: str) -> str:
    if not isinstance(secret, str) or not 1 <= len(secret) <= 256:
        raise ValueError('admin secret must contain 1 to 256 characters')
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac('sha256', secret.encode('utf-8'), salt, ROUNDS)
    return f'{PREFIX}${ROUNDS}${salt.hex()}${derived.hex()}'


def verify_secret(candidate: object, stored: object) -> bool:
    if not isinstance(candidate, str) or len(candidate) > 256 or not isinstance(stored, str):
        return False
    try:
        if legacy_hash(stored):
            return hmac.compare_digest(hashlib.sha256(candidate.encode()).hexdigest(), stored)
        scheme, rounds, salt, digest = stored.split('$')
        if (scheme != PREFIX or not rounds.isascii() or not rounds.isdecimal()
                or len(rounds) > 7 or not ROUNDS <= int(rounds) <= 2_000_000
                or not re.fullmatch(r'[a-f0-9]{32}', salt)
                or not re.fullmatch(r'[a-f0-9]{64}', digest)):
            return False
        derived = hashlib.pbkdf2_hmac('sha256', candidate.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(derived.hex(), digest)
    except (ValueError, UnicodeError):
        return False
